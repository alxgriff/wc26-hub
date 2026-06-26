"""Tests for fit_hybrid.py (the XGBoost overlay trainer).

Two skip gates:
  * xgboost / numpy may not be installed (the deps are added only for this layer)
  * the corpus is gitignored (≥3 MB CC0 file; fetched via curl per DATA_QUALITY.md)

The pure-stdlib helpers (rolling Elo, feature vector synthesis, RPS / Brier /
logloss math, time-decay weights) are tested unconditionally — they're the parts
where bugs cause silent miscalibration regardless of whether the booster trains.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import math
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import fit_hybrid as fh   # noqa: E402
import hybrid as hy       # noqa: E402
import predict as pr      # noqa: E402


def _has_xgboost() -> bool:
    try:
        import xgboost  # noqa: F401
        import numpy    # noqa: F401
        return True
    except ImportError:
        return False


def _mk(home, away, hs, as_, *, date_="2020-01-01",
        tournament="FIFA World Cup", neutral=False):
    """Synthetic curated-style row (post-load_curated shape)."""
    return {"home": home, "away": away, "hs": hs, "as": as_,
            "neutral": neutral, "date": date_, "tournament": tournament}


# ---------------------------------------------------------------- pure stdlib tests

class GoalDiffMultiplierTests(unittest.TestCase):
    def test_one_goal_or_less(self):
        self.assertEqual(fh.goal_diff_multiplier(0), 1.0)
        self.assertEqual(fh.goal_diff_multiplier(1), 1.0)
        self.assertEqual(fh.goal_diff_multiplier(-1), 1.0)

    def test_two_goals(self):
        self.assertEqual(fh.goal_diff_multiplier(2), 1.5)
        self.assertEqual(fh.goal_diff_multiplier(-2), 1.5)

    def test_three_plus_grows(self):
        self.assertEqual(fh.goal_diff_multiplier(3), (11 + 3) / 8.0)
        self.assertGreater(fh.goal_diff_multiplier(5), fh.goal_diff_multiplier(3))


class TierTests(unittest.TestCase):
    def test_major_recognised(self):
        self.assertEqual(fh.tier_of("FIFA World Cup"), "major")
        self.assertEqual(fh.tier_of("UEFA Euro"), "major")
        self.assertEqual(fh.tier_of("Copa América"), "major")

    def test_qualifier_recognised(self):
        self.assertEqual(fh.tier_of("FIFA World Cup qualification"), "qualifier")
        self.assertEqual(fh.tier_of("UEFA Euro qualification"), "qualifier")

    def test_other_competitive_is_minor(self):
        self.assertEqual(fh.tier_of("CONCACAF Gold Cup"), "minor")
        self.assertEqual(fh.tier_of(""), "minor")

    def test_major_K_above_qualifier_above_minor(self):
        self.assertGreater(fh.K_BY_TIER["major"], fh.K_BY_TIER["qualifier"])
        self.assertGreater(fh.K_BY_TIER["qualifier"], fh.K_BY_TIER["minor"])


class RollingEloTests(unittest.TestCase):
    """Most load-bearing logic in the trainer — leakage here ⇒ silent overfit."""

    def test_no_leakage_pre_update_snapshot(self):
        """Match N's feature row must use Elo updated through match N-1, not N."""
        ms = [
            _mk("A", "B", 2, 0, date_="2020-01-01"),
            _mk("A", "B", 1, 0, date_="2020-02-01"),
            _mk("A", "B", 0, 1, date_="2020-03-01"),
        ]
        rows = fh.roll_elo(ms)
        # match 1's pre-Elo is the cold start (no updates have happened yet)
        self.assertAlmostEqual(rows[0]["elo_home_pre"], fh.ELO_INITIAL)
        self.assertAlmostEqual(rows[0]["elo_away_pre"], fh.ELO_INITIAL)
        # match 2's pre-Elo reflects exactly the one update from match 1
        self.assertGreater(rows[1]["elo_home_pre"], fh.ELO_INITIAL)
        self.assertLess(rows[1]["elo_away_pre"], fh.ELO_INITIAL)
        # match 2's pre-Elo == match 1's outcome, NOT match 2's
        # If match 2 had leaked, A's pre-Elo for match 2 would have absorbed match 2's win too
        elo_a_after_one_win = rows[1]["elo_home_pre"]
        elo_a_after_two_wins_would_be = elo_a_after_one_win + (
            fh.K_BY_TIER["major"] * 1.0 * (1.0 - fh.expected_home(elo_a_after_one_win,
                                                                  rows[1]["elo_away_pre"],
                                                                  False)))
        self.assertLess(rows[1]["elo_home_pre"], elo_a_after_two_wins_would_be - 1.0)

    def test_winner_elo_strictly_rises_loser_strictly_falls(self):
        ms = [_mk("Winner", "Loser", 3, 0)]
        rows = fh.roll_elo(ms)
        # Replay to see the second-match snapshot via a follow-up match
        ms2 = ms + [_mk("Winner", "Loser", 0, 0, date_="2020-02-01")]
        rows2 = fh.roll_elo(ms2)
        self.assertGreater(rows2[1]["elo_home_pre"], fh.ELO_INITIAL)
        self.assertLess(rows2[1]["elo_away_pre"], fh.ELO_INITIAL)

    def test_draw_between_equals_leaves_elo_unchanged(self):
        ms = [_mk("A", "B", 1, 1, neutral=True, date_="2020-01-01"),
              _mk("A", "B", 0, 0, neutral=True, date_="2020-02-01")]
        rows = fh.roll_elo(ms)
        self.assertAlmostEqual(rows[1]["elo_home_pre"], fh.ELO_INITIAL, places=6)
        self.assertAlmostEqual(rows[1]["elo_away_pre"], fh.ELO_INITIAL, places=6)

    def test_zero_sum_update(self):
        """Elo is zero-sum — A's gain equals B's loss."""
        ms = [_mk("A", "B", 2, 0, date_="2020-01-01"),
              _mk("A", "B", 0, 0, date_="2020-02-01")]
        rows = fh.roll_elo(ms)
        delta_a = rows[1]["elo_home_pre"] - fh.ELO_INITIAL
        delta_b = rows[1]["elo_away_pre"] - fh.ELO_INITIAL
        self.assertAlmostEqual(delta_a, -delta_b, places=9)

    def test_outcome_label_convention(self):
        rows = fh.roll_elo([
            _mk("H", "A", 2, 0, date_="2020-01-01"),
            _mk("H", "A", 1, 1, date_="2020-02-01"),
            _mk("H", "A", 0, 1, date_="2020-03-01"),
        ])
        self.assertEqual(rows[0]["outcome"], 0)   # home wins
        self.assertEqual(rows[1]["outcome"], 1)   # draw
        self.assertEqual(rows[2]["outcome"], 2)   # away wins

    def test_chronological_invariant_assertion(self):
        # roll_elo sorts internally, so out-of-order input still works
        ms = [_mk("A", "B", 2, 0, date_="2020-02-01"),
              _mk("A", "B", 0, 1, date_="2020-01-01")]
        rows = fh.roll_elo(ms)
        self.assertEqual([r["date"] for r in rows], ["2020-01-01", "2020-02-01"])


