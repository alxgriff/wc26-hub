# WC26 Hub — Implementation Plan

Execution guide for completing the hub. Written June 12, 2026, after the
standings engine shipped. Work through the phases in order; the **Daily ops
checklist** at the bottom is the recurring loop that starts today.

Read CLAUDE.md first — its data contracts override everything here.

---

## Current state (verified June 12)

| Piece | Status |
|---|---|
| `data/fixtures.csv` | ✅ Complete: 72 rows, canon names, A1 played (Mexico 2–0). Renamed from `wc26_group_stage_fixtures.csv`. |
| `scripts/standings.py` | ✅ Done, reviewed, 20 tests green. Importable API: `load_fixtures(path)`, `compute_standings(matches, fair_play=None)`, `render_markdown(standings)`. Run: `python scripts/standings.py`. |
| `tests/test_standings.py` | ✅ 20 tests. Run: `python -m unittest discover -s tests` |
| `cards/md1.md`, `md2.md`, `md3.md` | ✅ Moved into place. |
| `cards/template.md` | ✅ In place (June 12): 9-section template + full B1 and D1 cards. NOTE its card-header format differs — see Phase 1 step 2. |
| `kb/2026_fifa_world_cup_guide.md`, `calendar.md` | ✅ In place. |
| A2 result (South Korea–Czechia, June 11 10 PM ET) | ✅ Entered June 12: 2–2, status played. CSV now shows 2 of 72 played. |
| git | Initialized June 12 with an initial commit. Commit after each edition (the accountability trail). |

## Standing guardrails (every phase, every session)

1. **Never edit team names, kickoff times, or match_ids** in fixtures.csv without explicit ask.
2. **Never invent** scores, odds, injury news, or probabilities. Results come from the user (or web-verified sources). If an input is missing, leave the section in placeholder state and say so.
3. All team-name joins are **exact-string** against the canon (CLAUDE.md / calendar.md). On any mismatch: stop and report. `standings.py` already raises/warns on this — keep that behavior in new scripts.
4. Python stdlib only. Import the `standings.py` API; never re-implement ranking/tiebreak logic anywhere else (no second implementation to drift).
5. After any code change: `python -m unittest discover -s tests` must be green.
6. New scripts follow the `standings.py` pattern: importable functions + small CLI, `sys.stdout.reconfigure(encoding="utf-8")` in main (Windows), data-integrity complaints to stderr, editorial output to stdout.
7. Editions are prose-forward markdown; flag uncertainty rather than smoothing it over.

---

## Phase 1 — `scripts/build_edition.py` (build TODAY; needed for the June 12 edition)

**Spec** (port doc §5 prompt 2): `python scripts/build_edition.py 2026-06-12 [--fixtures ...]` writes `editions/2026-06-12.md`.

1. **Select today's matches** by *editorial date*:
   `editorial_date = date_et - 1 day if kickoff_et_24h == "00:00" else date_et`.
   Exactly three matches shift (D2, J2, F4 — the 🌙 late-cap games); assert that set and warn if a new 00:00 row ever appears. Sort by kickoff (🌙 game last).
2. **Pull each match's card** from `cards/`:
   - md1/md2 cards: slice from `^## {match_id}:` to the next `^## ` (or EOF), dropping a trailing `---` separator. Matchday from the match_id digit: 1–2 → md1.md, 3–4 → md2.md, 5–6 → md3.md.
   - md3 cards use `^### {match_id}:` headers nested under `## June N` sections — slice `###`-to-next-`###`-or-`##`.
   - B1/D1 live in `cards/template.md` in a THIRD format: H1 headers with team names but no match_id (`# 🇨🇦 Canada vs Bosnia and Herzegovina 🇧🇦`), sections at `##` level, cards separated by doubled `---` lines. Extraction rule for this file only: find the `^# ` line containing BOTH team_a and team_b (exact canon strings from the fixtures row); slice until the next `^# ` line or `^## Pre-bake status`, stripping trailing `---` lines. The card's section headers are `##` (not bold-inline like md1/md2), so the Stakes slot here is a `## Stakes` section whose body is a `*[...]*` placeholder paragraph — replace the placeholder body, keep the header.
   - If a card is missing anywhere, insert a clearly marked placeholder block and warn on stderr. Do not synthesize card prose.
