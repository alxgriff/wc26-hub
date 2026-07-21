# WC26 Hub — Final Retrospective

**Written 2026-07-20, the day after the Final. All numbers below were computed from the repo's ledgers (`predictions_log.csv`, `picks_log.csv`, `shadow_picks_log.csv`, `ko_predictions_log.csv`, `edge_drd_log.csv`, `odds_log.csv`, `knockout.csv`, `fixtures.csv`) and independently re-derived by an adversarial verification pass. Where the verifier corrected a number, the corrected value is used and noted.**

## The tournament in one paragraph

Spain are champions — 1-0 over Argentina after extra time in match 104, capping a five-tie knockout run the advance model priced at a chained 0.172 (about 1 in 5.8), never rating Spain below 0.588 in any round. The model's final report card: 69 of 72 group games graded at cumulative Brier 0.509 / RPS 0.152 (coin-flip baseline 0.667), 43/69 (62.3%) correct calls; 26/32 (81.3%) on knockout advance calls at a 2-class Brier of 0.278 (coin-flip 0.5); and a real-money book that finished +9.62u on 133 flat-stake picks. Two honest asterisks travel with those headlines. The known draw defect held to the very end — mean p_draw 0.192 against a 27.5% realized draw rate, with the model never once making draw its modal call — and it is the entire reason the closing market beat the model overall (0.482 vs 0.509). And the +9.62u is variance, not captured edge: at de-vigged closing prices the book's expected value was **-3.34u**, so roughly +14.81u of the outcome was run-good.

## Group-stage calibration (the 3-way ledger)

The ledger graded 69 of 72 group games — the three holes are permanent and structural, not grading bugs: A1 (Mexico 2-0 South Africa) and A2 (South Korea 2-1 Czechia) kicked off before the ledger's first log on June 12, and B1 (Canada 1-1 Bosnia and Herzegovina) is the logging gap known since the June 13 audit. The ledger is immutable post-kickoff, so 69 is the definitive denominator. Final numbers: cumulative Brier **0.509**, RPS **0.152**, 43/69 correct — matching the published edition anchor, unchanged since June 28.

