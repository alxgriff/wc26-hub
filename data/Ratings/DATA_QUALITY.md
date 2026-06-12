# data/Ratings — provenance & data-quality audit

Four predictive rating sources for the 48 WC2026 teams (each provider's own data
dictionary is in `World_Cup_2026_Rankings_README.txt`). Audited June 12, 2026
before use in the predictor (`scripts/predict.py`).

## Which file to use

| Source | Status | Use it for |
|---|---|---|
| **Elo** | ⚠️ original corrupted — **use `Elo_Ratings_World_Cup_2026_VERIFIED.csv`** | match-strength backbone |
| **Futi** (`...Futi_Detailed_Profiles_Final.csv`) | ✅ reliable | Attack/Defense goals model + tactical color |
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
- **Futi is the most trustworthy** — it independently rated Morocco a top-8 side,
  catching the Elo error.
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

## Name normalization (to the CLAUDE.md canon) — required before any join

`Congo DR → DR Congo`, `Ivory Coast → Côte d'Ivoire`, `USA → United States`,
`IR Iran → Iran`, `Korea Republic → South Korea`, `Turkey → Türkiye`,
`Czech Republic → Czechia`, `Bosnia-Herzegovina → Bosnia and Herzegovina`,
`Cape Verde Islands → Cape Verde`. (Futi uses a different naming convention than
Elo/Opta/Zeileis.) On any name that does not resolve to the canon: stop and report.
