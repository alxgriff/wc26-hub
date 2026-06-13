# WC26-Hub Predictor — Model Improvement Spec

**For:** an implementing agent (Claude Code) working in the `wc26-hub` repo.
**Scope:** `scripts/predict.py` (and small new helpers/tests). The build pipeline,
site, and standings are out of scope unless noted.
**Status:** every quantitative claim below was checked against the literature on
2026-06-13; see **Sources**. Items are grouped into tiers by confidence and
dependency. **Read the tier guardrails — two items are deliberately marked
"do not mechanically implement."**

---

## 0. Architecture findings — what NOT to change

The audit *validated* the core design. Do not refactor these away:

- **The Elo + Futi hybrid is sound and externally endorsed.** A WC-2026 modelling
  piece (Economics Observatory, June 2026) argues explicitly *against* using Elo
  alone because it "would exclude information on whether some national teams
  operate with a more defensive or more attacking approach" — which is exactly
  why this model blends Elo (strength) with Futi attack/defense (style/goals).
  Keep the two-source consensus.
- **Independent Poisson is a top-tier base, not a weak one.** Ley et al. (2019)
  found the independent Poisson and bivariate Poisson to be the best-performing
  maximum-likelihood models for football. The base goal model is fine; it needs
  the low-score *correction* below, not replacement.
- **θ = 190 is correctly fit** to the Elo expectancy curve and locked by a
  regression test. Leave it. Any new constant must follow the same
  fit-then-lock discipline (the project's stated philosophy: fit, don't
  hand-pick — see the predictor docstring's rejection of an invented ρ).
- **Only the ρ piece of Dixon-Coles applies here.** The full Dixon-Coles method
  has two parts: (a) the low-score ρ correction, and (b) exponential
  time-decay (ξ) weighting of historical matches when *fitting attack/defense
  from a match history*. This model does **not** fit att/def from history — it
  reads current Elo/Futi ratings, which already encode recency. **Do NOT add ξ
  time-decay weighting to the predictor; it would double-handle recency.** Only
  implement the ρ correction (Tier 1).

---

## Tier 1 — Implement now (self-contained, no new data required)

### 1.1 Dixon-Coles low-score (ρ) correction

**Why.** Independent Poisson systematically under-weights the 0-0 / 1-1 corner,
so it under-predicts draws in low-scoring games. In the current model the modal
score is (1,0) for nearly every competitive fixture — the classic symptom. The
Dixon-Coles τ correction shifts mass from 1-0/0-1 into 0-0/1-1, lifting the draw
line where it should be lifted.

**Verified specification** (τ form and sign confirmed against Dixon & Coles 1997
and multiple reproductions — see Sources):

```
τ(x,y) = 1 − λ_a·λ_b·ρ   if (x,y) = (0,0)
         1 + λ_a·ρ        if (x,y) = (0,1)
         1 + λ_b·ρ        if (x,y) = (1,0)
         1 − ρ            if (x,y) = (1,1)
         1                otherwise
```
- `λ_a` = expected goals for `team_a` (listed-first side), `λ_b` for `team_b`.
- **ρ < 0 raises P(0-0) and P(1-1)** and slightly lowers P(1-0)/P(0-1) — the
  empirically correct direction. ρ is "almost always estimated as a small
  negative value"; published football fits typically cluster **−0.13 to −0.18**
  (the often-quoted −0.03…−0.15 band is loose at the low-magnitude end — allow the
  grid search down to ~−0.20). [corrected 2026-06-13: independent lit review]
- Apply τ to the **un-normalised** score matrix, *then* let the existing
  normalisation run. This is exactly the literature form
  `P(x,y) = τ(x,y)·Poisson(x;λ_a)·Poisson(y;λ_b)` followed by renormalisation,
  and it keeps W/D/L, totals, and BTTS summing to 1.

**Code change** (in `scripts/predict.py`):

1. Add a module-level helper:
   ```python
   def _dc_tau(i: int, j: int, lam_a: float, lam_b: float, rho: float) -> float:
       """Dixon-Coles low-score dependence correction (rho<0 lifts 0-0 & 1-1)."""
       if i == 0 and j == 0: return 1.0 - lam_a * lam_b * rho
       if i == 0 and j == 1: return 1.0 + lam_a * rho
       if i == 1 and j == 0: return 1.0 + lam_b * rho
       if i == 1 and j == 1: return 1.0 - rho
       return 1.0
   ```
2. Add `rho: float = 0.0` to `Config` with a comment: *placeholder; activate via
   the fitted value from `fit_rho.py` (Tier 2). 0.0 ⇒ identical to current
   independent Poisson.* **Default MUST stay 0.0** so this commit is behaviour-
   preserving until ρ is fit on data (consistent with the project's no-invented-
   constants rule).
3. In `predict_match`, right after the matrix is built and before the existing
   `z = sum(matrix.values())` normalisation, multiply each cell by τ:
   ```python
   matrix = {(i, j): matrix[(i, j)] * _dc_tau(i, j, lam_a, lam_b, cfg.rho)
             for (i, j) in matrix}
   ```
   Leave the existing normalisation and all downstream W/D/L / over / BTTS /
   `top_scores` logic unchanged.

**Tests** (mirror the existing θ-calibration regression test):
- **Inertness:** with `rho = 0.0`, every output of `predict_match` is bit-for-bit
  identical to pre-change for a fixed set of fixtures (e.g. C1, B2, D2).
- **Direction & monotonicity:** for a low-total even fixture, P(draw) strictly
  increases as ρ moves from 0 → −0.03 → −0.06 → −0.10 → −0.13.
- **Magnitude sanity:** at ρ = −0.06, the draw-probability increase is small and
  bounded (single-digit pp) and larger for low-total ties than for lopsided ones.
- **Validity:** τ stays positive for all cells across ρ ∈ [−0.15, 0] and the
  fixtures' λ range (guards the known DC constraint; safe here because ρ < 0 and
  λ are moderate).