class FeatureVectorTests(unittest.TestCase):
    def test_feature_order_matches_hybrid_module(self):
        """Same FEATURES tuple drives train and predict — a length mismatch
        would be caught here before fitting an unloadable artifact."""
        row = fh.roll_elo([_mk("A", "B", 2, 0)])[0]
        v = fh.feature_vector(row, pr.Config())
        self.assertEqual(len(v), len(hy.FEATURES))

    def test_feature_vector_round_trip(self):
        """Match each value at its canonical index — order is the contract."""
        row = fh.roll_elo([_mk("A", "B", 2, 0)])[0]
        v = fh.feature_vector(row, pr.Config())
        d = dict(zip(hy.FEATURES, v))
        self.assertAlmostEqual(d["elo_a"], row["elo_home_pre"])
        self.assertAlmostEqual(d["elo_b"], row["elo_away_pre"])
        self.assertAlmostEqual(d["elo_gap"], row["elo_home_pre"] - row["elo_away_pre"])
        self.assertEqual(d["is_neutral"], 0)
        self.assertEqual(d["home_advantage_side"], +1)

    def test_neutral_match_sets_side_zero(self):
        rows = fh.roll_elo([_mk("A", "B", 1, 1, neutral=True)])
        v = fh.feature_vector(rows[0], pr.Config())
        d = dict(zip(hy.FEATURES, v))
        self.assertEqual(d["is_neutral"], 1)
        self.assertEqual(d["home_advantage_side"], 0)


