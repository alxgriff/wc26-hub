# data/Ratings — provenance & data-quality audit

Four predictive rating sources for the 48 WC2026 teams (each provider's own data
dictionary is in `World_Cup_2026_Rankings_README.txt`). Audited June 12, 2026
before use in the predictor (`scripts/predict.py`).

## Which file to use

| Source | Status | Use it for |
|---|---|---|
| **Elo** | ⚠️ original corrupted — **VERIFIED is the anchor; `..._CURRENT.csv` (rolled, gitignored) is preferred live** | match-strength backbone |
| **Futi** (`World_Cup_2026_Futi_6_28.csv`, active) | ✅ reliable, refreshed at the group→knockout boundary | match-strength (1.5× weight) + Attack/Defense goals model |
| **Opta** (`Opta_Predictions...`) | ✅ reputable, but tournament-level | context overlay only (advance %, win %) |
| **Market** (`Market_Outrights_VERIFIED.csv`) | ✅ real market, de-vigged | public-sentiment context + divergence flags |
| **Zeileis** (`Zeileis_Hybrid_Model...`) | ❌ both key columns broken | **not used at all** |

## What the audit found

- **Elo — the supplied `Elo_Ratings_World_Cup_2026.csv` is corrupted.** 17 of 48
  teams are off by ≥80 points (Morocco +326, Haiti −268, South Korea +250,
  Sweden +242); the top ~18 are correct, the bottom two-thirds are scrambled in
  both directions. Replaced with `Elo_Ratings_World_Cup_2026_VERIFIED.csv`
  (source: international-football.net, asof 2026-06-11, canon team names). The
  replacement validates: Elo-vs-Futi rank correlation rises 0.73 → 0.94. The
  original is kept only for provenance — **do not use it.**
- **Elo auto-rolls forward (2026-06-19).** `scripts/update_elo.py` rolls VERIFIED (the committed
  6/11 anchor) through played WC results (K=60, goal-diff multiplier, host +100) into
  `Elo_Ratings_World_Cup_2026_CURRENT.csv` — **gitignored, regenerated each nightly build**, and
  preferred by the model when present. Deterministic from VERIFIED + fixtures.csv, so it's a
  reproducible artifact (run the script), not a new source of truth; VERIFIED stays the leak-free
  anchor (and the exact-value regression baseline pins to it via `elo_current=False`).
- **Futi is the most trustworthy** — it independently rated Morocco a top-8 side,
  catching the Elo error.
- **Futi = futi.live** (Imburgio/Muller, ex-American Soccer Analysis): an Expected
  Possession Value / "goals added" model — chance-quality, *orthogonal* to results-based
  Elo. Its tournament ratings are match-driven (dynamic). **`World_Cup_2026_Futi_6_28.csv`**
  is the latest, transcribed at the group→knockout boundary from the futi.live "Teams
  rankings" list view (Att/Def/Rat per team, `futi_6_28/IMG_0038-0042`, gitignored, ranks
  1-45). Display-only Formation/Top_Player/Coach are carried forward from 6/24 (the list view
  omits them); three eliminated teams not in the 6/28 screenshots (Qatar, Haiti, Curaçao — out
  of the tournament, no remaining matches) are carried forward unchanged from 6/24. Ingested
  for going-forward (knockout) predictions only — played games stay graded from immutable
  logged calls (no leakage). Prior vintages: `World_Cup_2026_Futi_6_24.csv` (post-MD2),
  `World_Cup_2026_Futi_6_18.csv` (post-MD1), `World_Cup_2026_Futi_Final_Fixed_Futi_Detailed_Profiles_Final.csv`
  (pre-tournament). The 1.5× Futi blend weight and this whole call are documented in
  DECISIONS.md (2026-06-18) and reproducible via `scripts/eval_blend.py`.
- **Opta** is from Stats Perform and its ordering is sound, but its numbers are
  tournament-path probabilities (bracket-confounded), so they are context, not
  match strength.
- **Zeileis squad value is broken** (Cape Verde €524m > Japan €117m; Norway
  €24m), which also corrupted its own tournament win-probability column. Dropped.
- **Zeileis "Bookmaker_Consensus_Odds_Decimal" is not market data** (June 12
  audit): every one of the 48 rows equals `1/(1.30 × Win_Probability)` — a
  uniform multiplier (stdev 0.02), i.e. the model's own output dressed up as
  odds. Checked against the real outright market (BetMGM, June 12): only 15/48
  teams within 1.5×; errors up to 51× (Haiti listed 48.5 vs real 2501).
- **`Market_Outrights_VERIFIED.csv`** is the replacement: real BetMGM outright
  odds (via Yahoo Sports, asof 2026-06-12), with raw and de-vigged implied
  probabilities (overround 1.242). This is the public-sentiment reference for
  the `sources_diverge` flag in `data/team_strength.csv`.
- **`Opta_Power_Ratings_PARTIAL.csv`** — Opta's match-level Power Ratings
  (0–100) salvaged from The Analyst articles: 45/48 teams (per-row `asof`,
  mostly 2025-12-05 draw-day vintage, some 2026-06-05). Missing: Ecuador,
  Côte d'Ivoire, Bosnia and Herzegovina. Validates strongly: +0.97 rank
  correlation with verified Elo, +0.96 with Futi — third independent
  confirmation of the corrected Elo. Context/validation only until the user
  pulls a complete, current table (then candidate third consensus source).

## Per-match Opta predictions (`Opta_Match_Predictions.csv`)

The Analyst publishes the Opta supercomputer's W/D/L probabilities for every
match in a server-rendered preview article, available **at least a day ahead**,
at a predictable URL:

    theanalyst.com/articles/{team-a}-vs-{team-b}-prediction-world-cup-2026-match-preview

(team names lowercased/hyphenated, e.g. `united-states-vs-paraguay`,
`canada-vs-bosnia-herzegovina`). **Daily ops:** each morning, fetch the
preview for every match on today's editorial slate (including the 🌙 game) and
append rows here — schema `match_id,p_home,p_draw,p_away,source,asof` with
p_home = the fixtures row's team_a (CLAUDE.md's preferred match-level schema).
`predict.py {match_id}` automatically blends these with the model (simple
average per CLAUDE.md) and shows Consensus + both sources. Refresh day-of if
team news breaks — Opta re-runs its sims. The interactive pages
(/fixtures, /predictions, /stats) are client-side apps and NOT fetchable;
the articles are the reliable path. Triangulation check (June 12): C1 Brazil
57.7/23.5/18.8 (Opta) vs 55/26/19 (our model) vs 59/24/17 (de-vigged market).

## Name normalization (to the CLAUDE.md canon) — required before any join

`Congo DR → DR Congo`, `Ivory Coast → Côte d'Ivoire`, `USA → United States`,
`IR Iran → Iran`, `Korea Republic → South Korea`, `Turkey → Türkiye`,
`Czech Republic → Czechia`, `Bosnia-Herzegovina → Bosnia and Herzegovina`,
`Cape Verde Islands → Cape Verde`. (Futi uses a different naming convention than
Elo/Opta/Zeileis.) On any name that does not resolve to the canon: stop and report.

## Opta column hygiene (2026-06-13)
The supplied Opta `Advance_From_Group_%` carries raw-float artifacts for the weakest
sides (Haiti 1.020739..., Curaçao 2.587135...). Display-only (never a model input)
and already masked by output format specifiers; `predict._clamp_pct` now rounds to 2dp
and clamps to [0,100] on ingest as a belt-and-suspenders guard.