3. **Inject standings into each card's Stakes slot.** Cards carry `**Stakes:** *[...]*` placeholders. Replace with a starting block: the match's group table (from `render_markdown` output, or a compact 4-row slice) + one factual sentence of context (e.g. "Mexico lead Group A on 3 pts; both these teams are on 0"). Keep it factual — no scenario claims until Phase 3 (scenarios.py) exists.
4. **Leave `**The Call:**` and `**Odds & Best Bet:**` slots exactly as-is** until their phases are live.
5. **Edition skeleton**, in this order (fan reading order):
   - Masthead: `# WC26 Daily — {Weekday} June {D}` + day number (June 11 = Day 1) + match count + first kickoff/TV.
   - **Overnight**: yesterday's editorial-date results from the CSV (score lines + the `notes` field). Once predict.py exists, add per-match grading + Brier (Phase 4). If a result is missing from the CSV, print "result not yet entered" — loudly.
   - **Today's slate**: one line per match: time, match_id, teams, TV, stadium. 🌙 flagged.
   - **Standings snapshot**: full `render_markdown(standings)` output inside `<details><summary>All tables</summary>...</details>`. From June 18 (MD2), also surface the third-place table outside the fold (calendar says the tracker debuts ~June 18, centerpiece June 22–27).
   - **Match cards** (with Stakes filled).
   - Header note for MD2/MD3 cards: injury/selection notes are pre-baked — **verify day-of before publishing** (the card files say this themselves; anything "(verify before use)" must be web-verified or cut).
6. **Tests** (`tests/test_build_edition.py`): synthetic card text + tmp fixtures CSV (use `tempfile`); cover (a) card extraction for all three formats (`## A1:` md1/md2, `### A5:` md3, H1-by-team-names template.md), (b) the 🌙 mapping — assert D2/J2/F4 land on June 13/16/20 editions and date-only queries for June 14 do NOT include D2, (c) Stakes replacement leaves The Call/Odds untouched, (d) missing card → placeholder + warning, not a crash.

**Acceptance:** tests green; `python scripts/build_edition.py 2026-06-12` produces a readable edition with B1+D1 (or placeholders if template.md still missing); no invented content.

**June 20 edition special check:** F4 (Tunisia–Japan 🌙) kickoff is flagged for re-verification in fixtures notes + CLAUDE.md. Verify before publishing that edition; if unverifiable, print the flag in the edition.

## Phase 2 — results entry helper (small, optional but useful)

`python scripts/enter_result.py A2 0-0` → sets score_a/score_b, status=played on that row, preserving all other columns/quoting (csv module round-trip, utf-8-sig). Refuses to overwrite an existing played result without `--force`. Test with a tmp CSV. This keeps daily edits mechanical and contract-safe; without it, results are edited by hand in the CSV (fine too).

## Phase 3 — `scripts/scenarios.py` (build June 22–23; REQUIRED before June 24)

For a group entering MD3 (two simultaneous games): enumerate all **9 W/D/L outcome combinations**. For each combo compute final points; rank with `standings.py` logic where decidable. Margins are unknown at W/D/L level, so for clusters tied on points where GD/GF would decide, **do not fabricate margins** — classify the slot as "margin-dependent" and report the live context (current GDs, the H2H result if those teams already played). Per team output: in how many of 9 combos they finish top-2 / 3rd / out / margin-dependent, plus plain-language stakes ("wins and is through; draws and needs Qatar not to beat Bosnia", etc.) and the current third-place table so "3rd" can be read against the cutline.

CLI: `python scripts/scenarios.py A` → markdown to stdout, plus an importable `enumerate_scenarios(group, matches)` for build_edition's Stakes slots on MD3 days.

Tests: synthetic group where (a) a team is locked top-2 in all 9 combos, (b) a combo is margin-dependent, (c) a team is eliminated in all 9.

⚠️ **This is the trickiest logic in the project** (tiebreak interactions under unknown margins). If running on a smaller model: implement, then ask the user to let a stronger model review this one script before June 24. Budget a day of slack — it must work for the June 24 edition.

## Phase 4 — `scripts/predict.py` + prediction ledger (when the user supplies `data/ratings.csv`)

Per CLAUDE.md: match-level schema (`match_id,p_home,p_draw,p_away,source,asof`) used directly; team-level schema (`team,rating,source,asof`) goes through a Poisson layer (rating gap → expected goals per side → score matrix → W/D/L). **Propose the gap→xG mapping constants to the user and get sign-off before first publication** (e.g. base total ~2.6 goals split by a logistic of the gap — but the user decides). Validate: canon names (stop on mismatch), probabilities sum to 1.0 ± 0.001, simple average across sources unless weights given.

Ledger: append every published prediction to `data/predictions_log.csv` (`match_id,p_home,p_draw,p_away,predicted_score,timestamp`) at edition build time. Brier = MSE of the 3-vector vs the 1/0/0 outcome; recap shows per-match + per-day + cumulative. Wire into build_edition's Overnight section (grade ✓/✗, per-match Brier, running ledger line).

Tests: Poisson path probabilities sum to 1; Brier of a certain correct call = 0, of a certain wrong call = 2; ledger append idempotence (re-running a build must not double-log — key on match_id).

## Phase 5 — Odds & Best Bet (when the user goes live; CLAUDE.md "Phase 3")