**Reference magnitudes** (computed on the *current* model, ρ applied post-hoc, to
size the effect — not committed numbers):

| fixture | total | current D% | ΔD at ρ=−0.06 | ΔD at ρ=−0.13 |
|---|---:|---:|---:|---:|
| Brazil v Morocco | 2.28 | 26.0% | +1.4pp | +3.1pp |
| Australia v Türkiye | 2.84 | 25.1% | +1.4pp | +3.1pp |
| France v Norway | 2.91 | 21.1% | +1.2pp | +2.6pp |
| Qatar v Switzerland | 3.01 | 10.1% | +0.5pp | +1.1pp |

The correction is self-targeting: large on close, low-scoring ties; negligible on
mismatches. That is the desired behaviour.

### 1.2 Knockout resolution layer (mechanism)

**Why.** A knockout match cannot end level after 90 minutes: if tied it goes to
extra time (two full 15-minute halves, not sudden death), then a penalty shootout.
The current `predict_match` returns a 90-minute W/D/L — the wrong terminal shape for
a knockout. **This is NOT a reason for a second model.** The core (consensus,
supremacy, Poisson matrix, the 1.1 DC correction) is identical for a knockout; only
the *draw mass* must be re-routed into "advance" probability. So this is a thin layer
on top of the matrix `predict_match` already produces. (Blending two models would be
strictly worse: two parameter sets, an arbitrary blend weight, and loss of the single
coherent score distribution everything downstream reads.)

**Behaviour-preserving.** The layer activates **only for knockout fixtures** (gate on
a `stage`/`round` flag, or an explicit `advance=True` argument). Group-stage calls are
untouched — `predict_match` keeps returning 90-minute W/D/L exactly as today.

**Resolution (reuses the 90-minute matrix):**
```
P(A advances) = p_a + p_draw · P(A advances | level after 90)

P(A advances | level after 90) = q_a + q_d · s_a
    where (q_a, q_d, q_b) = W/D/L from an extra-time Poisson matrix with
        λ_ET_a = lam_a · (30/90) · c ,   λ_ET_b = lam_b · (30/90) · c
        (same supremacy share; the same _dc_tau correction applies), and
    s_a = P(A wins the shootout) = 0.5     # flat — see below
```

**Defaults (documented, tunable):**
- `c` — extra-time caution/fatigue discount on the per-minute scoring rate. Default
  `0.85`; **flagged for calibration in 2.5.** (`c = 1.0` treats ET as simply
  proportional to its length; empirically ET is a touch more cautious, hence `< 1`.)