**The draw defect is the story.** Mean p_draw was 0.192 against a realized draw rate of 0.275 (19/69), a +8.3pp under-pricing that was identical at the July 4 re-grade because the sample was already final. Versus the June 20 checkpoint the gap "halved" only because the realized draw rate regressed from 36.0% (9 of the 25 games graded through June 18 — note: 28 games were *played* by then, but only 25 *graded*) down to 27.5%; the model itself barely moved (0.194 → 0.192). In zero of 69 games was draw the modal call — the highest p_draw ever logged was 0.314 — and the draw probabilities carried almost no discrimination (mean 0.202 on games that drew vs 0.188 on games that didn't). The three worst calls of the tournament are all the same failure mode, a ~90% favorite held to 0-0: H1 Spain–Cape Verde (p_home 0.941, Brier 1.785), E4 Ecuador–Curaçao (0.887, Brier 1.604), L3 England–Ghana (0.883, Brier 1.594). The eventual champion being blanked by Cape Verde was the single most expensive prediction of the tournament. The best call was C4 Brazil 3-0 Haiti, p = (0.958, 0.035, 0.007), Brier 0.003.

Reliability by predicted-favorite probability:

| Bucket | n | Mean p | Hit rate |
|---|---|---|---|
| <0.40 | 7 | 0.380 | 3/7 = 0.429 |
| 0.40–0.50 | 8 | 0.464 | 3/8 = 0.375 |
| 0.50–0.60 | 8 | 0.561 | 4/8 = 0.500 |
| 0.60–0.70 | 17 | 0.660 | 11/17 = 0.647 |
| 0.70+ | 29 | 0.827 | 22/29 = 0.759 |
| 0.60+ combined | 46 | 0.766 | 33/46 = 0.717 |

The draw leak explains the strong-favorite shortfall specifically, not every bucket: in the 0.60–0.70 bucket misses were 5 draws vs 1 opposite-side win, and at 0.70+ they were 7 draws vs 0 flips — but at 0.40–0.50 it was 2 draws vs 3 flips and at 0.50–0.60, 1 draw vs 3 flips (those sub-0.6 buckets are n=7–8 and noise-dominated anyway). Across all 26 misses: 19 draws, 7 flips.

Slot-level accounting (verifier-corrected interpretation): the team_a-win slot was nearly perfectly calibrated (mean 0.452 vs 0.464 realized, *under*-priced by 1.2pp) and the team_b-win slot was over-priced by 9.5pp (0.356 vs 0.261). The team_b excess funded **both** the 8.3pp draw shortfall and the 1.2pp team_a shortfall; team_a funded nothing.

Two flags. The +8.3pp draw gap carried p≈0.06 at the July 4 re-grade — it never quite reached conventional significance, but its persistence across every checkpoint and its concentration in the worst-call tail make it the model's one confirmed systematic defect. And a claim in the analysis that the three ungraded games "all had lopsided-favorite profiles" is unverifiable (no probabilities were ever logged for them); what the data does say is that across all 72 played games there were 20 draws (27.8%) — ungraded B1 was itself a draw — so the exclusions marginally understate the realized draw rate and, if anything, reinforce the finding.

## Model vs the closing market

Benchmarked by exact match_id join of the 69 consensus rows against the de-vigged closing h2h in `odds_log.csv` (multiplicative de-vig; the market here is the DraftKings-preferred/US-book mix, not Pinnacle):

| Split | n | Model Brier | Market Brier | Δ (model−market) | 95% CI |
|---|---|---|---|---|---|
| Overall | 69 | 0.509 | 0.482 | +0.027 | [−0.007, +0.064] |
| Decisive | 50 | 0.277 | 0.289 | −0.012 | [−0.049, +0.029] |
| Draws | 19 | 1.119 | 0.988 | +0.131 | [+0.078, +0.187] |

RPS tells the same story: 0.152 vs 0.144 overall, 0.119 vs 0.121 decisive, 0.239 vs 0.204 draws. Top-pick hit rate: model 43/69 (62.3%), market 47/69 (68.1%). Mean p_draw: model 0.192, market 0.217, realized 0.275 — the market under-priced draws too, just less.

The market beat the model overall, and the entire margin came from draws — the only split whose CI excludes zero (model better on just 3 of 19 drawn games). On decisive games the June 20 audit's "sharp on decisive games" claim survived in direction: the decisive Brier improved from 0.337 (June 18 cutoff) to 0.277 at n=50, still below the market's 0.289, with the model the better forecaster on 33/50 — but the margin is inside the CI, so the honest full-tournament framing is: *competitive with the closing market on decisive games, clearly worse on draws, beaten overall because of them.* Biggest single-game wins over the market: L6 Croatia–Ghana 2-1 (+0.297 Brier delta), E6 Ecuador–Germany 2-1 (+0.190), A5 Czechia–Mexico 0-3 (+0.182). Biggest losses: J6 Algeria–Austria 3-3 (−0.438), E2 Côte d'Ivoire–Ecuador 1-0 (−0.415), L2 Ghana–Panama 1-0 (−0.408). (Market-definition sensitivity is negligible: the draws Brier is 0.987–0.988 depending on best-of-books vs DK-preferred closing triple.)

This benchmark is group-stage only by construction; knockout accountability is the separate 2-class ledger below.

## The betting book

Final record: **133 settled picks, 70W-55L-8P** (68 won + 2 half-won quarter-line spreads), **+9.62u** at flat 1u stakes, mean CLV **+0.16pp** over the 117 picks with a true closing line.

The luck question resolves against skill. Reconstructing each pick's closing fair probability, the 117 CLV-tracked picks had an expected value of **−3.34u** at de-vigged closing prices — versus +11.47u actually realized on them — so about **+14.81u of the outcome is variance**. At snapshot de-vigged fair value, all 133 picks price out to −4.37u. A mean CLV of +0.16pp was positive but nowhere near enough to clear the vig. The profit is real money; it is not evidence of edge.

By market:

| Market | Picks | Record | Units | CLV (n) |
|---|---|---|---|---|
| h2h (1X2 consensus overlay) | 49 | 25W-24L | −1.67u | +0.25pp (46) |
| totals | 48 | 25W-19L-4P | +4.70u | −0.23pp (39) |
| spreads | 36 | 20W-12L-4P | +6.59u | +0.52pp (32) |
| btts | 0 | — | — | — |

Every unit of profit came from the model-priced score-matrix markets: totals + spreads together went 45W-31L-8P for **+11.29u** (CLV +0.11pp), while the 1X2 consensus overlay lost −1.67u despite decent CLV. One correction from verification: within the group phase the per-market CLV ranking was spreads +0.77pp > h2h +0.25pp > totals −0.24pp — h2h was second of three, not the best path as the analysis first claimed.

By phase: group 108 picks, 58W-47L-3P, +6.50u, CLV +0.24pp; knockout 25 picks (M74–M103), 12W-8L-5P, +3.12u, CLV −0.18pp. All 49 h2h picks were group-stage — the knockout book was exclusively 90-minute totals (14, +1.79u) and spreads (11, +1.33u), consistent with the derived advance read staying display-only.

By logged edge: [4,5)pp — 37 picks, +2.36u, CLV +0.25pp. [5,8)pp — the workhorse, 72 picks, +11.80u, CLV +0.24pp. [8,15)pp — the only losing band: 24 picks, 12W-12L, **−4.54u**, CLV −0.17pp, of which 20 were h2h running −4.66u; the 4 non-h2h all predate the June 14 ceiling split. No pick reached 15pp (recorded span 4.0–14.6pp). The largest claimed edges were the model's miscalibration, not market error.

Color: the biggest win was D2, Australia to beat Türkiye at 5.40 (betonlineag, 5.8pp edge, taken June 13) — +4.40u; the next two were also Group D longshot moneylines (D5 +3.10u, D4 +2.85u). The highest-conviction loss was D6 h2h away at 4.20 with a 14.6pp logged edge (Paraguay 0-0 Australia).

## The sanity ceiling (shadow book)

The shadow ledger — every ceiling-suppressed "risky" call, paper-only — settled 82 picks: 36W-37L (9 pushes), **−2.88u**, mean CLV +0.13pp (n=68). Every shadow row is genuinely ceiling-suppressed (min h2h edge 15.5pp, min model-priced edge 8.1pp).

The two ceilings graded very differently:

- **The 15pp 1X2 ceiling earned its keep decisively.** The 10 suppressed consensus-checked calls went 3W-7L for −4.58u at a mean CLV of exactly +0.00pp (n=9) — the market never moved toward these severe-disagreement calls. This one subset accounts for more than the shadow book's entire loss. Keep it unchanged.
- **The 8pp model-priced ceiling was one notch too tight.** The suppressed 8–12pp band was actually good: 34 settled, 18W-13L (58.1% of decided), **+4.48u**, CLV +0.22pp (n=29) — better CLV than anything in the kept book. Decay sets in around 12pp: the 12pp+ suppressed picks went 15W-17L (46.9%), −2.78u, and the 15pp+ tail 7W-11L, −4.19u. Win rate and units decay monotonically with edge size; band-level CLV does not (the 15pp+ tail shows +0.51pp on n=15 — noise, not evidence the tail was +EV).

**Counterfactual — verifier-corrected.** The analysis's "no-ceiling book = +9.62 − 2.88 = +6.74u" double-counts: 29 of the 82 shadow rows share a (match, market) key with a recorded real pick, and the recording protocol allows one selection per distinct market, so a no-ceiling book replaces those real picks (which earned +2.08u) rather than adding to them. The corrected counterfactual is **+4.66u** (replacement semantics; +4.33u under an earliest-snapshot variant), making the ceiling worth **+4.96u** in realized units (+5.29u variant) — roughly 2.1–2.4u *more* than the analysis first credited. In CLV terms the ceiling was roughly neutral (+0.13pp suppressed vs +0.16pp kept): it did not systematically screen out bad prices; its value was concentrated in the 1X2 and 15pp+ subsets.

Scope caveat (verifier): the shadow log contains **zero knockout rows of any kind** — all 82 are group-stage — so the 8–12pp evidence behind the loosen-to-12pp recommendation is group-stage-only. Either no knockout pick ever breached the ceiling or knockout suppressions were never shadow-logged; the ledger cannot say which.

Verdict: **split**. Keep the 15pp 1X2 ceiling as-is; loosen the model-priced ceiling from 8pp to 12pp for the next cycle, run on the same protocol as the June 14 bar experiment — re-grade when the 8–12pp band reaches ~50 CLV observations (currently 29), revert if band CLV goes negative.

## The DRD edge tracker

The Deserved-Result-Divergence tracker — the project's first orthogonal-input edge experiment — logged 11 picks under its single tag, all group-stage 3-way h2h, June 18–25 (8 of the 11 in one June 18 batch, so not fully independent). On its own metric (de-vigged consensus close vs snapshot): mean CLV **+2.43pp**, sd 3.43, t=2.35, nominal p≈0.041, with 9/11 beating the close. On paper it went 7/11 for +0.91u.

The headline needs its haircut stated plainly: the +2.43pp uses early snapshots (as early as June 18 for games played June 24–27) against a different CLV basis than the book's. The apples-to-apples check — all 11 DRD selections were also real recorded picks — shows the tagged subset at **+0.88pp** book-basis CLV vs +0.09pp for the 106 untagged observations: delta **+0.80pp**, Welch t=1.80, p≈0.091. Direction confirms the hypothesis; n=11 does not clear significance, and the nominal p≈0.04 carries no multiple-comparison correction across the project's many graded experiments.

One characterization the verifier struck: the two negative-CLV picks (Qatar +650, Australia +255) were *not* "the two longest-priced underdogs" — Australia was the shortest-priced of the four underdog picks, and the two intermediate-priced underdogs both ran positive CLV. There is no monotone price pattern; the "cleaner on favorites" hint is unsupported.

The tracker went dormant after June 25 and was never extended to the 32 knockout ties — the main reason n stayed at 11. The other planned tags (heat→totals, MD3 motivation) were never implemented: no code, no rows, nothing to grade. Verdict: carry it into a future cycle as a pre-registered experiment — target ~50 observations, extend past the group stage, and fix the CLV metric to the book-implied basis so the grade is uncontaminated by snapshot timing — not as a bankroll-backed conviction.

## The knockout advance model

The 2-class advance ledger graded all 32 knockout ties (no missed calls, all timestamps strictly pre-kickoff): **26/32 correct (81.3%)**, cumulative 2-class Brier **0.278** against a 0.5 coin-flip baseline.

| Round | Record | Brier |
|---|---|---|
| R32 | 13/16 | 0.254 |
| R16 | 6/8 | 0.346 |
| QF | 4/4 | 0.133 |
| SF | 2/2 | 0.293 |
| Third place | 0/1 | 0.610 |
| Final | 1/1 | 0.340 |

The extra-time layer graded cleanly and the shootout layer got unlucky. Seven ties went past 90 minutes: on the 3 decided in extra time (including the Final) the model went 3/3 at Brier 0.187 — better than its overall average — but on the 4 penalty shootouts it went **0/4** at Brier 0.802. That 0-for-4 has probability 1/16 (6.25%) under the model's own coin-flip shootout assumption — consistent with variance, not a pricing defect (the assumption washes by construction: conditional on reaching a shootout, the model's stake was 50/50). Those 4 ties contributed 3.21 of the 8.90 total Brier sum (36%) from 12.5% of the ties; flipping two of the four shootouts would put the cumulative Brier in the 0.228–0.265 range depending on the pair. On the 25 regulation-decided ties the model was excellent: 23/25, Brier 0.205.