`data/odds_log.csv` (`match_id,market,selection,odds,source,timestamp`). De-vig 1X2 multiplicatively: `implied_i = (1/odds_i) / Σ(1/odds_j)`. Edge = model_p − implied; display threshold 3 percentage points. Recorded picks (evolved June 12): up to **3 per match**, the best selection per distinct market, each ≥ 5pp edge and ≤ 15pp sanity ceiling; same-match picks are correlated and the UI says so. Below the bar print "No bet" (a normal outcome). Log closing odds for every pick; CLV = closing implied − snapshot implied. Recaps report units (flat 1u) + CLV next to Brier. **If no odds snapshot was provided, the section stays in placeholder state — never fetch-and-guess, never invent.**

## Phase 6 — static HTML site ✅ (shipped June 12; extended same day)

Live: `python scripts/build_site.py` renders docs/ — index (standings hub),
48 team cards (`docs/teams/{slug}.html`, parsed from kb/ by site_content.py),
72 matchup previews (`docs/matches/{id}.html`: card prose + live Stakes + The
Call via a defensive predict.py adapter + Odds placeholder per contract).
Played matches grade the model call (probability on outcome + Brier). All
templates in templates/ share site.css (inlined per page, zero JS). Daily ops
step: rebuild + commit docs/ after entering results. Original spec follows:

## Phase 6 (original spec) — static HTML site (optional polish; any time after Phase 1)

Per the agreed design direction (see `~/.claude` project memory `display-direction` / the June 12 design panel): `scripts/build_site.py` renders a single self-contained `docs/index.html` from the same `Standings` object — stdlib `string.Template`, no Node, no build step. 12 group cards in a CSS grid; third-place race with the top-8 cutline as a structural rule; green/neutral/grey status (wired via optional statuses arg, populated by scenarios.py later); tiebreak notes as footnote daggers, lots-⚠️ promoted. Add `to_dict(standings)` to standings.py (unit-tested) and embed the JSON in the page. Host: GitHub Pages from `/docs` or just open the file. Do NOT build: frameworks, bundlers, auto-refresh, flag emoji (broken on Windows).

---

## Daily automation (live June 12) — .github/workflows/

`daily-build.yml` (07:00 ET + manual dispatch) runs the morning pipeline:
results from The Odds API (`fetch_results.py`) → settle picks → log slate
predictions → odds snapshot → evaluate + record picks → Sonnet stakes blurb
(`stakes_blurb.py`, grounded in computed data only, renders on the index) →
optional UNVERIFIED news digest (`fetch_news.py`, gated on the
`ENABLE_NEWS_DIGEST` repo variable or a dispatch input; writes news/, never
auto-publishes) → tests (hard gate) → site + edition build → push (= deploy).
`closing-odds.yml` (11:30 AM + 6:30 PM ET) logs closing lines for CLV.
Secrets required: `ODDS_API_KEY`, `ANTHROPIC_API_KEY`.

The news digest (enabled June 12 via the ENABLE_NEWS_DIGEST variable) is
relayed automatically on match pages as "The Wire" — attributed reporting
in a distinct box, never house voice. Human jobs the automation deliberately
leaves: turning verified claims into card edits or `data/discipline.csv`
rows (cards → fair-play tiebreaker; columns yellows/second_yellow_reds/
direct_reds/yellow_plus_reds — these change computed standings, so they
stay human), the June 20 F4 kickoff re-verification, and reading the
edition before trusting it. The checklist below remains the manual fallback.

## Daily ops checklist (manual fallback)

1. Get yesterday's results from the user (scores for every match on yesterday's *editorial* date — including the 🌙 game on late-cap nights). Enter into `data/fixtures.csv` (status → `played`, scores in). **Today: A2 South Korea–Czechia is outstanding.**
2. `python scripts/standings.py` — sanity-check tables; stderr must be silent (warnings = data problem, fix before publishing).
3. Web-check overnight injury/team news for today's teams; surgically update today's cards if news demands (sourced facts only — don't regenerate cards).
4. `python scripts/build_edition.py YYYY-MM-DD` (+ scenarios for MD3 groups from June 24; predictions/odds once Phases 4–5 live).
5. Read the edition top to bottom before calling it done; anything "(verify before use)" gets verified or cut.
6. `git add -A; git commit -m "Edition YYYY-MM-DD"`.
7. June 20 only: re-verify the F4 Tunisia–Japan kickoff first.

## Suggested model split

- **Smaller model handles fine:** daily ops, Phase 1 (precisely specced above), Phase 2, Phase 5 mechanics, Phase 6.
- **Ask for a stronger pass on:** Phase 3 (scenarios tiebreak/margin logic) before June 24, and the Phase 4 Poisson mapping when ratings arrive. Everything else here is deliberately mechanical.
