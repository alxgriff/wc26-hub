# Brian's ML Hybrid Build Plan

**Goal.** Add a Groll-style ML overlay (gradient-boosted trees) on top of the
existing wc26-hub structural model as a **pure add-on**. The structural pipeline
(`predict_match`, score matrix, totals/BTTS/DNB, `resolve_knockout`) is **not
modified at all**. The ML layer produces a separate W/D/L triple shown
side-by-side, so we can compare in real time as the tournament runs and decide
later whether to promote it.

**Why side-by-side, not integrated.** Keeps the structural model's behavior
bit-for-bit identical, avoids the score-matrix coherence problem, and gives a
clean A/B comparison via the existing Brier/RPS ledger. Promotion (if any) is a
later, evidence-based decision.

---

## The shape

```
predict_match(model, a, b, hfa)         # untouched — same outputs as today
hybrid_predict(model, a, b, ctx)        # NEW — separate W/D/L triple
render_prediction(model, pred, hybrid=hybrid_pred)  # shows both, side by side
```

`hybrid_predict` calls `predict_match` internally to *get* the structural
features, then runs the booster, then returns its own `(p_a, p_draw, p_b)` as a
separate object. Nothing about the structural prediction, the score matrix, or
`resolve_knockout` changes. The score model / totals / BTTS keep coming from
the structural side only — they are **not** hybridized.

---

## What's added

### 1. `scripts/hybrid.py` — the new module

```python
@dataclass
class HybridPrediction:
    team_a: str
    team_b: str
    p_a: float
    p_draw: float
    p_b: float
    source: str          # e.g. "xgb-v1 fit 2026-06-15"
    asof: str

def hybrid_predict(model, team_a, team_b, *, hfa_team=None,
                   stage="group") -> HybridPrediction | None:
    booster = _load_booster()           # None if data/calibration/hybrid.ubj absent
    if booster is None:
        return None                     # inert — render falls back to structural-only
    struct = predict_match(model, team_a, team_b, hfa_team=hfa_team)
    feats = _features(model, struct, team_a, team_b, stage)
    p_a, p_draw, p_b = booster.predict_proba(feats)
    return HybridPrediction(team_a, team_b, p_a, p_draw, p_b,
                            source=booster.meta["source"],
                            asof=booster.meta["asof"])
```

### 2. `scripts/fit_hybrid.py` — trainer

Trains the booster against `data/History/results.csv` using the structural
model's outputs as features, walk-forward CV, RPS objective, time-decay
weights. Writes `data/calibration/hybrid.ubj` + a metrics card. Negative result
is fine — just don't write the artifact, and the live system stays
structural-only (same discipline as `fit_rho.py`).

### 3. `predict.py` integration — three small edits, no logic changes

```python
# CLI: opt-in flag, default off so today's output is bit-identical
ap.add_argument("--hybrid", action="store_true", help="show ML overlay alongside")

...
pred = predict_match(model, a, b, hfa_team=hfa)
hyb  = hybrid_predict(model, a, b, hfa_team=hfa, stage=stage) if args.hybrid else None
print(render_prediction(model, pred, overlay_row=overlay_row, hybrid=hyb))
```

And in `render_prediction`, one extra block when `hybrid` is non-None:

```
- **Model (structural):** Mexico 41% · Draw 28% · South Korea 31%
- **ML overlay (xgb-v1):** Mexico 46% · Draw 22% · South Korea 32%
  (RPS gain on held-out WC matches: +3.2% vs structural-only; fit 2026-06-15, n=10,748)
```

That's the full user-facing surface. The headline line, score model, totals,
BTTS, DNB, knockout `resolve_knockout` — all untouched.

---

## Features the booster reads

All from things wc26-hub already has — no new data sources:

| Group | Features |
|---|---|
| Structural outputs | `p_a, p_draw, p_b, lam_a, lam_b, total, sup, texture` |
| Raw ratings | `elo_a, elo_b, futi_a, futi_b, strength_a, strength_b, elo_gap, futi_gap, zAtt_a, zDef_a, zAtt_b, zDef_b` |
| Context | `tournament_weight, neutral, host_at_home, stage` |
| Optional when present | Opta `p_home/draw/away`, market implied |

Form lags (5/10-match) are nice-to-have but not in the rating files today —
skip in v1, add in v2 if RPS gain warrants it.

---

## Model + objective

- **Gradient-boosted trees (XGBoost or LightGBM), `multi:softprob`, 3 classes.**
- **Loss: ranked probability score** (or multiclass log-loss with monotonic
  regularization on `p_a − p_b` vs `sup`). RPS respects the W/D/L ordering —
  Ley 2019 and most international-football papers report it for a reason.