The six misses, with the called side's p_advance: M74 Germany 0.755 (pens), M75 Netherlands 0.551 (pens), M88 Australia 0.557 (pens), M91 Brazil 0.755 (regulation, to Norway — the only clean 90-minute upset of a strong favorite), M96 Colombia 0.649 (pens), M103 France 0.552 (regulation). Three sit within 0.007 of the coin-flip band; the two genuine favorites-lost were Germany and Brazil at 0.755.

Calibration on the called side ran slightly timid: 0.5–0.6 bucket n=6, mean 0.561 vs 0.500 realized; 0.6–0.7 n=8, 0.672 vs 0.875; 0.7+ n=18, 0.804 vs 0.889. Directionally under-confident on its stronger calls, but no bucket gap is significant at n=32.

Spain's title path, chaining the logged pre-kickoff p_advance each round (0.864 × 0.684 × 0.776 × 0.639 × 0.588, taking team_b sides where Spain was listed B): **0.172, about 1 in 5.8**. The model rated Spain a live favorite in every tie. Note this is not a pre-tournament path probability — the per-round inputs incorporated mid-tournament rating refreshes and condition on the opponents actually drawn.

## What carries forward

1. **Draw pricing is the defect; everything else is calibrated.** One number to remember: the model made draw its modal call in 0 of 69 games against a 27.5% realized rate, and the +8.3pp gap (p≈0.06 — persistent at every checkpoint, never quite conventionally significant) is the entire reason the closing market won overall. The win-side slots were fine — team_a within 1.2pp — and the missing draw mass was funded from the team_b slot. The June 20 / July 4 hold on activating ρ was defensible at the time (the fitted ρ could not reach the advance calls that were then the live product), but the next cycle should not start with a model structurally incapable of calling a draw.

