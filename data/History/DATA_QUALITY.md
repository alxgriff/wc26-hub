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

## Result of the HFA measurement (2026-06-13)
`scripts/fit_hfa.py` measured international home advantage on 7,216 non-neutral
competitive matches (2010+):

- **Home edge at even strength ≈ +0.455 goals ≈ ~67 Elo pts** (full crowd). The
  current `Config.hfa = 60` is therefore **corroborated** — a sensible split-crowd
  discount of the ~67 full-crowd edge, not an arbitrary number.
- **Per-match host-strength scaling: c = +0.032** — the spec's (Kalwij) direction
  (stronger host → larger edge), but **negligible**: out-of-sample home-GD RMSE
  improves by only 0.001 (≈ noise). And it's moot for WC2026 — the three hosts
  (US/Mexico/Canada) are mid-strength, so a scaled HFA ≈ the flat value for them.

**Decision: keep the flat `hfa = 60` (validated); do NOT add per-match strength
scaling.** Built the host-aware structure instead: `Config.hfa_by_host` (optional
`{host: elo_pts}`, default None ⇒ flat for every host, regression-guarded). Prefer
FITTING per-host values against actual results as the tournament runs (or setting
crowd-context values deliberately) over guessing — `data/calibration.json` can carry
`hfa` / `hfa_by_host` to activate. Not written, so the model is unchanged.

## Result of the hybrid fit (2026-06-15)
`scripts/fit_hybrid.py` trained a Groll-style XGBoost overlay (200 trees,
depth 4, η=0.05, multi:softprob, τ=1.5y time-decay sample weights) on the same
10,756 curated competitive matches (2010-01-01..2026-06-14), train/holdout split
at 2023-01-01. Features: structural outputs (p_a/p_draw/p_b/lam_a/lam_b/total/
sup/texture) + rolling Elo + context (tournament_weight, is_neutral,
home_advantage_side). Booster artifact (XGBoost native binary) would have lived
at `data/calibration/hybrid.ubj` + `hybrid.meta.json`.

Holdout (n=2686 competitive; n=95 deployment-slice = WC/Euro/Copa):

| slice               |   n  | RPS struct → hybrid | logloss | Brier   |
|---------------------|------|---------------------|---------|---------|
| all competitive     | 2686 | 0.1716 → 0.1776 (+3.5%) | 0.871 → 0.897 | 0.511 → 0.525 |
| major tournaments   |   95 | 0.1961 → 0.2055 (+4.8%) | 1.027 → 1.061 | 0.615 → 0.637 |

The overlay **made things worse**, not better, on every slice and every metric.
Per-year on the deployment slice: 2024 (n=83) +0.0086 RPS, 2026 (n=12) +0.0149
RPS. Per-year noise σ ≈ 0.00444; the observed margin (+0.0094) is ~2σ in the
WRONG direction.

Likely causes: (a) the structural triple `p_a/p_draw/p_b` is already an
near-sufficient statistic of what the consensus-Elo+Futi model "thinks", leaving
no signal for tree boosters to recover beyond what they overfit to; (b) τ=1.5y
decays pre-2018 matches to negligible weight, so effective training data is
~2,500 matches with 14 mostly-redundant features (a known XGBoost overfit
regime); (c) tree boosters typically produce peakier probabilities than a
well-calibrated structural baseline, and the wc26 structural model is already
well-calibrated (the predict.py audit notes ~26% draw at even strength,
historically consistent).

**Decision: hybrid is NOT activated** (no `data/calibration/hybrid.ubj` is
written, so predict.py output stays bit-for-bit identical to the structural-only
behavior). The `--hybrid` CLI flag is implemented and inert (silent stderr note),
ready to activate if a v2 fit clears the bar. The same "data rejected it"
discipline as ρ — the validation gate (correctly) refused to ship an overlay
that doesn't help.

Plausible v2 directions if revisited: longer τ (5–10y, larger effective sample);
drop the redundant `p_a/p_draw/p_b/lam_a/lam_b` features and keep only `sup`
+ Elo + context (force the booster to learn from raw rather than digested
inputs); add information the structural model lacks (Transfermarkt squad value,
form lags, H2H); calibrate the booster output (isotonic / Platt) post-fit.
