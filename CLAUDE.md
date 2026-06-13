# WC26 Daily Hub

**Roadmap:** see PLAN.md for the phased implementation plan, per-script specs,
and the daily ops checklist.

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
  predicted score, timestamp. Brier score = sum of squared errors of the W/D/L
  probability vector vs the 1/0/0 outcome vector (multiclass Brier, range 0–2;
  0.667 = coin-flip baseline); report cumulative + per-day.

## Phase 3 — Odds & Best Bet methodology
- At publish time, snapshot the market: 3-way moneyline (1X2) and total goals
  O/U, plus Asian handicap (spreads) and both-teams-to-score where quoted. Log
  to data/odds_log.csv: match_id, market, selection, line, odds (decimal),
  source, phase, timestamp. The 1X2 edge is vs the published consensus;
  totals/handicap/BTTS are model-priced from the score matrix (the overlay
  covers W/D/L only). Draw-no-bet is computed but not yet snapshotted/recorded.
- De-vig the 1X2: implied_i = (1/odds_i) / Σ(1/odds_j) (multiplicative
  method; power or Shin method optional upgrade later).
- Edge_i = model_p_i − implied_i. Display threshold 3 percentage points;
  **recorded picks**: up to 3 per match, the best selection per distinct
  market, each clearing a 5-point recording bar (user-set June 12) and the
  15-point sanity ceiling. Same-match picks are correlated — disclose it
  wherever they're shown. Otherwise output "No bet" — a legitimate,
  expected result.
- Track closing line value: log closing odds for every pick; CLV = closing
  implied minus snapshot implied on our selection. Report units (flat 1u
  stakes unless told otherwise) and CLV alongside Brier in recaps.
- Never invent odds. If no snapshot was provided/fetched, the section stays
  in placeholder state.

## Style
- Editions are markdown, prose-forward; match cards keep their 9-section
  format from cards/template.md. Don't editorialize injuries beyond sourced
  facts. Flag uncertainty rather than smoothing it over.