- **Time-decay sample weights** on the training loss (Dixon-Coles half-life
  trick): `w_i = exp(-(t_now - t_i) / τ)` with τ ≈ 1.5 years. Canonical recency
  knob.
- **Capacity: small.** 100–300 trees, depth 3–4, high regularization. The
  structural features already carry most of the signal; the hybrid is learning
  *corrections*, not the function from scratch. Overfitting risk is high on
  ~10k internationals.

---

## Training data caveat

The historical training corpus needs **as-of-match-date** features, which
means a historical Elo time series. Two options, in order of effort:

- **v1: roll Elo from `data/History/results.csv`** the way `predict_today.py`
  does. Self-contained. You lose historical Futi (which doesn't exist anyway)
  but get a clean historical Elo. The booster trains on Elo-derived structural
  features and at predict time still gets Futi as an extra feature — it just
  won't have learned to weight it heavily.
- **v2:** swap in the Kaggle `afonsofernandescruz/2026-fifa-world-cup-historical-elo-ratings`
  daily-snapshot dataset for both training and live predict-time. Kills two
  birds: also addresses the static-ratings problem flagged in
  `MODEL_IMPROVEMENTS.md` §3.2.

---

## Validation

Same "fit broad, validate narrow" discipline as the rest of the project:

- **Walk-forward CV by year.** Train through *Y−1*, validate on *Y*. Roll
  forward. Never train on the future.
- **Deployment-slice reporting.** Headline metrics computed on major-tournament
  matches only (WC, Euros, Copas), with **group and knockout reported
  separately**.
- **Metrics:** RPS (primary), multiclass log-loss, reliability curves. Brier
  as a secondary check.
- **Acceptance bar:** hybrid must beat structural-only on RPS *on the
  deployment slice*, by a margin larger than the walk-forward CV's noise.
  Otherwise don't ship the booster artifact — the layer stays inert.
  (Same "the data rejected it" pattern as the ρ activation.)

---

## Ledger / accountability

CLAUDE.md already specifies a Brier ledger for predictions. The add-on slots in
naturally:

- `picks_log.csv` / `predictions_log.csv` gets a `source` column
  (`structural` vs `hybrid_v1`).
- Both are logged per match.
- The daily recap reports Brier and RPS for each, side by side.
- After ~20–30 matches there's a real comparison; decide whether to promote the
  hybrid to the headline or leave it as a watch-this-space curiosity.

---

## Inertness contract

Same discipline as ρ and per-host HFA:

- No `data/calibration/hybrid.ubj` → `hybrid_predict()` returns None →
  `render_prediction` shows exactly today's output.
- `--hybrid` flag default off → CLI behavior unchanged unless explicitly
  asked.
- Regression test: with the flag off (or the artifact absent),
  `render_prediction` is bit-for-bit identical to pre-change for a fixed
  fixture set.

---

## Build order

1. **`fit_hybrid.py`** — rolling-Elo corpus + structural-features-per-historical-match
   + booster fit + walk-forward CV + metrics card. **~1.5 days; bulk of the
   work.**
2. **`hybrid.py`** module with `HybridPrediction` + `hybrid_predict()` + loader.
   ~few hours.
3. **`predict.py`** edits: CLI flag, one call site, one extra render block.
   ~hour.
4. **Ledger column + recap** in `build_edition.py` / `ledger.py`. ~hour or two.
5. **Decide later** based on accumulated Brier/RPS — but the decision is "do
   we promote to headline," not "do we keep the layer." Side-by-side is the
   steady state.

Total: ~2–3 focused days. Structural model never changes. Booster artifact can
be deleted at any time to disable the add-on without touching code.

---

## Realistic expectation of gains

Looking at the Groll-Ley-Schauberger WC2014/18/22 papers, hybrid vs
pure-Poisson typically lifts RPS by **2–5%**. Real, but not "now you're picking
65% of games." The hybrid's biggest practical wins are:

- **Draw calibration** at lower-than-DC totals (where ρ was inert) — the model
  can learn the international draw rate empirically rather than imposing a
  parametric form.
- **Regime correction** — WC knockouts behave differently from CONCACAF
  qualifiers, and the model can absorb that without hand-coded tournament
  weights.
- **Tail behavior** in lopsided matchups, where independent Poisson is known
  to underweight upsets.

What it *won't* fix: stale Elo (still need the §3.2 snapshot feed), missing
injury/lineup info, the small-sample variance of any single tournament.
