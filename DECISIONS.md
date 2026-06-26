# WC26 Hub — Decisions (what we decided and why)

*Last updated 2026-06-13. An ADR-lite log of the load-bearing choices, each with its
rationale and source (commit / file). Newest themes first within each section. When
you make a non-obvious call, add it here.*

## Tournament rules & standings

- **2026 group tiebreakers are Euro-style: head-to-head BEFORE overall GD.** The engine
  had used the pre-2026 order (overall GD/goals before H2H) plus "drawing of lots";
  both were wrong and mis-ranked any points-tied group (and, downstream, the bracket).
  Order is now: points → H2H (points, GD, goals; reapplied to any still-level subset) →
  overall GD → overall goals → fair-play conduct → FIFA World Ranking. Verified against
  the FWC2026 Regulations (May 2025). *`scripts/standings.py` (`_h2h_partition`); CLAUDE.md; commit `da43738`.*
- **FIFA World Ranking is not modelled — residual ties are flagged provisional, never
  resolved by lots.** 2026 replaced "drawing of lots" with the FIFA ranking; we don't
  model it, so a tie surviving fair-play is shown alphabetically and flagged provisional
  rather than silently broken. *`scripts/standings.py`; commit `da43738`.*

## Knockout bracket (the "as it stands" projector)

- **The bracket is a pure, draw-free lookup, and it's GATED.** R32→Final is fixed in
  advance (no post-group draw): group winners/runners-up + the eight best thirds drop
  into a fixed template, with thirds assigned by the FIFA **Annex C** 495-row table
  (`data/annex_c.csv`, generated + integrity-checked by `parse_annex_c.py`). Unstarted
  groups render abstract ("Winner E", "Best 3rd of …"); the eight winner-vs-third ties
  resolve only once all 12 groups have a standing and the cutline isn't provisional.
  *`scripts/bracket.py`; commits `03e460b`, `5f02e5a`.*
- **Winner projection runs through the tree; host HFA applies at a host's own venue.**
  `bracket.feed` propagates the model's projected advancer down the tree (model injected, so
  `bracket.py` stays model-agnostic; real results can override). **Updated 2026-06-25:** now
  that `data/knockout.csv` carries each tie's venue + country, a US/Mexican/Canadian host
  playing in its OWN country gets the standard home bonus (`predict.host_hfa` → `resolve_knockout(hfa_team=…)`),
  applied to the displayed advance Call, the logged advance call, the betting price, AND the
  bracket projection (via a team-set→country map for resolved ties; purely-projected ties stay
  neutral). The shown `%` is the winner's 90'+ET+shootout advance probability. *`scripts/build_site.py` (`load_knockout_resolver`, `load_ko_call`); `scripts/predict.py` (`host_hfa`); commits `2ddd01f`, `1828533`.*

## Knockout stage (the live tournament, R32→Final — `knockout-stage` branch)

- **Knockout fixtures live in a SEPARATE `data/knockout.csv`, not `fixtures.csv`.** `standings`
  locks `fixtures.csv`'s `match_id` to the `A1–L6` form, and CLAUDE.md names it the GROUP
  single source of truth — so the knockout stage (matches 73–104, the FIFA/bracket numbering)
  gets its own contract file. team_a/team_b are a **materialized view of `bracket.py`** (the
  structural authority — no second implementation to drift), filled by `knockout.py --resolve`:
  R32 from locked group positions, later rounds from played feeders. *`scripts/knockout.py`.*
- **A shootout winner is NEVER inferred from the score.** A level knockout result is recorded
  with `decided_by=penalties` and an explicit `winner` side (A|B), which is authoritative for
  advancement (`bracket.feed` + the ledger read `winner`, not the score). `fetch_ko_results.py`
  auto-enters only DECISIVE results and reports level ones for manual `--enter` — the API can't
  say who won a shootout, so we don't guess. (The API also doesn't flag extra time, so a decisive
  auto-entry records `regulation`; revise to `extra_time` by hand if needed.) *`scripts/knockout.py`, `scripts/fetch_ko_results.py`.*
- **Knockout cards are auto-generated AND auto-published (user-chosen).** Sonnet writes the
  tactical sections grounded STRICTLY in the two teams' KB profiles + computed facts + the Wire,
  injuries tagged "(verify before use)", fail-soft to a placeholder (never fabricates) — the same
  grounding contract as `stakes_blurb.py`. Matchups aren't known until a round resolves, so cards
  can't be pre-baked; they generate in the overnight build. *`scripts/knockout_cards.py`.*