- **Shootout `s_a = 0.5`, flat, NO strength tilt.** A 2025 study of 268 UEFA shootouts
  (2000–2025) finds them "equivalent to a perfect lottery": Elo-stronger teams do
  **not** win more often, and kicking order / venue / momentum show no effect. **Do
  NOT add a supremacy-based shootout tilt** — unsupported and only adds noise.

**Do NOT bump the regulation total for knockouts.** Independent stats sources show
knockout matches average **fewer** goals per 90' than group games (~2.3–2.5 from the
Round of 16 on vs ~2.7 in groups), **not more**. The often-quoted "~2.8 knockout"
figure is inflated because it counts extra-time goals — which this layer already adds
via the ET period. So if anything regulation μ for knockouts is slightly *lower*;
raising it would be doubly wrong. Keep regulation μ as-is (any small fitted adjustment
belongs in 2.5 and must be validated, not assumed). [corrected 2026-06-13: the earlier
"knockouts higher-scoring" premise was refuted in direction — independent review]

**Tests:**
- **Group untouched:** every group-fixture output is identical to pre-change.
- **Normalisation:** `P(A advances) + P(B advances) = 1` for knockout fixtures.
- **Monotonicity / symmetry:** stronger A ⇒ higher P(A advances); at equal strength
  with `s_a = 0.5`, P(A advances) → 0.5.
