# data/History — provenance & quality

## Corpus: `results.csv`
- **Source:** `martj42/international_results` (the GitHub repo that feeds the Kaggle
  mirror `martj42/international-football-results-from-1872-to-present`).
- **License:** CC0 (public domain).
- **Downloaded:** 2026-06-13.
- **Re-fetch (anonymous, no Kaggle account needed):**
  ```
  curl -sSL -o data/History/results.csv \
    https://raw.githubusercontent.com/martj42/international_results/master/results.csv
  ```
- **Not committed** (gitignored, ~3.6 MB). Reproducible via the command above.
- **Schema:** `date,home_team,away_team,home_score,away_score,tournament,city,country,neutral`
  — current team names, no round/stage column (group vs knockout is **not** derivable
  from this file alone; use `openfootball` round-coded data for that). 49,478 rows.
- **Curation for fitting (`scripts/fit_rho.py`):** drop friendlies, drop unplayed
  (`NA` scores — incl. the 68 future WC2026 fixtures and the 4 already-played ones, to
  avoid leakage), restrict to ≥ 2010. → ~10,748 competitive matches.

## Result of the rho fit (2026-06-13)
`fit_rho.py` fit the Dixon-Coles low-score parameter and validated it out-of-sample
(W/D/L log-loss & Brier on 2023+ holdout):

- Fitted **rho ≈ −0.015** (full window) / −0.008 (train) — an order of magnitude
  smaller than the club-football literature's −0.13…−0.18.
- Out-of-sample log-loss and Brier did **NOT** improve (both marginally worse).
- Diagnostic: independent Poisson already predicts draws at ~22.9% vs an empirical
  ~22.0% — it slightly **over**-predicts draws, so the negative-rho DC correction
  (which *adds* draw mass) moves calibration the wrong way.

**Decision: rho is NOT activated** (no `data/calibration.json` is written, so
`Config.rho` stays 0.0 and the model is unchanged). The Tier-1 DC mechanism remains
in place and ready; the club-football prior simply does not transfer to international
football here, and the validation gate (correctly) rejected it. Re-run `fit_rho.py`
if a better corpus / lambda model (e.g. a full Maher fit) or the knockout regime
(more cautious, may differ) warrants revisiting.