class MetricsTests(unittest.TestCase):
    def test_rps_perfect_call_is_zero(self):
        self.assertAlmostEqual(fh.rps(1.0, 0.0, 0.0, 0), 0.0)
        self.assertAlmostEqual(fh.rps(0.0, 1.0, 0.0, 1), 0.0)
        self.assertAlmostEqual(fh.rps(0.0, 0.0, 1.0, 2), 0.0)

    def test_rps_respects_ordering(self):
        """Calling A-win when actual is A is the right call; B-win is the most
        wrong; draw is in between. RPS must reflect that ordering."""
        # actual: A wins (outcome=0)
        wrong_by_two = fh.rps(0.0, 0.0, 1.0, 0)   # called B with certainty
        wrong_by_one = fh.rps(0.0, 1.0, 0.0, 0)   # called draw with certainty
        self.assertGreater(wrong_by_two, wrong_by_one)

    def test_logloss_matches_definition(self):
        self.assertAlmostEqual(fh.logloss(0.5, 0.3, 0.2, 0), -math.log(0.5), places=12)
        self.assertAlmostEqual(fh.logloss(0.1, 0.1, 0.8, 2), -math.log(0.8), places=12)

    def test_brier_perfect_call_is_zero(self):
        self.assertAlmostEqual(fh.multiclass_brier(1.0, 0.0, 0.0, 0), 0.0)

    def test_evaluate_arrays_handles_empty(self):
        r = fh.evaluate_arrays([], [])
        self.assertEqual(r["n"], 0)
        self.assertTrue(math.isnan(r["rps"]))


class TimeDecayTests(unittest.TestCase):
    def test_most_recent_match_weight_is_one(self):
        rows = [_mk("A", "B", 1, 0, date_="2022-01-01"),
                _mk("A", "B", 1, 0, date_="2022-12-31")]
        # roll_elo-shaped dicts use the same "date" key as load_curated rows; the
        # weight function only reads date
        ws = fh.time_decay_weights(rows, ref_date="2022-12-31")
        self.assertAlmostEqual(ws[1], 1.0)
        self.assertLess(ws[0], 1.0)
        self.assertGreater(ws[0], 0.0)

    def test_half_life_consistent_with_tau(self):
        """At t = τ days back, the weight should be exp(-1) ≈ 0.3679."""
        rows = [_mk("A", "B", 1, 0, date_="2020-01-01")]
        ref = (date(2020, 1, 1) + timedelta(days=int(fh.TAU_DAYS))).isoformat()
        ws = fh.time_decay_weights(rows, ref_date=ref)
        self.assertAlmostEqual(ws[0], math.exp(-1.0), places=3)


class StructuralSynthesisTests(unittest.TestCase):
    """Synthesizing structural features per historical match must use the
    project's predict._wdl, not a reimplemented Poisson — same code path as the
    live model."""

    def test_equal_elo_neutral_match_yields_symmetric_probs(self):
        row = fh.roll_elo([_mk("A", "B", 1, 1, neutral=True)])[0]
        s = fh.synthesize_structural(row, pr.Config())
        self.assertAlmostEqual(s["p_a"], s["p_b"], places=9)
        self.assertGreater(s["p_draw"], 0.2)

    def test_probabilities_sum_to_one(self):
        row = fh.roll_elo([_mk("A", "B", 2, 0)])[0]
        s = fh.synthesize_structural(row, pr.Config())
        self.assertAlmostEqual(s["p_a"] + s["p_draw"] + s["p_b"], 1.0, places=6)