2. **CLV, not units, is the yardstick — and by it the book had no edge.** +9.62u realized against −3.34u of closing-price EV is +14.81u of variance. Mean CLV of +0.16pp cannot beat the vig. The strength model is not an edge against a sharp market (the backtest said so before the tournament did); future cycles should spend effort on orthogonal inputs and CLV capture, not on re-tuning W/D/L probabilities that already sit within noise of the close on decisive games.

3. **The ceiling verdict is a split, and it's now quantified.** Keep the 15pp consensus-checked 1X2 ceiling exactly as-is (suppressed set: 3W-7L, −4.58u, +0.00pp CLV — no evidence of any edge). Loosen the model-priced ceiling 8pp → 12pp (suppressed 8–12pp band: +4.48u, +0.22pp CLV; decay starts at 12pp+, 46.9% win rate). Corrected for market-slot displacement, the ceiling as-run was worth about +4.96u — nearly double the naive additive estimate. Run the 12pp change as a measured experiment: re-grade at ~50 CLV observations in the band, revert on negative CLV. Caveat: all suppressed-set evidence is group-stage only.

4. **The 4pp bar is validated — make it permanent.** Final grade at tournament end: the [4,5)pp band produced +2.36u over 37 settled picks with mean CLV +0.25pp over 32 closing-line observations, matching the ≥5pp book's CLV. The June 14 lowering met its pre-registered condition (CLV ≥ 0 at ~50 observations was the target; the band closed at 32 obs, positive on both measures — near enough the target that a fresh cycle should confirm at full n, but the working answer is: keep 4pp).

5. **DRD is the one live edge candidate — carry it as a pre-registered experiment.** 9/11 beat the close; +2.43pp on its own (timing-contaminated) metric, +0.80pp like-for-like over untagged picks (p≈0.09). In a book that ran +0.16pp overall, that is what an edge looks like at small n — and n stayed small because the tracker was left dormant for the entire knockout stage. Next cycle: same tag, ~50-observation target, book-implied CLV basis from day one, coverage through the final — and actually build the other tags.