- **Frequency band (from WC history):** ~32% of knockout matches reach extra time;
  in the modern (post-2004, no golden goal) era the MAJORITY of those go to penalties,
  so **≈20–25% of knockout matches reach a shootout** (FiveThirtyEight: 25.3% over the
  last five men's WCs). Averaged over a realistic slate, the layer's implied reach-ET /
  reach-shootout rates should land near ~32% / ~20–25%. [corrected 2026-06-13: the
  earlier "~40% of ET → pens, ≈13% shootout" was backwards/too low — independent review]

---

## Tier 2 — Data-dependent (needs a historical corpus; unlocks 1.1's ρ and honest validation)

> **Data acquisition (applies to the Tier 2 / Tier 3 Kaggle pulls — read this
> first).** These datasets live on Kaggle, which requires authentication; the
> implementing agent's sandbox
> will usually **not** have the user's Kaggle credentials *or* network access to
> kaggle.com. **Do not assume you can download them at runtime.** Expect the files
> to be placed in the repo by the user, verify their presence, and stop-and-report
> if missing (same contract as the canon-resolution checks) rather than failing
> mid-run.

**Two ways to get the files into the repo:**

1. **Manual (default; no agent credentials needed).** The user downloads from the
   dataset page in a browser, unzips, and drops the CSV at the path below.
2. **Kaggle CLI (only if credentials exist in the environment).**
   ```bash
   pip install kaggle
   # API token: kaggle.com -> Account -> "Create New API Token" -> kaggle.json
   mkdir -p ~/.kaggle && cp kaggle.json ~/.kaggle/ && chmod 600 ~/.kaggle/kaggle.json
   # historical corpus (Tier 2.1): zip contains results.csv (+ shootouts.csv, goalscorers.csv)
   kaggle datasets download -d martj42/international-football-results-from-1872-to-present \
       -p data/History --unzip
   # daily Elo snapshots (Tier 3.2, optional): confirm exact CSV name after unzip
   kaggle datasets download -d afonsofernandescruz/2026-fifa-world-cup-historical-elo-ratings \
       -p data/EloHistory --unzip
   ```

**Expected paths after acquisition:**
- `data/History/results.csv` — main match-results file (Tier 2.1).
- `data/EloHistory/` — Elo-snapshots CSV (Tier 3.2, optional; confirm filename on unzip).

**Presence check — run before any fit/backtest (stop-and-report, don't crash mid-run):**
```python
from pathlib import Path
corpus = Path("data/History/results.csv")
if not corpus.exists():
    raise SystemExit(
        "Tier 2 needs data/History/results.csv "
        "(Kaggle: martj42/international-football-results-from-1872-to-present). "
        "Download it manually or via the Kaggle CLI, then re-run.")
```

**Gotchas:**
- **License:** check the dataset's stated license on its Kaggle page before
  vendoring the raw file into the repo, and record provenance + download date in
  `data/History/DATA_QUALITY.md`.
- **Schema:** `results.csv` has `date, home_team, away_team, home_score,
  away_score, tournament, city, country, neutral`, with **current** team names.
  Use the `neutral` flag when fitting/repurposing HFA, and run names through the
  existing `ALIAS` canon (stop-and-report on misses).
- **Slugs change:** if a dataset slug 404s, report it rather than guessing an
  alternate — the user will supply the file.

### 2.1 Build the corpus: competitive internationals, recent window (not 1872-present)

**Dataset:** `martj42 / international-football-results-from-1872-to-present` (~49,000
rows) — but **do not use it wholesale.** "Every match since 1872" mixes eras and match
types that don't reflect the current game. Note: **size is not the concern** (the file
is ~5 MB, loads instantly; fitting ρ is a millisecond op) — *relevance* is. Curate:
- **Drop friendlies** (`tournament == 'Friendly'`): rotated squads and low stakes make
  their scorelines noisy and unrepresentative of competitive national-team football.
- **Restrict to a recent window** — default start ~2010 (last four cycles). Don't
  hard-code a year blindly: **plot annual goals-per-match and draw-rate and set the
  cutoff where they stabilise**, then report the choice.
- **Tag each match with its `tournament` type** (World Cup, continental finals,
  qualifiers, Nations League, …) so ρ-sensitivity and validation can be sliced by
  regime in 2.2 / 2.3 without re-deriving.
- Store under `data/History/` with provenance + window + as-of in
  `data/History/DATA_QUALITY.md`. Canon-normalise names via `ALIAS`; stop-and-report
  on misses.

**Data gap that affects the knockout work (2.5).** martj42 carries `tournament` but
**no round/stage column**, so you cannot separate group from knockout matches from it.
For the knockout-layer calibration you need a curated knockout-match list — e.g.
derived from `openfootball` world-cup data (which encodes rounds) or compiled for the
past ~20 years of WC + continental knockouts. Treat this as its own data dependency;
**do not fabricate stage labels** from the results file.

### 2.2 `fit_rho.py` — estimate ρ on the broad competitive set

> **RESULT (2026-06-13): built, fit, and NOT activated — the data rejected it.**
> `scripts/fit_rho.py` fit ρ on 10,748 competitive internationals (2010+, friendlies
> and unplayed rows dropped) by Dixon-Coles partial-LL and validated out-of-sample
> (2023+ holdout). Fitted **ρ ≈ −0.015** — an order of magnitude below the
> club-football −0.13…−0.18 — and out-of-sample W/D/L log-loss & Brier did **not**
> improve. Diagnostic: independent Poisson already predicts draws at ~22.9% vs an
> empirical ~22.0% (it slightly *over*-predicts), so the negative-ρ correction moves
> calibration the wrong way. Per "validate, don't assume," **no `calibration.json` is
> written and `Config.rho` stays 0.0** — the Tier-1 mechanism is in place but off.
> The club-football DC prior does not transfer to international football here. Re-run
> if a fuller λ model (Maher MLE) or the knockout regime warrants it. See
> `data/History/DATA_QUALITY.md`.

- **Fit broad, not major-tournament-only.** ρ only "sees" the 0-0/0-1/1-0/1-1 corner,
  so it is robust to era and essentially unaffected by blowouts — a broad competitive
  sample gives a tighter, lower-variance estimate. (Restricting to major tournaments
  shrinks the sample hard and selects a partly mismatched regime, e.g. cautious
  knockouts — the wrong move for this parameter.)
- Fit by **maximising the Dixon-Coles log-likelihood**, or (simpler, robust) grid-
  search ρ ∈ [−0.20, 0] minimising out-of-sample multiclass log-loss / Brier on W/D/L,
  holding λ-generation fixed. Report ρ and its sensitivity **across `tournament`
  slices** as a diagnostic (expect it to be fairly stable).
- **Recency, done the canonical way:** to weight recent matches more, apply
  Dixon-Coles **exponential time-decay (a half-life)** to the *fitting* likelihood — a
  soft down-weight — rather than a hard year cutoff. Half-life ≈ 1–2 years is a
  reasonable starting point for internationals; tune it.
  - **This does NOT contradict §0's "no time-decay in the predictor" rule.** That rule
    forbids decay *inside `predict_match`*, which reads current exogenous ratings.
    Time-decay *when fitting ρ on historical results* is a different operation and is
    exactly what Dixon & Coles did. Keep the two strictly separate.
- Output: write fitted ρ to `data/calibration.json` (the loader reads it into
  `Config.rho`), with fit date, window, and sample size. Only then is the 1.1
  mechanism active in production.

### 2.3 Backtest harness — fit broad, validate narrow

- Replay historical matches through the model; measure calibration (reliability curve,
  Brier, log-loss) for W/D/L, Over/Under, BTTS — and, for knockout matches,
  advance-probability calibration.
- **Fit broad, validate narrow.** Parameters (ρ, any regime adjustments) are *fit* on
  the broad competitive set for sample size, but calibration is *reported on the
  deployment context*: major-tournament matches, and — where stage labels exist (the
  2.1 data gap) — **group vs knockout reported separately.** A model can be
  well-calibrated overall yet off on the slice you actually publish.
- **Guardrail for the betting layer:** the live ledger is three settled picks
  (−1.13u) — statistically meaningless in either direction. Do **not** tune anything to
  it. Gate any "edge" conclusion on **positive CLV over a meaningful sample**, not unit
  profit. State this in the betting-layer docs.

### 2.4 Host-specific home-field advantage (HFA) that scales with host strength

**Why (well-evidenced).** Current HFA is a single hand-set constant
(`hfa = 60` Elo-pts) applied to any host at home. The literature says HFA is
*not* one-size-fits-all:
- **Host advantage scales with the host's own strength.** Kalwij (Utrecht Univ.,
  *J. Quantitative Analysis in Sports* 2025, DOI 10.1515/jqas-2024-0056), on FIFA
  World Cups + continental championships: HA for tournament victory is ~22pp for an
  average-Elo host, ranging ~9pp (one SD below average) to ~42pp (one SD above).
  Stronger host ⇒ larger home edge. (This is tournament-victory HA, i.e. *compounded*
  over a multi-match run — use it for *direction and shape* at the advancement level,
  not as a per-match number; at single-match level the relationship can even reverse.)
  [corrected 2026-06-13: author is Kalwij, not Krumer — independent review]
