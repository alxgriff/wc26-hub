# Porting the WC26 Daily Hub to Claude Code

This kit moves the project from claude.ai (where we built the content and data layer) into Claude Code (where we'll automate standings, scenarios, predictions, and edition assembly). Everything Claude Code needs to know is in the `CLAUDE.md` below — paste it verbatim into the repo root.

---

## 1. Why port, and what stays where

**Claude Code takes over:** anything repetitive or computational — standings math, third-place permutations, the Poisson prediction layer, Brier/units/CLV tracking, and assembling each day's edition file from the pre-baked cards.

**claude.ai keeps:** the project knowledge base lives here, so editorial judgment calls (rewriting a card after a major injury, recap prose, "what does this result mean" analysis) can continue in this project. The two workflows share the same files; the repo is the source of truth once created.

---

## 2. Repo structure

```
wc26-hub/
├── CLAUDE.md                      ← paste from section 4 below
├── data/
│   ├── fixtures.csv               ← wc26_group_stage_fixtures.csv (rename)
│   ├── ratings.csv                ← Phase 2: your model output (schema in CLAUDE.md)
│   └── odds_log.csv               ← Phase 3: created by scripts
├── kb/
│   └── 2026_fifa_world_cup_guide.md   ← copy from project knowledge
├── cards/
│   ├── template.md                ← wc26_match_card_template_and_samples.md
│   ├── md1.md                     ← wc26_cards_md1.md
│   ├── md2.md                     ← wc26_cards_md2.md
│   └── md3.md                     ← wc26_cards_md3.md
├── calendar.md                    ← wc26_daily_hub_calendar.md
├── editions/                      ← generated daily (2026-06-12.md, ...)
└── scripts/                       ← built by Claude Code (see prompts)
```

## 3. Setup steps

1. Download the five files from this claude.ai project (fixtures CSV, calendar, template, three card files) plus the knowledge-base markdown.
2. `mkdir wc26-hub && cd wc26-hub && git init`, create the folders above, place the files per the tree.
3. Create `CLAUDE.md` at the repo root with the contents of section 4.
4. Open Claude Code in the repo and run the prompts in section 5, in order.

---

## 4. CLAUDE.md — paste everything in this block

```markdown
# WC26 Daily Hub

Daily 2026 World Cup group-stage guide + game predictor. One edition per ET
date (June 11–27), built from pre-baked match cards + computed standings +
model predictions. Group stage only; knockout stage is a later project.

## Data contracts (do not violate)

### data/fixtures.csv — single source of truth for matches
Columns: match_id, group, matchday, date_et, kickoff_et_24h, kickoff_et,
team_a, team_b, stadium, city, country, tv_us, score_a, score_b, status, notes
- match_id: group letter + 1–6 (A1–L6). MD1 = ids 1–2, MD2 = 3–4, MD3 = 5–6.
- status: "scheduled" | "played". score_a/score_b empty until played.
- date_et is the **ET calendar date** of kickoff. Three games kick off at
  12:00 AM ET (D2, J2, F4): they belong editorially to the **previous day's
  edition** (🌙 late-cap convention) even though date_et is the next day.
- Never edit team names, kickoff times, or match_ids without explicit ask.

### Team-name canon — all joins are exact-string on these names
Use exactly: "United States", "South Korea", "South Africa", "Bosnia and
Herzegovina", "Côte d'Ivoire", "Türkiye", "DR Congo", "Cape Verde",
"New Zealand", "Saudi Arabia", "Czechia", "Curaçao". Any ratings/odds file
the user supplies must be normalized to this canon before joining; if a
name doesn't match, stop and report it rather than fuzzy-matching silently.

### data/ratings.csv — Phase 2 (user-supplied models + Opta aggregate)
Preferred schema (match-level): match_id, p_home, p_draw, p_away, source, asof
Fallback schema (team-level): team, rating, source, asof — in this case
scripts/predict.py converts rating gaps to W/D/L probabilities via a
Poisson layer (rating gap → expected goals per side → score matrix → W/D/L).
Aggregation across sources: simple average of probabilities unless the user
specifies weights. Probabilities must sum to 1.0 ± 0.001 per match.

## Tournament rules (encoded in standings/scenario scripts)
- 12 groups of 4, single round-robin. Top 2 per group + 8 best third-placed
  teams advance to a Round of 32.
- Group tiebreakers, in order: points, goal difference, goals scored,
  head-to-head, fair play points, drawing of lots.
- Third-place ranking across groups: points, GD, goals scored, fair play, lots.
- MD3 games within a group kick off simultaneously.

## Daily edition workflow (scripts/build_edition.py output)
1. Update fixtures.csv with yesterday's results (status → played, scores in).
2. Run standings (group tables + live third-place table).
3. Pull today's match cards from cards/md{1,2,3}.md by match_id.
4. Fill live sections: **Stakes** (from standings/scenarios), **The Call**
   (from predictions once Phase 2 is live), **Odds & Best Bet** (Phase 3).
5. Recap section: yesterday's predictions vs results — Brier score running
   ledger, plus units and CLV once betting goes live.
6. Write to editions/YYYY-MM-DD.md.

## Verification rules
- Cards were pre-baked June 11. **Injury/selection notes must be refreshed
  from current news before publishing** — MD2/MD3 cards say so in their
  headers. Anything tagged "(verify before use)" must be web-verified or cut.
- Tunisia–Japan (F4) kickoff was flagged for re-verification (listed as
  12:00 AM ET June 21 / 10 PM June 20 Monterrey). Confirm before that edition.
- The kb/ guide is the tactical source of truth as of June 11; prefer it for
  squad/system facts, and the web for anything after that date.

## Prediction accountability
- Every published prediction gets logged: match_id, p_home/p_draw/p_away,
  predicted score, timestamp. Brier score = mean squared error of the W/D/L
  probability vector vs the 1/0/0 outcome vector; report cumulative + per-day.

## Phase 3 — Odds & Best Bet methodology
- At publish time, snapshot the market: 3-way moneyline, draw no bet, total
  goals O/U. Log to data/odds_log.csv: match_id, market, selection, odds
  (decimal), book/consensus source, timestamp.
- De-vig the 1X2: implied_i = (1/odds_i) / Σ(1/odds_j) (multiplicative
  method; power or Shin method optional upgrade later).
- Edge_i = model_p_i − implied_i. Best bet = largest positive edge **if** it
  clears the threshold (default 3 percentage points, user-tunable).
  Otherwise output "No bet" — a legitimate, expected result.
- Track closing line value: log closing odds for every pick; CLV = closing
  implied minus snapshot implied on our selection. Report units (flat 1u
  stakes unless told otherwise) and CLV alongside Brier in recaps.
- Never invent odds. If no snapshot was provided/fetched, the section stays
  in placeholder state.

## Style
- Editions are markdown, prose-forward; match cards keep their 9-section
  format from cards/template.md. Don't editorialize injuries beyond sourced
  facts. Flag uncertainty rather than smoothing it over.
```

---

## 5. First-session prompts for Claude Code (run in order)

1. **Standings engine:** "Read CLAUDE.md and data/fixtures.csv. Write scripts/standings.py that computes all 12 group tables and the third-place ranking table per the tiebreaker rules, from played matches only. Output both as markdown to stdout and as a function importable by other scripts. Add a couple of unit tests with synthetic results."

2. **Edition builder:** "Write scripts/build_edition.py per the daily workflow in CLAUDE.md: given a date, pull that date's cards (respecting the 🌙 midnight convention), inject current standings into each card's Stakes slot as a starting block, and write editions/YYYY-MM-DD.md. Leave The Call and Odds sections untouched until their phases are live."

3. **Scenario calculator (before June 24):** "Write scripts/scenarios.py that, for a given group entering MD3, enumerates all outcome combinations of the two simultaneous games and reports each team's advancement status (top-2 locked, third-place alive, eliminated), including current third-place table context."

4. **Phase 2, when ratings arrive:** "Here's my ratings file. Normalize team names to the canon in CLAUDE.md, then write scripts/predict.py implementing the match-level or Poisson path as appropriate, and a prediction ledger with Brier scoring per CLAUDE.md."

5. **Phase 3, when going live on odds:** "Implement the odds workflow from CLAUDE.md: odds_log.csv schema, de-vig, edge calculation with the 3-point threshold, no-bet handling, and the CLV/units ledger wired into the recap output."

---

## 6. Operating notes

- **Verify in whichever tool has web access at the moment.** Claude Code can fetch pages if its permissions allow; otherwise do the morning news check in claude.ai and paste findings into the edition prompt.
- **Don't regenerate cards in Claude Code from scratch** — they encode KB synthesis. Edit them surgically when news demands it.
- **Commit after each edition.** The git history becomes your accountability trail alongside the Brier/units ledger.