- **Knockout betting is a 2-way model-priced ADVANCE market.** "To qualify" (advance, incl. ET +
  a coin-flip shootout) priced from `predict.resolve_knockout`; model-priced ⇒ the stricter **8pp**
  sanity ceiling. Settled penalty-aware from `knockout.csv`'s `winner`. **Assumption (flagged for
  review):** The Odds API quotes 90-minute markets, not "to qualify", so advance odds are entered
  manually (`odds.py enter … advance H,A`) — absent a 2-way line the honest output is "No bet".
  Accountability is a **2-class advance Brier** (0 best / 0.5 coin-flip / 2 worst). *`scripts/odds.py`, `scripts/ledger.py`.*
- **The site/edition flip to the knockout phase by data, not date.** When the group stage
  completes (or the slate date passes the last group date), the masthead/progress/slate switch to
  knockout and a banner elevates the bracket — so the most-trafficked moment (the R32) never lands
  on a frozen "rest day" page. Crons extended June 12 → July 19 (two day-of-month ranges per
  month). *`scripts/build_site.py`, `scripts/build_edition.py`, `.github/workflows/`.*

## Prediction model (`predict.py`)

- **Knockout layer CALIBRATED to the historical frequency bands (2026-06-21; MODEL_IMPROVEMENTS §2.5).**
  Diagnostic: the resolve_knockout layer UNDER-predicted extra time / shootouts (reach-ET 24% vs the
  historical ~32%; reach-shootout 13% vs ~20-25%). Calibrated three resolve_knockout-only knobs to those
  bands (group predict_match byte-identical): **ko_mu_factor=0.93** (a modest goal cut keeping the KO total
  at the historical ~2.4), **ko_rho=−0.20** (a knockout-only Dixon-Coles draw-clustering term — the RIGHT
  lever for caginess, lifting reach-ET at *constant* goals; a μ-cut alone over-cuts goals to 2.13 and still
  under-shoots), **et_caution 0.85→0.50** (reach-shootout band). Result reach-ET 30% / reach-SO 21% / KO
  total 2.41. ko_rho capped at −0.20 for DC stability (residual ~2pp to 32% absorbed by the more-even real
  KO field). Reproducible via `scripts/fit_knockout.py --write`; active in calibration.json. Forward-only /
  projection-only — no knockout games yet, so the §2.5 *validate-narrow* half (advance-prob reliability)
  awaits stage-labeled data. *`scripts/predict.py` (`ko_mu_factor`, `ko_rho`), `scripts/fit_knockout.py`.*
- **Model audit at 28 games (2026-06-20): one real defect — DRAW under-pricing — but no model/weight
  change shipped.** 9-agent audit (5 lanes + 3 adversarial verifiers): live model Brier 0.649 (barely beats
  the 0.667 baseline) but the entire deficit is draws (0/9 hit, 67% of error, p_draw ≈19% vs ≈36% realised);
  on the 16 decisive games it's Brier 0.337, BEATING the market's 0.593. No change cleared the bar — every
  Elo:Futi weight CI straddles 0, ρ still OOS-worse (even with 28 WC games folded in), hosts are the BEST
  subset (no host-bump), and a draw uplift fails leak-free OOS splits (defer to the post-MD3 re-grade, n≥40).
  **Follow-ups shipped (not parameter changes):** (1) a draw-bias CAUTION on win-side 1X2 picks across cards,
  record and the shadow book (`build_site._h2h_win_side` / `_DRAW_AUDIT_CAUTION`) — a disclosure, deliberately
  NOT an invented stricter edge bar; (2) `scripts/corpus_sync.py` folds played WC results into the lever-fit
  corpus (fit_rho/fit_hfa now read through the tournament), and `fit_hfa`'s adopt bar now needs a ≥0.5% OOS
  RMSE gain (the old `1e-4` bar fired on a 0.05% artifact). *memory: model-audit-2026-06-20.*