- **Per-match signature is asymmetric.** A WC-2026 model (Economics Observatory,
  June 2026): home advantage reduces goals *conceded* by ~0.2 and increases goals
  *scored* by ~0.1 — the defensive effect is the larger one.
- **Partial / mixed crowds still produce sizable HA** (Scientific Reports 2021,
  COVID empty-vs-full analysis) — relevant to neutral-ish co-host NFL venues with
  split crowds, and a reason the discount-from-100 is defensible but should be
  *fit*, not guessed.

**Implementable change (simple version):**
- Replace the scalar `Config.hfa` with a small host table keyed by host nation
  (`United States`, `Mexico`, `Canada`), and/or make the bonus a function of the
  host's own rating so a stronger host gets a larger bump. Keep it **fit against
  results** as the tournament progresses rather than fixed a priori.
- **Acceptance:** a flat host table equal to the current 60 must reproduce
  today's outputs exactly (regression guard).

**Deeper refinement (optional, evaluate via 2.3 backtest):** apply HFA
asymmetrically — more to the host's defense (fewer conceded) than its attack —
rather than purely through the symmetric supremacy split, reflecting the
−0.2 / +0.1 signature. Only adopt if it improves backtest calibration.

> Caveat on a candidate third strength source (Opta Power Ratings, +0.97/+0.96
> rank-corr per `DATA_QUALITY.md`): if added to the consensus, note it shares a
> parent with the per-match Opta overlay, so using both double-counts Opta.
> Weight for the overlap; don't blend naively.

### 2.5 Knockout-layer calibration (pairs with the 1.2 mechanism)

The 1.2 mechanism ships with documented defaults (`c = 0.85`, shootout `0.5`). This
item *calibrates* it — and this is where the "major tournaments, last ~20 years" data
instinct is actually correct, because the knockout regime **is** the deployment
context for these parameters (unlike ρ, which is fit broad).

- **Data:** a curated **knockout-match list** for the past ~20 years of WC +
  continental tournaments (source per the 2.1 data gap — `openfootball` round-coded
  data, or a compiled list). martj42's results file alone can't supply stage labels.
- **Fit `c`** (the extra-time scoring discount) against observed extra-time goal rates
  and the empirical **reach-shootout frequency** (target ≈ the ~32% reach-ET / **~20–25%**
  reach-shootout marks from WC history). Do not eyeball it.
