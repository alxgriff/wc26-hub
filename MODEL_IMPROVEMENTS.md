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
  negative value, typically between −0.03 and −0.15."
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

---

## Tier 2 — Data-dependent (needs a historical corpus; unlocks 1.1's ρ and honest validation)

> **Data acquisition (applies to 2.1 and 3.2 — read this first).** Both datasets
> live on Kaggle, which requires authentication; the implementing agent's sandbox
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

### 2.1 Acquire a historical international-match corpus

**Dataset:** `martj42 / international-football-results-from-1872-to-present`
(Kaggle) — ~49,000 men's full internationals, `date, home_team, away_team,
home_score, away_score, tournament, city, country, neutral`, using current team
names. This is the canonical free source.
- Store under `data/History/` with provenance + as-of date in
  `data/History/DATA_QUALITY.md`, matching the existing Ratings audit style.
- Normalise team names to the CLAUDE.md canon via the existing `ALIAS` map; extend
  the map and **stop-and-report on any unresolved name** (same contract as the
  ratings join).

### 2.2 `fit_rho.py` — estimate ρ from data

- Fit ρ by **maximising the Dixon-Coles log-likelihood** (or, simpler and robust,
  grid-search ρ ∈ [−0.20, 0] minimising out-of-sample multiclass log-loss / Brier
  on W/D/L) over the corpus, holding the model's λ-generation fixed.
- Optionally restrict to recent internationals and/or major-tournament matches so
  ρ reflects the relevant regime; report ρ and its sensitivity.
- Output: write the fitted ρ into `Config.rho` (or a small `data/calibration.json`
  the loader reads), with the fit date and sample size recorded. Only then does
  the Tier-1 mechanism become active in production.

### 2.3 Backtest harness — validate calibration BEFORE trusting live Brier

- Replay historical matches through the model and measure calibration (reliability
  curve, Brier, log-loss) for W/D/L, Over/Under, BTTS.
- **Guardrail for the betting layer:** the current live ledger is three settled
  picks (−1.13u) — statistically meaningless in either direction. Do **not** tune
  anything to it. Gate any "edge" conclusion on **positive CLV over a meaningful
  sample**, not unit profit. State this in the betting-layer docs.

### 2.4 Host-specific home-field advantage (HFA) that scales with host strength

**Why (well-evidenced).** Current HFA is a single hand-set constant
(`hfa = 60` Elo-pts) applied to any host at home. The literature says HFA is
*not* one-size-fits-all:
- **Host advantage scales with the host's own strength.** Krumer/Utrecht (2025),
  on FIFA World Cups + continental championships: HA for tournament victory is
  ~22pp for an average-Elo host, ranging ~9pp (one SD below average) to ~42pp
  (one SD above). Stronger host ⇒ larger home edge. (This is tournament-victory
  HA, i.e. compounded — use it for *direction and shape*, not as a per-match
  number.)
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
  Validate its team-name canon and as-of date before wiring in.

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

1. **Behaviour-preserving defaults:** with `rho = 0.0` and a flat 60 host table,
   `predict_match` reproduces current outputs exactly. Both are regression-tested.
2. **No invented constants in production:** ρ and any HFA differentiation are
   activated only from a fitted/fit-against-results value, with fit date and
   sample size recorded.
3. **No tuning to the live ledger:** calibration claims are validated on the
   historical backtest (2.3); betting conclusions require positive CLV over a
   meaningful sample.
4. **Keep existing tests green** (θ calibration, canon-resolution stop-and-report)
   and add the ρ tests in 1.1.

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
- Host advantage scales with host strength — Krumer / Utrecht Univ. (2025),
  research-portal.uu.nl: ~22pp average host, 9–42pp by strength.
- Per-match HA signature (−0.2 conceded / +0.1 scored) and WC-2026 context, plus
  endorsement of Elo+att/def hybrid — Economics Observatory (June 2026).
- Partial-crowd HA still sizable — Scientific Reports s41598-021-00784-8 (2021).
- Elo K = 60 for World Cup finals — en.wikipedia.org/wiki/World_Football_Elo_Ratings;
  betfair-datascientists.github.io tutorial; Grokipedia (World Football Elo Ratings).
- Historical corpus — Kaggle `martj42/international-football-results-from-1872-to-present`.
- Daily Elo snapshots (backtest-safe) — Kaggle
  `afonsofernandescruz/2026-fifa-world-cup-historical-elo-ratings`.