# ---------------------------------------------------------------- training tests (skip if no deps)

class PAVTests(unittest.TestCase):
    """Pool Adjacent Violators primitive. Each test isolates one property —
    bugs here break calibration silently (predictions still sum to 1 etc.)."""

    def test_already_monotone_returns_input_unchanged(self):
        xs = [0.1, 0.3, 0.5, 0.7, 0.9]
        ys = [0.05, 0.25, 0.45, 0.65, 0.85]
        out = fh.pool_adjacent_violators(xs, ys)
        for a, b in zip(out, ys):
            self.assertAlmostEqual(a, b, places=9)

    def test_single_violation_merged_to_weighted_mean(self):
        xs = [0.1, 0.3, 0.5]
        ys = [0.2, 0.8, 0.5]   # 0.8 > 0.5 -> violation, merged to (0.8+0.5)/2 = 0.65
        out = fh.pool_adjacent_violators(xs, ys)
        self.assertAlmostEqual(out[0], 0.2, places=9)
        self.assertAlmostEqual(out[1], 0.65, places=9)
        self.assertAlmostEqual(out[2], 0.65, places=9)

    def test_full_cascade_merges_to_global_mean(self):
        """A strictly decreasing y-sequence on an increasing x: every adjacent
        pair violates, the whole thing collapses to the mean. Hardens the
        backward-check in the merge loop."""
        xs = [0.1, 0.2, 0.3, 0.4, 0.5]
        ys = [0.9, 0.7, 0.5, 0.3, 0.1]
        out = fh.pool_adjacent_violators(xs, ys)
        mean = sum(ys) / len(ys)
        for v in out:
            self.assertAlmostEqual(v, mean, places=9)

    def test_output_is_always_non_decreasing(self):
        """Property test — random-ish ys, output must be monotone."""
        xs = [i / 100 for i in range(100)]
        ys = [((i * 13) % 7) / 7 for i in range(100)]   # noisy non-monotone
        out = fh.pool_adjacent_violators(xs, ys)
        for i in range(1, len(out)):
            self.assertGreaterEqual(out[i] + 1e-12, out[i - 1])


class FitIsotonicTests(unittest.TestCase):
    """The PAV-based isotonic CURVE — what fit_hybrid persists, what hybrid
    applies. Verifies the breakpoint compaction + apply round-trip."""

    def test_fit_then_apply_recovers_calibrated_values(self):
        """Fit on a 50/50 over-confident set and check apply gives sane values."""
        # 100 calibration points: booster says 0.7 with confidence, but actually
        # the class occurs only 50% of the time. After fit, apply(0.7) ≈ 0.5.
        x = [0.7] * 100
        y = [1.0 if i < 50 else 0.0 for i in range(100)]
        curve = fh.fit_isotonic(x, y)
        self.assertAlmostEqual(hy.apply_isotonic(curve, 0.7), 0.5, places=2)

    def test_perfect_calibration_already_returns_diagonal(self):
        """If the calibration data is already perfectly calibrated (e.g.,
        at p=0.3 the event happens 30% of the time), apply ≈ identity."""
        x = []
        y = []
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            for i in range(100):
                x.append(p)
                y.append(1.0 if i < p * 100 else 0.0)
        curve = fh.fit_isotonic(x, y)
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            self.assertAlmostEqual(hy.apply_isotonic(curve, p), p, delta=0.05)

    def test_curve_is_json_serializable(self):
        """Persistence contract — the fit output must round-trip through json."""
        import json
        curve = fh.fit_isotonic([0.1, 0.3, 0.6, 0.9], [0, 0, 1, 1])
        s = json.dumps(curve)
        back = json.loads(s)
        self.assertEqual(curve, back)

    def test_handles_empty_input(self):
        self.assertEqual(fh.fit_isotonic([], []), [])