- **538-style margin-of-victory multiplier TESTED, NOT adopted (2026-06-19).** Asked whether
  FiveThirtyEight's winner-corrected MoV weight — `ln(GD+1) × 2.2/((winner−loser)·0.001+2.2)`,
  which dampens a favourite's blowout and amplifies an underdog's upset — beats our eloratings
  step function (winner-agnostic 1/1.5/1.75/+⅛). `scripts/backtest_mov.py`: online, leak-free
  predict-then-update roll, per-scheme-calibrated Poisson, paired bootstrap, over 11k–20k
  internationals (3 windows) AND 27k club matches. The asymmetry is real (a 3-0 *underdog* upset
  ×2.03 vs a *favourite*'s ×1.54), but it does **not** reliably improve forecasts: every effect
  size is ~0.001 Brier and the occasional "separable" subset flips sign across windows/datasets
  (the pure-dampening variant even HURTS on club data). So it's multiple-testing noise, not signal.
  Keep the simple step multiplier — same validate-don't-assume discipline as ρ (fit, rejected) and
  per-host HFA (built, not adopted). *`scripts/backtest_mov.py`.*
- **Elo auto-rolls forward through WC results each nightly build (2026-06-19).** Previously the
  Elo file was frozen at the pre-tournament 6/11 snapshot while Futi had been refreshed to 6/18 —
  an incoherent post-MD1-Futi / pre-MD1-Elo blend. `scripts/update_elo.py` now rolls the VERIFIED
  6/11 anchor through played games (World Football Elo: K=60, goal-diff multiplier, host +100) into
  a **gitignored** `..._CURRENT.csv` the model prefers (`load_ratings(elo_current=...)`). Forward-
  only/leak-free (played games only, in kickoff order; logged calls graded from the immutable
  ledger), deterministic from committed inputs (anchor + fixtures), regenerated each run in
  daily-build + closing-odds — so it never destabilises the exact-value regression baseline (pinned
  to the anchor via `elo_current=False`). **Notable finding: this does NOT fix the USA-under-Australia
  disagreement.** Both won MD1 and Australia beat higher-rated Türkiye, so Australia gained *more*
  (USA 1726→1780 rk24; AUS 1777→1839 rk21) — the model still has it ~37/27/36 vs the market's
  ~59/23/18. The host-USA gap is **structural (host effect), not staleness**, as the negative CLV on
  an Australia bet confirmed. *`scripts/update_elo.py`; `predict.py` (`ELO_CURRENT`, `elo_current`).*
- **Modest fixed Futi tilt (Elo:Futi = 1 : 1.5) + post-MD1 6/18 futi ratings, forward-only (2026-06-18).**
  A colleague claimed a Futi-only model beats our equal-weight hybrid. Independent backtest
  (`scripts/eval_blend.py`) on 558–999 recent WC-team internationals + the 16 tournament
  games says: Futi is the marginally stronger single source (Elo-only is significantly the
  WORST config; ranking AUC Futi 0.78 vs Elo 0.77), but Futi-**only** is *not* reliably
  better than the hybrid — its tournament "win" is ~3 known-bias games and partly leakage
  (post-MD1 ratings retrodicting results). Elo↔Futi correlate r=0.94, so the Brier optimum
  is a flat plateau (f≈0.5–0.9). Decision: bank the consistent direction with a **fixed
  prior tilt** w_futi 1.0→1.5 (f=0.60) — a judgment call, NOT a data-fit (the
  forecast-combination puzzle says don't tune weights on small samples), and ingest the
  match-driven 6/18 futi.live ratings for **upcoming** predictions only (played games stay
  graded from their immutable logged calls — verified no MD1 leakage). futi.live is an
  Expected-Possession-Value / "goals added" model (ex-American Soccer Analysis), orthogonal
  to results-based Elo. Re-tune only after ~30+ matches clear a significance test. C1
  baseline 53/28/19→51/29/20. *`scripts/predict.py` (`Config.w_futi`, `FUTI_FILE`);
  `scripts/eval_blend.py`; investigation 2026-06-18.*
- **θ recalibrated 290→190; the "Zeileis consensus" was retired as fake.** θ was re-fit
  to the Elo expectancy curve (the old value understated favorites 6–8pp) and locked by a
  regression test. The Zeileis "bookmaker consensus" file was proven synthetic and
  replaced with a verified de-vigged BetMGM outrights file. Constants (mu0=2.6, θ=190,
  alpha=0.20, HFA=60) and equal-weight model+Opta aggregation were user-signed-off.
  *MODEL_IMPROVEMENTS.md §0; `scripts/predict.py` (`Config`); commits `1b27092`, `e49275c`.*
- **Dixon-Coles ρ ships INERT (default 0.0).** The τ low-score correction is in
  `predict_match` but `Config.rho=0.0`, so output is bit-for-bit independent Poisson until
  ρ is fit. The (0,1)→λ_a / (1,0)→λ_b cross-mapping is the canonical DC form (commented so
  it isn't "fixed" into the common swapped-index bug). *`scripts/predict.py`; MODEL_IMPROVEMENTS.md §1.1; commit `de6ba6f`.*
- **ρ was fit, then NOT activated — the data rejected it.** `fit_rho.py` fit ρ ≈ −0.015 on
  10,748 competitive internationals; out-of-sample log-loss/Brier did not improve
  (independent Poisson already slightly over-predicts draws). Per "validate, don't assume,"
  no `calibration.json` is written. *MODEL_IMPROVEMENTS.md §2.2; `data/History/DATA_QUALITY.md`; commit `b1dc594`.*
- **Flat HFA=60 validated; per-host scaling built but NOT adopted.** `fit_hfa.py` measured
  a full-crowd edge ≈ +0.455 goals ≈ ~67 Elo pts, corroborating flat 60 as a sensible
  split-crowd discount. Host-strength scaling (c≈+0.032) is the right direction but
  negligible and moot for the three mid-strength 2026 hosts. `Config.hfa_by_host` structure
  exists (default None ⇒ flat) but nothing ships active. *MODEL_IMPROVEMENTS.md §2.4; commit `352d888`.*
- **Knockout layer is gated; shootout is a flat 0.5 lottery; no regulation-μ bump.** A thin
  ET (c=0.85) + flat-0.5-shootout layer over the same matrix, active only for knockout
  fixtures. Shootout stays strength-neutral (supported by a 268-shootout study); regulation
  total is not raised for knockouts (they're lower-scoring per 90'). *MODEL_IMPROVEMENTS.md §1.2, §2.5; commit `de6ba6f`.*

## Odds & accountability

- **Edges are model-priced derivatives off the Poisson matrix; de-vig is multiplicative.**
  1X2 edge is vs the published consensus; totals/AH/BTTS are model-priced (the overlay is
  W/D/L only). Recorded picks: up to 3 per match, best selection per distinct market, each
  clearing a 5pp recording bar and a 15pp sanity ceiling; otherwise "No bet." Correct-score
  betting is deliberately unsupported (matrix tails not trusted). *CLAUDE.md "Phase 3"; `scripts/odds.py`; commits `ab28127`, `fd1dc9b`.*
- **Brier = SUM of squared errors (range 0–2; 0.667 = coin flip).** Definition reconciled
  to the code (sum form, not mean). *CLAUDE.md "Prediction accountability"; commit `f223320`.*
- **CLV only counts against a true closing line.** A "closing" snapshot counts only if taken
  within ~6h before kickoff, so the bulk June-12 snapshot doesn't yield bogus CLV for future
  picks. Consensus probs are validated (sum 1.0±0.001) before driving a recorded bet.
  *`scripts/odds.py`; commits `88f1461`, `f223320`.*

## Site & UX

- **Today slate is a responsive fixture-board grid (no horizontal scroll).** Replaced the
  fixed-width scroll row with an auto-fill grid (1/2/up-to-3 columns); each card is a
  symmetrical matchup with a vermillion "v" disc that becomes the scoreline once played.
  Designed via the frontend-design skill; user picked the direction. *`scripts/build_site.py` (`render_slate`); `templates/site.css`; commit `35e5041`.*
- **Bracket page is a traditional left-to-right cascade.** Connector lines imply
  progression (no match numbers); downstream slots fill with the projected winner (green +
  advance %). *`templates/bracket.html`; `scripts/build_site.py` (`render_bracket_html`).*

## Ops, testing & honesty

- **The nightly test gate runs on the pristine checkout, pre-mutation.** The 07:00 ET gate
  used to run after data steps mutated the tree, so normal overnight data evolution reddened
  point-in-time tests and blocked the June-13 publish. The gate now precedes all mutation;
  data-shape breakage still fails loudly at the hard Build step. *`.github/workflows/daily-build.yml`; commits `1682d9d`, `5f198cc`.*
- **Hermetic tests build from a frozen snapshot + injected clock.** Site tests build from
  `tests/fixtures/site_snapshot/` (frozen June-12 data) with an injected `now`, fixing both
  brittleness and a wall-clock determinism bug; `build_site` gained injectable data/clock
  params (incl. `knockout_resolver`). One real-data smoke test is kept intentionally.
  *`tests/test_site_content.py`, `tests/test_honesty.py`; commit `b891989`.*
- **Honesty rules are encoded, not conventions.** Once kickoff passes, only the verified
  pre-kickoff logged call is shown/graded (never a live recompute); a kicked-off match with
  no logged call says so; recorded picks are immutable (changes need `--revise`); odds /
  scores / news are never invented (placeholder + flag instead); "The Wire" relays attributed
  news in a distinct box, never house voice. *CLAUDE.md; `scripts/build_site.py`; commits `d137b2a`, `9e352a5`.*
- **Commit + push proactively once confident** (rollback is cheap; user override is rare).
  Use explicit `git add <paths>`, not `git add -A`. *Session memory `commit-when-confident`.*
