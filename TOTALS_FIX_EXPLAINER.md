# Fixing the Model's "Blowout Blind Spot" — A Plain-English Explainer

*Written for someone who doesn't live in prediction-modeling land. It explains what
our score model does, the flaw we found and fixed on June 14 2026, the trade-offs we
weighed, and why we're confident in the change. No math background assumed — anything
technical is explained as we go.*

---

## The one-paragraph version (TL;DR)

Our model predicts soccer scores. It was very good at saying **who would win**, but it
had a blind spot: it couldn't tell that a lopsided game (a powerhouse vs a minnow)
produces **more total goals** than an even game. It quietly assumed every match had
roughly the same number of goals (~3), so it predicted Germany would *beat* Curaçao but
only by something like 2–0, when reality and the betting market both expected more like
4–5 goals. We confirmed this was a real, systematic flaw — not a one-off — by checking
**10,495 historical international matches**. We fixed it using the standard textbook
recipe for goal models, validated the fix several independent ways (including an
adversarial review by independent AI auditors), confirmed it didn't damage the thing the
model was already good at (predicting winners), and shipped it behind a one-file,
fully-reversible switch. The honest caveat: the *final* proof is how it does on real
World Cup results as they come in.

---

## Part 1 — What the model actually does, and how it "thinks"

The model's job is to look at two teams and predict the match: the chance each side wins
or draws, the likely scoreline, and the probabilities of things like "over 2.5 total
goals." We use those predictions to write each day's preview and to spot betting value.

To make a prediction, the model answers **two separate questions**:

1. **Who's better, and by how much?** This comes from team *ratings* — think of them like
   chess Elo or a power ranking. A big rating gap means a big favorite.
2. **How many goals will be scored?** This is about the *style* of the matchup — two
   attacking teams with leaky defenses produce more goals than two cagey defensive sides.