- **Shootout stays `0.5`.** Re-confirm against the knockout list if you like, but the
  prior is strong: shootouts are ≈ a fair lottery. **Do not fit a strength tilt.**
- **Regulation total:** only consider a small knockout-specific regulation-μ
  adjustment *if* the backtest (2.3) shows the regulation (pre-ET) scoreline
  distribution is miscalibrated for knockouts — and remember the ET layer already
  accounts for the extra-time goals inflating the naive "2.8 vs 2.6" figure, so the
  regulation adjustment (if any) should be small. Validate; never assume.
- **Validation:** report advance-probability calibration on held-out knockout matches
  (reliability curve over P(advance); Brier). This is the "validate narrow" half of
  2.3 applied to knockouts specifically.

---

## Tier 3 — Investigate only (DO NOT mechanically implement)

### 3.1 Goals total vs strength gap — REVISED; the earlier patch idea was wrong

**Correction to a prior suggestion.** An earlier draft proposed coupling the goals
total to the supremacy gap via a `(1 + κ·|sup|)` term. **The literature does not
support this as a fix, and it should not be implemented as a mechanical patch.**

What the audit found:
- The standard, validated goal models (Maher 1982 → Dixon-Coles → Ley 2019) derive
  **each team's** expected goals from **its own attack × the opponent's defense**.
  The total *emerges* from the four attack/defense quantities; there is **no
  separate "gap → total" term** in the canonical formulation.