class FitPlattTests(unittest.TestCase):
    def test_recovers_known_sigmoid(self):
        """Synthesize labels from σ(4x - 2); Platt should recover similar A, B."""
        import random
        rng = random.Random(42)
        x = []
        y = []
        for _ in range(2000):
            xi = rng.random()
            p_true = 1.0 / (1.0 + math.exp(-(4.0 * xi - 2.0)))
            x.append(xi)
            y.append(1.0 if rng.random() < p_true else 0.0)
        A, B = fh.fit_platt(x, y)
        # Should be ROUGHLY (4, -2) — Platt's 1/(n+2) smoothing biases towards
        # less-extreme slopes, so we allow generous tolerance.
        self.assertGreater(A, 2.0)
        self.assertLess(B, 0.0)

    def test_monotone_increasing_for_positive_A(self):
        A, B = fh.fit_platt([0.1, 0.2, 0.7, 0.8] * 50,
                            [0.0, 0.0, 1.0, 1.0] * 50)
        self.assertGreater(A, 0.0)

    def test_constant_class_returns_finite_params(self):
        """Degenerate input — all positives — must not crash or return NaN."""
        A, B = fh.fit_platt([0.5] * 20, [1.0] * 20)
        self.assertTrue(math.isfinite(A))
        self.assertTrue(math.isfinite(B))


class TripleApplyTests(unittest.TestCase):
    """The _apply_*_triple wrappers used during evaluation. Must renormalise."""

    def test_isotonic_triple_renormalises(self):
        per_class = [[[0.0, 0.0], [1.0, 0.5]]] * 3   # halve everything
        out = fh._apply_iso_triple(per_class, (0.6, 0.3, 0.1))
        self.assertAlmostEqual(sum(out), 1.0, places=9)

    def test_platt_triple_renormalises(self):
        per_class = [[1.0, 0.0]] * 3
        out = fh._apply_platt_triple(per_class, (0.6, 0.3, 0.1))
        self.assertAlmostEqual(sum(out), 1.0, places=9)


class TrainingSmokeTests(unittest.TestCase):
    """End-to-end smoke at the smallest viable scale. Skipped without deps."""

    @unittest.skipUnless(_has_xgboost(), "xgboost / numpy not installed")
    def test_train_predict_returns_simplex(self):
        # 50 synthetic matches alternating A vs B winners
        ms = []
        for i in range(50):
            d = f"2020-{1 + (i // 20):02d}-{1 + (i % 20):02d}"
            h, a = ("A", "B") if i % 2 == 0 else ("B", "A")
            ms.append(_mk(h, a, 2, 0, date_=d, tournament="FIFA World Cup"))
        rows = fh.roll_elo(ms)
        booster, names = fh.train_booster(rows, pr.Config())
        self.assertEqual(names, list(hy.FEATURES))
        probs = fh.booster_predict_proba(booster, rows[:5], pr.Config())
        self.assertEqual(len(probs), 5)
        for p in probs:
            self.assertAlmostEqual(sum(p), 1.0, places=5)

    @unittest.skipUnless(_has_xgboost(), "xgboost / numpy not installed")
    def test_artifact_round_trip_loadable_by_hybrid(self):
        """A booster saved by fit_hybrid must be loadable by hybrid._load_booster
        (same on-disk format + meta schema) — the integration contract."""
        import json
        ms = [_mk("A" if i % 2 == 0 else "B", "B" if i % 2 == 0 else "A",
                  2, 0, date_=f"2020-{1 + (i // 20):02d}-{1 + (i % 20):02d}",
                  tournament="FIFA World Cup")
              for i in range(50)]
        rows = fh.roll_elo(ms)
        booster, names = fh.train_booster(rows, pr.Config())
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "hybrid.ubj"
            meta = Path(d) / "hybrid.meta.json"
            booster.save_model(str(art))
            meta.write_text(json.dumps({
                "source": "xgb-smoke", "asof": "2026-06-15",
                "feature_names": names,
            }), encoding="utf-8")
            loaded = hy._load_booster(artifact_path=art, meta_path=meta)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.meta["source"], "xgb-smoke")
            p = loaded.predict_proba(fh.feature_vector(rows[0], pr.Config()))
            self.assertAlmostEqual(sum(p), 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