The model combines these into an **"expected goals" number for each team** (e.g. "Germany
2.8, Curaçao 0.2"). Those expected-goals numbers get fed into a standard statistical
recipe (a *Poisson model* — just a well-established formula for "if a team averages X
goals, how often do they score 0, 1, 2, 3…?") to produce the full set of probabilities:
win/draw/loss, the over/under, the most likely scoreline, and so on.

Here's the key mental picture, because the bug lives right here:

> The model thought of the goals as a **pie**. First it decided **how big the pie is**
> (total goals in the match), then it **sliced the pie** between the two teams based on
> who's stronger. The favorite gets the bigger slice.

That's a reasonable way to do it — *except* for how it decided the size of the pie.

---

## Part 2 — The bug: the model baked every pie the same size

When two teams are evenly matched, the pie-and-slice approach works fine. The problem
showed up in **mismatches** — a top team against a weak one.

Take **Germany vs Curaçao** (a real World Cup group game). Germany is one of the best
teams on earth; Curaçao is a tiny Caribbean nation ranked near the bottom of the field.
What does that game look like? In reality, Germany hammers them — something like 4–0 or
5–1. Lots of goals, because a great attack carves up a weak defense while the minnow
barely threatens.

Our model said: **Germany wins 91% of the time** (correct!) … but with an expected total
of only **~3 goals**, and it literally *could not* predict Germany scoring more than about
2.8. The betting market, meanwhile, was pricing the game at **4.5 total goals**.

Why did the model lowball it? Because of *how it sized the pie*. The recipe it used to set
the total goals **averaged the two teams' attacking and defensive qualities together**.
In a mismatch, Germany's enormous attacking edge got *averaged against* Curaçao's near-zero
attacking threat — and the two roughly **cancelled out**, leaving the total stuck near its
"average game" value of ~3. The model captured *who* would score (Germany gets almost the
whole pie) but not that **the pie itself should be much bigger** in a blowout.

The tell-tale fingerprint: across all 144 tournament games, the most goals the model could
ever predict for a single team was **2.84**. It was *structurally incapable* of forecasting
a team scoring 4 — which is exactly the thing that happens when a giant plays a minnow.

---

## Part 3 — Why this hid for so long (it's not negligence)

Two honest reasons.

**1. The model's "report card" never tested it.** We grade the model's accuracy mainly on
*who wins* (a score called the Brier score — basically, "how close were your win/draw/loss
probabilities to what actually happened"). Here's the subtle part: **predicting the winner
barely depends on the total goals.** Germany is 91% to beat Curaçao whether the final is
2–0 or 5–0. So the model could be *excellent* on its report card while being *quietly wrong*
about total goals — the report card simply wasn't looking at that. Totals only became a
graded, money-on-the-line output recently, when we added the betting overlay.

**2. It had actually been considered — and deliberately left alone.** Our model-improvement
notes show a prior review looked at exactly this ("should total goals depend on the
mismatch?") and decided to **leave it as-is**, judging the model "not blind to blowouts"
based on eyeballing a couple of examples. What changed our mind wasn't a new opinion — it
was that the betting overlay put a hard **number** on the disagreement (model 3.0 vs market
4.5), turning a shrug into a measurable gap. So this was a known, reasonable judgment call
that new evidence overturned — not something anyone missed through carelessness.

---

## Part 4 — Proving it was real, not just one weird game

One mispriced game (Germany–Curaçao) isn't proof of a systematic flaw. The market could be
wrong; the model could be right. So we ran a **backtest** — we replayed history and checked
the model's assumption against reality.

We took **10,495 real international matches** (competitive games, 2010 onward), sorted them
by how big a favorite each had, and asked one simple question: **as games get more lopsided,
do they actually produce more goals?**

The answer was unambiguous:

| Matchup type | Real average total goals | What our (old) model predicted |
|---|---|---|
| Even games | 2.4 | 2.6 |
| Lopsided (heavy favorite) | **3.7** | **2.7** |

In real life, the total **climbs by +1.3 goals** as you go from even matchups to blowouts.
Our model's total was essentially **flat** (+0.05). In the most lopsided games, the favorite
*actually* scores 3.4 goals on average; our model capped it at 2.5. Real blowouts go over
3.5 total goals **48%** of the time; the model said **28%**.

That settled it. The flaw was **real and systematic**, the market's higher blowout totals
were **right**, and the bias leaned the *same direction every time* — a fingerprint of a
model error, not 10,000 separate coincidences.

One nuance we were careful about: the data also showed blowouts are a bit more *unpredictable*
(occasional 6–0s and 7–1s) — statisticians call that "overdispersion." That's a real but
**secondary** effect. The **main** problem was the average being too low, not the spread
being too narrow. We fixed the main problem; the spread is a noted follow-up.

---

## Part 5 — The fix: compute each team's goals directly

The fix is to stop "sizing one pie and slicing it," and instead do what the **standard
textbook goal models** (Maher 1982, Dixon–Coles 1997, and modern descendants) have always
done:

> Compute **each team's** expected goals **directly**, from *its own attacking strength*
> vs *the opponent's defensive weakness.*

So Germany's goals come from "Germany's attack vs Curaçao's leaky defense" (→ a big number),
and Curaçao's goals come from "Curaçao's weak attack vs Germany's strong defense" (→ a tiny
number). Add them up and the **total naturally gets bigger in a mismatch** — without us
hand-forcing it. The lopsidedness creates the high total as a *consequence*, which is exactly
how real soccer works.

Crucially, we kept the part the model was already great at. The model has two strength
signals: a broad "power ranking" (great at predicting winners) and an attack/defense profile
(great at goal volume). The fix uses the **attack/defense profile to size the goals** but
keeps the **power ranking to decide the split** (who's favored). Best of both — we fixed the
totals **without touching the winner-prediction engine** that earns the model's good report
card.

**What we deliberately did *not* do** (this matters):
- We did **not** bolt on an ad-hoc "add more goals when it's a mismatch" fudge factor. A prior
  audit had already considered and **rejected** that as an unprincipled hack. The textbook
  Maher recipe gets the higher total the *legitimate* way.
- We did **not** reach for "Dixon–Coles" (a popular tweak someone might suggest). We checked:
  it only adjusts how often games end 0–0 or 1–1 — it does **nothing** to the average total.
  Wrong tool for this job.

---

## Part 6 — The trade-offs (being straight about them)

No change is free. Here's what we weighed.

**1. We changed numbers that had been reviewed and "locked."** The model's settings had been
through multiple audits. Changing core constants on a trusted, much-reviewed system deserves
real caution — and the project owner was rightly reticent. We treated that seriously: the bar
for changing them was *evidence*, not opinion.

**2. The winner-predictions shift a little.** Making the totals right slightly changes some
win/draw/loss numbers (more goals → slightly fewer draws, etc.). We measured this carefully.
The net effect on winner-accuracy is **statistically zero** — and, encouragingly, it's
actually *better* in the big mismatches (where it improved the most) while being a touch worse
in moderate games. It washes out overall. The thing the model was good at stayed good.

**3. There's a limit we can't fully escape.** Ideally we'd test the *exact* live settings on
held-out historical games. But the model uses a single, current snapshot of team ratings — so
there's no clean way to "rewind" it to predict a 2018 match with 2018-appropriate ratings.
We got as close as possible with stand-in ratings, and we're transparent that the **final,
true test is live World Cup results.** This isn't a gap in our work; it's a fundamental
property of the data, and we're naming it honestly rather than papering over it.

**4. For betting, the fix makes edges *honest*, not *zero*.** A natural expectation is "fix the
model and the betting flags disappear." They didn't — and that's correct. Before, the model's
totals were biased the same direction on a third of the board, producing fake "value" we
correctly refused to bet. After the fix, the model agrees with the market on the obvious games
(Germany–Curaçao's fake edge is gone) but still *disagrees* on others — and now those
disagreements are **balanced and trustworthy** rather than a one-way artifact. Whether any are
genuinely profitable is a separate question that only real betting results (closing-line value)
can answer.

---

## Part 7 — Why we're confident

Confidence here doesn't come from one clever test. It comes from **layers** of independent
evidence all pointing the same way, plus engineering guardrails:

**The evidence stack:**
- **History agrees with the diagnosis** — 10,495 real games show the totals-rise-with-mismatch
  pattern the old model missed.
- **The fix matches reality** — after recalibrating, the model's totals track the real curve
  almost exactly (the worst gap shrank from −1.4 goals to about −0.02).
- **It doesn't harm winner-prediction** — confirmed both on the data we fit to *and* on a
  separate, **held-out** slice of years the model never saw during fitting.
- **It does sane things on the actual fixtures** — every World Cup game's new total looks
  reasonable (the biggest is 4.4; no absurd 6-goal predictions), lifting blowouts and gently
  trimming even games, exactly as intended.
- **Independent adversarial review** — we had a panel of independent AI auditors specifically
  *try to break* the validation: hunt for data leakage, statistical sleight-of-hand, and
  reasons not to trust it. They confirmed it was leak-free and correct, flagged a few
  honesty/robustness nitpicks (which we fixed), and pushed back where we'd overstated — which
  is exactly what a good review should do.

**The guardrails (so this is safe even if we're wrong):**
- **Fit, don't guess** — the new settings were *computed from data*, never hand-picked to look
  good. This is a long-standing project rule.
- **Shipped "off" first** — the new machinery was added in a dormant state that produces
  byte-for-byte identical results until deliberately switched on, so it could be reviewed
  without risk.
- **Fully reversible** — activation is a single settings file. Deleting that one file instantly
  restores the exact previous model. If live results disappoint, rollback is trivial.
- **Locked tests** — automated tests pin the model's behavior, so any *unintended* change
  screams immediately. (One test legitimately needed updating, with a documented note.)

**The honest bottom line:** the fix corrects a real, measured flaw with the principled,
textbook method; it's been validated every way the data feasibly allows; it leaves the model's
strengths intact; and it's reversible. The one thing no lab test can provide — performance on
real, unplayed World Cup games — is the scoreboard we'll watch next. If the blowouts land where
the fix predicts, the case is closed. If they don't, undoing it is one file away.

---

## Mini-glossary

- **Rating** — a number for how good a team is (like chess Elo). Bigger gap = bigger favorite.
- **Expected goals** — the average number of goals we'd predict a team to score in this match.
- **Poisson model** — a standard formula turning "averages X goals" into "how often they score
  0, 1, 2, 3…". The industry-standard base for soccer score models.
- **Total / the pie** — the combined goals expected in a match (both teams).
- **The split / supremacy** — how the goals divide between the favorite and the underdog.
- **Maher form** — the textbook recipe of computing each team's goals from *its attack vs the
  opponent's defense*; the heart of our fix.
- **Brier score** — the model's "report card" for win/draw/loss accuracy.
- **Backtest** — replaying historical games to check the model against what really happened.
- **Holdout / out-of-sample** — testing on data the model was *not* tuned on, to make sure it
  generalizes rather than memorizes.
- **Overdispersion** — the (secondary) fact that blowouts are also more *erratic*, not just
  higher-scoring. A noted follow-up, not part of this fix.
- **Closing-line value (CLV)** — a betting yardstick: did our pick's price move our way by game
  time? The real-world test of whether an "edge" was genuine.

*Technical record: see `MODEL_IMPROVEMENTS.md` §3.1 and the scripts `backtest_totals.py`
(the history check), `fit_maher.py` (the fit + validation gates), and `predict.py` (the model
itself, where `Config.maher_w` turns the fix on). The live settings live in
`data/calibration.json`.*