- The real phenomenon a mismatch produces is **overdispersion** — fatter tails /
  occasional lopsided scores (JRSS 2025: "Overdispersion can be brought on by
  games where there is a mismatch of abilities"). The textbook remedy is an
  **overdispersed count model (e.g. negative binomial)**, i.e. more *variance*,
  not a higher *mean* total.
- This model's quirk is structural, not a missing gap term: it computes the total
  from a **symmetric att/def "texture"** `T = ((zAtt_a−zDef_b)+(zAtt_b−zDef_a))/2`
  and then **re-splits** that total by Elo supremacy. It therefore already
  contains the two correct Maher half-terms (`zAtt_a−zDef_b` and
  `zAtt_b−zDef_a`) but averages them and re-splits, which decouples the total
  from the gap.

**Options (pick via backtest, do not hard-code):**
- **(a) Leave as-is.** Defensible — independent Poisson is a top model and the
  att/def channel captures much of the gap indirectly. (Observed: France v Haiti,
  516-pt gap → total 2.96, modal (2,0); the model is not blind to blowouts.)
- **(b) Move the goals layer toward the Maher form**, letting each λ come directly
  from own-attack vs opponent-defense. **Tradeoff:** this discards the separate
  Elo-supremacy split, which is currently the model's *better* strength signal.
  Real design decision — evaluate calibration both ways on the 2.3 backtest before
  committing.
- **(c) If blowout tails specifically hurt calibration**, add overdispersion
  (negative binomial goals) rather than a mean bump.

**Instruction to implementer:** do **not** add a `κ·|sup|` total-inflation term.
Raise this as an investigation backed by the backtest; change nothing in
`predict_match`'s total/`texture` logic without backtest evidence.

### 3.2 Within-tournament Elo updating — low priority

- The K-factor the model cites is correct: **K = 60 for World Cup finals**
  (World Football Elo Ratings), with the standard goal-difference multiplier.
- But Elo "tends to converge after about 30 matches," so a 3-game group stage
  barely moves a rating — **modest payoff over the group stage.** Keep below
  Tier 2.
- If pursued, simplest path is to *consume* a daily-snapshot Elo feed rather than
  recompute: `afonsofernandescruz / 2026-fifa-world-cup-historical-elo-ratings`
  (Kaggle) publishes pre-tournament and daily-during-tournament snapshots with a
  `snapshot_date` column and is documented as backtest-safe (no future leakage).
  Validate its team-name canon and as-of date before wiring in. **License: CC BY-SA
  4.0** (attribution + share-alike) — unlike the CC0 martj42 results file — so if any
  *derived* Elo is redistributed into `docs/`, the attribution/share-alike terms apply.
  [added 2026-06-13: license caveat the spec omitted — independent review]

---

## Tier 4 — Defer

- **Per-match closing lines from a second sharp book / line aggregator.** Currently
  only BetMGM *outrights* are de-vigged (for divergence flags) and Opta is blended
  at the match level. A second source of *match* closing lines would tighten the
  overlay consensus and give CLV a real benchmark. Mostly a betting-layer gain →
  lower than accuracy items.
- **Shot-level xG goals layer (StatsBomb / FBref).** A large re-architecture for
  marginal group-stage benefit; revisit only if extending to knockouts.
- **Automated injury / lineup adjustment.** Hard to do well; keep it in the
  editorial layer (manual card rewrites on team news) unless the morning-news step
  becomes the bottleneck.

---

## Global acceptance criteria

1. **Behaviour-preserving defaults:** with `rho = 0.0`, a flat 60 host table, and the
   knockout layer gated to knockout fixtures only, `predict_match` reproduces current
   group-stage outputs exactly. All three are regression-tested.
2. **No invented constants in production:** ρ, any HFA differentiation, and the
   knockout `c` are activated only from fitted / fit-against-results values, with fit
   date and sample size recorded. The shootout term stays a flat `0.5` (no tilt).
3. **No tuning to the live ledger:** calibration claims are validated on the
   historical backtest (2.3), with the deployment slice (tournament; group vs
   knockout) reported separately; betting conclusions require positive CLV over a
   meaningful sample.
4. **Keep existing tests green** (θ calibration, canon-resolution stop-and-report) and
   add the ρ tests (1.1) and the knockout-layer tests (1.2).

---

## Sources (verified 2026-06-13)

- Dixon, M. & Coles, S. (1997), *Modelling Association Football Scores and
  Inefficiencies in the Football Betting Market*, JRSS-C 46(2):265–280 — origin of
  the τ/ρ low-score correction.
- τ form, sign, and the −0.03…−0.15 ρ range, reproduced at:
  football-bet-prediction.com (Dixon-Coles explainer); dashee87.github.io
  (DC + time-weighting); pena.lt/y (penaltyblog); arXiv:2103.07272 and
  arXiv:2508.20075 (τ piecewise definition).
- Ley, Van de Wiele & Van Eetvelde (2019), via arXiv:2101.10597 — independent &
  bivariate Poisson are the best-performing ML football models.
- Maher (1982) attack/defense Poisson structure — arXiv:1705.09575, arXiv:2508.05891,
  mdpi 2076-3417/14/16/7230.
- Overdispersion from ability mismatch — JRSS-C 74(3):717 (Oxford Academic, 2025).
- Host advantage scales with host strength — Kalwij, Utrecht Univ., *J. Quantitative
  Analysis in Sports* (2025), DOI 10.1515/jqas-2024-0056: ~22pp average host, 9–42pp by
  strength. [corrected: was mis-cited as "Krumer"]
- Per-match HA signature (−0.2 conceded / +0.1 scored) and WC-2026 context, plus
  endorsement of Elo+att/def hybrid — Economics Observatory (June 2026).
- Partial-crowd HA still sizable — Scientific Reports s41598-021-00784-8 (2021).
- Elo K = 60 for World Cup finals — en.wikipedia.org/wiki/World_Football_Elo_Ratings;
  betfair-datascientists.github.io tutorial; Grokipedia (World Football Elo Ratings).
- Historical corpus — Kaggle `martj42/international-football-results-from-1872-to-present`.
- Daily Elo snapshots (backtest-safe) — Kaggle
  `afonsofernandescruz/2026-fifa-world-cup-historical-elo-ratings`.
- Knockout format (no draw; ET two 15-min halves then shootout; 2026 ET from Round of
  32) — ESPN, FOX Sports, AOL World Cup 2026 rules explainers (June 2026).
- Knockout frequencies (~32% reach extra time; in the modern era MOST of those reach
  penalties → ~20–25% reach a shootout, FiveThirtyEight 25.3% over the last five men's
  WCs). Knockouts are LOWER-scoring per 90' than groups (~2.3–2.5 R16-on vs ~2.7); the
  "~2.8 knockout" figure is ET-inflated. [corrected: earlier ~13%/~2.8-higher figures
  were wrong in level and direction — independent review]
- Penalty shootouts ≈ a fair lottery (stronger team no advantage; no order/venue/
  momentum effect), 268 UEFA shootouts 2000–2025 — arXiv:2510.17641 (Dec 2025);
  contested first-mover-advantage literature (Brams, NYU) noted for context.
