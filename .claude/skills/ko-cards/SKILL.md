---
name: ko-cards
description: Generate knockout match cards WITHOUT an ANTHROPIC_API_KEY, using Sonnet subagents + the pipeline's own grounding/assembly code. Use when cards are missing for resolved ties and the CI card step can't run (no key locally, tie outside the window, key/API failure).
---

# Knockout cards via subagents (keyless fallback)

The pipeline path is `scripts/knockout_cards.py` (CI runs it with the repo secret;
±3-day window). When that path can't run, reproduce it exactly — same grounding
contract, same assembly code — with subagents standing in for the API call.
Card quality contract lives in `knockout_cards.py` (SYSTEM prompt) and CLAUDE.md.

## Procedure

1. Data first: cards need resolved participants. Run the hub-health skill's fixes if
   `team_a/team_b` are blank (results → `--resolve`) — a card can NEVER be generated
   for an unresolved tie, and never invent a matchup.

2. Dump grounding packs using the pipeline's own functions (do NOT hand-write facts —
   `build_facts` computes the advance call from the live model). In a scratchpad script:
   load knockout + fixtures, `ko.materialize_teams(bk.project(standings), matches)`,
   `sc.parse_kb(...)`, `pr.load_ratings()`, then per match_no:
   `facts = kc.build_facts(km, model=model, ko_by_no=ko_by_no)` and write
   `kc.SYSTEM + "\n\n=== USER MESSAGE ===\n" + kc._grounding_pack(km, pa, pb, facts)`
   to `pack_M{no}.txt`. (Precedent: session 2026-07-04, scratchpad `dump_packs.py`.)

3. One **Sonnet** subagent per card, in parallel (Sonnet is the pipeline's card model —
   don't burn a bigger model on this). Prompt: read the pack file; treat everything
   above `=== USER MESSAGE ===` as your system contract; final message = ONLY the four
   `## ` sections, no preamble/fences.

4. **QA every returned section against the pack before assembly** — the observed
   failure modes (real, from 2026-07-04) are embellishments the contract forbids:
   - Round mislabels: an R32 shootout described as a "group-stage draw".
   - Invented scores: "4-1 win over Ghana" when the pack said 1–0.
   - A level-aggregate feeder stated without its shootout (reads as a draw) — say
     "won on penalties" (the fact pack now tags this; check it survived).
   - Player spellings must match the pack VERBATIM (if the KB itself is wrong, fix the
     KB knowingly, not silently).
   Check every score, round name, and stat in the sections appears in the pack.

5. Assemble with the pipeline's own `kc._parse_sections` + `kc._assemble(km, sections,
   facts)` + `kc.save_card(no, card)` — never hand-build the card markdown (the site
   parser and card format stay in lockstep only if `_assemble` writes it). Assert no
   `_PLACEHOLDER` leaked.

6. Rebuild + verify + ship: `build_site.py --date <today>`, `build_edition.py <today>`,
   full test suite, then commit `cards docs editions` and push. CI skips existing cards,
   so generated cards stand (use `--force` semantics consciously if replacing).
