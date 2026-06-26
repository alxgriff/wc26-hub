"""Tests for scripts/hybrid.py and the predict.py overlay-rendering hook.

The load-bearing test here is the INERTNESS regression: with no artifact (the
default state today, and the default state until fit_hybrid.py writes one), the
hybrid layer must not change render_prediction's output by a single byte. The
CLI inertness extension lives in tests/test_predict.py.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import predict as pr  # noqa: E402
import hybrid as hy   # noqa: E402


def mk(name, strength, z_att=0.0, z_def=0.0, elo=None):
    return pr.TeamRating(
        team=name, elo=elo if elo is not None else strength, futi=70.0,
        attack=70.0, defense=70.0,
        strength=strength, z_att=z_att, z_def=z_def,
        elo_rank=1, futi_rank=1, consensus_rank=1,
        opta_advance=None, opta_wincup=None, opta_rank=None,
        market_odds=None, market_implied=None, market_rank=None,
    )


def model(*teams, **cfg):
    return pr.RatingModel({t.team: t for t in teams}, pr.Config(**cfg), "2026-06-11")


class LoaderInertnessTests(unittest.TestCase):
    """The hybrid layer must be dead unless every required ingredient is present:
    the artifact file, the meta file, and the deps. Each gate gets its own test."""

    def test_returns_none_when_artifact_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(hy._load_booster(
                artifact_path=Path(d) / "absent.ubj",
                meta_path=Path(d) / "absent.meta.json"))

    def test_returns_none_when_meta_missing_but_artifact_present(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "hybrid.ubj"
            art.write_bytes(b"\x00")   # contents don't matter; meta is missing
            self.assertIsNone(hy._load_booster(
                artifact_path=art, meta_path=Path(d) / "absent.meta.json"))

    def test_returns_none_when_meta_feature_names_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "hybrid.ubj"
            meta = Path(d) / "hybrid.meta.json"
            art.write_bytes(b"\x00")
            meta.write_text(json.dumps({
                "source": "stub", "asof": "2026-06-15",
                "feature_names": ["wrong", "schema"],
            }), encoding="utf-8")
            # Either returns None right away (missing xgboost) or the explicit
            # feature-name guard fires. In both cases: silently inert.
            self.assertIsNone(hy._load_booster(artifact_path=art, meta_path=meta))

    def test_hybrid_predict_returns_none_when_module_state_clean(self):
        m = model(mk("A", 1900), mk("B", 1700))
        orig_art, orig_meta = hy.ARTIFACT, hy.META
        try:
            hy.ARTIFACT = Path("/nonexistent/hybrid.ubj")
            hy.META = Path("/nonexistent/hybrid.meta.json")
            self.assertIsNone(hy.hybrid_predict(m, "A", "B"))
        finally:
            hy.ARTIFACT, hy.META = orig_art, orig_meta


class RendererInertnessTests(unittest.TestCase):
    """The render_prediction hook must be a true no-op when ``hybrid`` is None."""

    def test_render_with_hybrid_none_equals_render_without_kwarg(self):
        m = model(mk("A", 1900, 0.4, -0.2), mk("B", 1700, -0.3, 0.3))
        pred = pr.predict_match(m, "A", "B", hfa_team="A")
        a = pr.render_prediction(m, pred)
        b = pr.render_prediction(m, pred, hybrid=None)
        self.assertEqual(a, b)

    def test_render_with_hybrid_present_adds_overlay_line(self):
        m = model(mk("A", 1900, 0.4, -0.2), mk("B", 1700, -0.3, 0.3))
        pred = pr.predict_match(m, "A", "B", hfa_team="A")
        baseline = pr.render_prediction(m, pred)
        overlay = hy.HybridPrediction(
            team_a="A", team_b="B", p_a=0.55, p_draw=0.20, p_b=0.25,
            source="xgb-test", asof="2026-06-15")
        rendered = pr.render_prediction(m, pred, hybrid=overlay)
        self.assertNotEqual(rendered, baseline)
        self.assertIn("ML overlay (xgb-test)", rendered)
        # overlay block is a sibling bullet immediately after the Model bullet
        model_idx = rendered.splitlines().index(next(
            ln for ln in rendered.splitlines() if ln.startswith("- **Model:**")))
        overlay_idx = rendered.splitlines().index(next(
            ln for ln in rendered.splitlines() if "ML overlay" in ln))
        self.assertEqual(overlay_idx, model_idx + 1)


class FeatureExtractionTests(unittest.TestCase):
    """The feature vector is the contract between fit_hybrid (train) and hybrid
    (predict). Catching drift here is cheaper than catching it as a calibration
    miss in production."""

    def test_vector_length_matches_FEATURES_tuple(self):
        m = model(mk("A", 1900, 0.4, -0.2, elo=1900), mk("B", 1700, -0.3, 0.3, elo=1700))
        pred = pr.predict_match(m, "A", "B")
        v = hy.extract_features_live(m, pred, "A", "B")
        self.assertEqual(len(v), len(hy.FEATURES))

    def test_elo_fields_match_rating_model(self):
        m = model(mk("A", 1900, elo=1875), mk("B", 1700, elo=1758))
        pred = pr.predict_match(m, "A", "B")
        v = hy.extract_features_live(m, pred, "A", "B")
        d = dict(zip(hy.FEATURES, v))
        self.assertAlmostEqual(d["elo_a"], 1875.0, places=9)
        self.assertAlmostEqual(d["elo_b"], 1758.0, places=9)
        self.assertAlmostEqual(d["elo_gap"], 1875.0 - 1758.0, places=9)

    def test_recovers_sup_and_texture_from_prediction(self):
        """sup = ln(lam_a/lam_b) and texture = ln(total/mu0)/alpha must round-trip:
        feeding them back through predict_match's formulas must reproduce lam_a/lam_b."""
        m = model(mk("A", 1900, 0.4, -0.2), mk("B", 1700, -0.3, 0.3))
        pred = pr.predict_match(m, "A", "B", hfa_team="A")
        v = dict(zip(hy.FEATURES, hy.extract_features_live(m, pred, "A", "B", hfa_team="A")))
        # round-trip texture
        total_reco = m.config.mu0 * math.exp(m.config.alpha * v["texture"])
        self.assertAlmostEqual(total_reco, pred.total, places=9)
        # round-trip sup -> share -> lambdas
        share = 1.0 / (1.0 + math.exp(-v["sup"]))
        self.assertAlmostEqual(pred.total * share, pred.lambda_a, places=9)
        self.assertAlmostEqual(pred.total * (1 - share), pred.lambda_b, places=9)

    def test_hfa_propagates_through_sup(self):
        m = model(mk("A", 1800), mk("B", 1800))
        neutral = pr.predict_match(m, "A", "B")
        home_a = pr.predict_match(m, "A", "B", hfa_team="A")
        v_neu = dict(zip(hy.FEATURES, hy.extract_features_live(m, neutral, "A", "B")))
        v_home = dict(zip(hy.FEATURES,
                          hy.extract_features_live(m, home_a, "A", "B", hfa_team="A")))
        self.assertGreater(v_home["sup"], v_neu["sup"])
        self.assertEqual(v_neu["home_advantage_side"], 0)
        self.assertEqual(v_neu["is_neutral"], 1)
        self.assertEqual(v_home["home_advantage_side"], +1)
        self.assertEqual(v_home["is_neutral"], 0)

    def test_home_advantage_side_flips_when_team_b_is_host(self):
        m = model(mk("A", 1800), mk("B", 1800))
        pred = pr.predict_match(m, "A", "B", hfa_team="B")
        v = dict(zip(hy.FEATURES, hy.extract_features_live(m, pred, "A", "B", hfa_team="B")))
        self.assertEqual(v["home_advantage_side"], -1)
        self.assertEqual(v["is_neutral"], 0)


class StubBoosterPredictTests(unittest.TestCase):
    """End-to-end of hybrid_predict() with a fake booster — verifies the
    normalization and metadata propagation without needing xgboost installed."""

    def test_hybrid_predict_with_stubbed_loader_returns_simplex(self):
        m = model(mk("A", 1900, 0.4, -0.2), mk("B", 1700, -0.3, 0.3))

        class StubBooster:
            meta = {"source": "xgb-stub", "asof": "2026-06-15",
                    "feature_names": list(hy.FEATURES)}
            def predict_proba(self, feats):
                # ignores features — returns an unnormalised triple; hybrid_predict normalises
                return 0.55, 0.20, 0.25

        orig = hy._load_booster
        try:
            hy._load_booster = lambda: StubBooster()
            result = hy.hybrid_predict(m, "A", "B", hfa_team="A")
        finally:
            hy._load_booster = orig

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.p_a + result.p_draw + result.p_b, 1.0, places=9)
        self.assertEqual(result.source, "xgb-stub")
        self.assertEqual(result.asof, "2026-06-15")
        self.assertEqual((result.team_a, result.team_b), ("A", "B"))

    def test_hybrid_predict_normalises_unnormalised_booster_output(self):
        m = model(mk("A", 1900), mk("B", 1700))

        class StubBooster:
            meta = {"source": "s", "asof": "2026-06-15",
                    "feature_names": list(hy.FEATURES)}
            def predict_proba(self, feats):
                return 2.0, 1.0, 1.0   # sum=4

        orig = hy._load_booster
        try:
            hy._load_booster = lambda: StubBooster()
            r = hy.hybrid_predict(m, "A", "B")
        finally:
            hy._load_booster = orig

        self.assertAlmostEqual(r.p_a, 0.5)
        self.assertAlmostEqual(r.p_draw, 0.25)
        self.assertAlmostEqual(r.p_b, 0.25)


class IsotonicApplyTests(unittest.TestCase):
    """The predict-time application of an isotonic curve. Monotonicity + edge
    clamping + interpolation are the contract."""

    def test_clamps_below_first_breakpoint(self):
        curve = [[0.2, 0.1], [0.5, 0.4], [0.9, 0.8]]
        self.assertEqual(hy.apply_isotonic(curve, 0.0), 0.1)
        self.assertEqual(hy.apply_isotonic(curve, 0.15), 0.1)

    def test_clamps_above_last_breakpoint(self):
        curve = [[0.2, 0.1], [0.5, 0.4], [0.9, 0.8]]
        self.assertEqual(hy.apply_isotonic(curve, 1.0), 0.8)
        self.assertEqual(hy.apply_isotonic(curve, 0.95), 0.8)

    def test_linear_interpolation_between_breakpoints(self):
        # midpoint of (0.2, 0.1) -> (0.6, 0.5) should be 0.3 -> 0.5 lerp
        curve = [[0.2, 0.1], [0.6, 0.5]]
        self.assertAlmostEqual(hy.apply_isotonic(curve, 0.4), 0.3, places=9)

    def test_exact_breakpoint_hit(self):
        curve = [[0.2, 0.1], [0.5, 0.4], [0.9, 0.8]]
        self.assertAlmostEqual(hy.apply_isotonic(curve, 0.5), 0.4, places=9)

    def test_monotone_curve_yields_monotone_output(self):
        curve = [[0.0, 0.05], [0.3, 0.2], [0.6, 0.5], [1.0, 0.95]]
        prev = -1.0
        for x in [i / 50 for i in range(51)]:
            v = hy.apply_isotonic(curve, x)
            self.assertGreaterEqual(v + 1e-12, prev)
            prev = v


class PlattApplyTests(unittest.TestCase):
    def test_sigmoid_round_trip(self):
        # A=4, B=-2 -> σ(4*0.5 - 2) = σ(0) = 0.5
        self.assertAlmostEqual(hy.apply_platt((4.0, -2.0), 0.5), 0.5, places=9)

    def test_overflow_guard(self):
        # A=1e6 should saturate, not crash
        self.assertAlmostEqual(hy.apply_platt((1e6, 0.0), 1.0), 1.0)
        self.assertAlmostEqual(hy.apply_platt((1e6, 0.0), -1.0), 0.0)


class CalibratorTests(unittest.TestCase):
    """The _Calibrator dispatches by method and renormalises the triple. Loader
    integration is covered separately."""

    def test_isotonic_dispatches_and_renormalises(self):
        # 3 identity curves over [0,1] => calibrated output is identical to input
        identity = [[0.0, 0.0], [1.0, 1.0]]
        cal = hy._Calibrator(method="isotonic", per_class=[identity] * 3)
        p = cal.apply(0.5, 0.3, 0.2)
        self.assertAlmostEqual(sum(p), 1.0, places=9)
        self.assertAlmostEqual(p[0], 0.5, places=9)

    def test_platt_dispatches_and_renormalises(self):
        # 3 identity-ish Platt (A large, B=0) maps [0,1] roughly to itself, but
        # after renormalisation the triple sums to 1 regardless.
        platt = [[8.0, -4.0]] * 3
        cal = hy._Calibrator(method="platt", per_class=platt)
        p = cal.apply(0.6, 0.3, 0.1)
        self.assertAlmostEqual(sum(p), 1.0, places=9)
        # ordering preserved
        self.assertGreater(p[0], p[1])
        self.assertGreater(p[1], p[2])

    def test_isotonic_curve_squashes_overconfidence(self):
        """The motivating use case — a booster claims 0.85, calibration says
        actually those calls were right only 0.6 of the time. The triple becomes
        less confident in W and more spread over D/L."""
        squash_w = [[0.0, 0.0], [0.85, 0.60], [1.0, 0.8]]
        identity = [[0.0, 0.0], [1.0, 1.0]]
        cal = hy._Calibrator(method="isotonic",
                             per_class=[squash_w, identity, identity])
        # raw booster: 0.85 / 0.10 / 0.05
        p = cal.apply(0.85, 0.10, 0.05)
        self.assertAlmostEqual(sum(p), 1.0, places=9)
        # p_a after calibration should be lower than 0.85 (we squashed it)
        self.assertLess(p[0], 0.85)
        # but still the dominant class
        self.assertGreater(p[0], p[1])
        self.assertGreater(p[0], p[2])

    def test_uncalibrated_booster_unaffected_when_calibrator_none(self):
        """The _Booster wrapper must be a true pass-through when calibrator=None."""
        m = model(mk("A", 1900), mk("B", 1700))

        class StubBooster:
            meta = {"source": "s", "asof": "2026-06-16",
                    "feature_names": list(hy.FEATURES)}
            def predict_proba(self, feats):
                return 0.7, 0.2, 0.1

        orig = hy._load_booster
        try:
            hy._load_booster = lambda: StubBooster()
            r = hy.hybrid_predict(m, "A", "B")
        finally:
            hy._load_booster = orig

        self.assertAlmostEqual(r.p_a, 0.7)
        self.assertAlmostEqual(r.p_draw, 0.2)
        self.assertAlmostEqual(r.p_b, 0.1)


class CalibrationLoadingTests(unittest.TestCase):
    """The artifact-meta loader must accept a calibration block when present,
    and ignore it when malformed (logging a warning, falling back to uncalibrated)."""

    def test_loader_attaches_calibrator_when_meta_includes_block(self):
        import tempfile, json as jsonlib
        # We can't load a real xgboost booster without xgboost; the goal here
        # is to verify the dispatch reads the calibration block at all. Use a
        # zero-byte artifact and confirm: if xgboost ISN'T installed the loader
        # returns None (no calibrator to test), otherwise it attaches one.
        try:
            import xgboost  # noqa: F401
        except ImportError:
            self.skipTest("xgboost not installed — loader test requires it")

        # Train a tiny stub model so save_model produces a real artifact
        import scripts.fit_hybrid as fh
        m = model(mk("A", 1900, elo=1900), mk("B", 1700, elo=1700))
        # one synthetic match to get a real DMatrix shape
        synth_rows = [{"date": "2020-01-01", "home": "A", "away": "B",
                       "hs": 1, "as_": 0, "neutral": False, "tournament": "FIFA World Cup",
                       "elo_home_pre": 1900, "elo_away_pre": 1700, "outcome": 0}] * 30
        import predict as pr2
        booster, _ = fh.train_booster(synth_rows, pr2.Config())
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "hybrid.ubj"
            meta = Path(d) / "hybrid.meta.json"
            booster.save_model(str(art))
            meta.write_text(jsonlib.dumps({
                "source": "s", "asof": "2026-06-16",
                "feature_names": list(hy.FEATURES),
                "calibration": {"method": "isotonic", "per_class": [
                    [[0.0, 0.0], [1.0, 1.0]],
                    [[0.0, 0.0], [1.0, 1.0]],
                    [[0.0, 0.0], [1.0, 1.0]],
                ]},
            }), encoding="utf-8")
            loaded = hy._load_booster(artifact_path=art, meta_path=meta)
            self.assertIsNotNone(loaded)
            self.assertIsNotNone(loaded.calibrator)
            self.assertEqual(loaded.calibrator.method, "isotonic")

    def test_loader_falls_back_when_calibration_block_malformed(self):
        import tempfile, json as jsonlib
        try:
            import xgboost  # noqa: F401
        except ImportError:
            self.skipTest("xgboost not installed — loader test requires it")
        import scripts.fit_hybrid as fh
        synth_rows = [{"date": "2020-01-01", "home": "A", "away": "B",
                       "hs": 1, "as_": 0, "neutral": False, "tournament": "FIFA World Cup",
                       "elo_home_pre": 1900, "elo_away_pre": 1700, "outcome": 0}] * 30
        import predict as pr2
        booster, _ = fh.train_booster(synth_rows, pr2.Config())
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "hybrid.ubj"
            meta = Path(d) / "hybrid.meta.json"
            booster.save_model(str(art))
            meta.write_text(jsonlib.dumps({
                "source": "s", "asof": "2026-06-16",
                "feature_names": list(hy.FEATURES),
                "calibration": {"method": "isotonic", "per_class": [[]]},   # malformed (1 class)
            }), encoding="utf-8")
            loaded = hy._load_booster(artifact_path=art, meta_path=meta)
            self.assertIsNotNone(loaded)
            self.assertIsNone(loaded.calibrator)   # fell back to uncalibrated


class TournamentWeightTests(unittest.TestCase):
    def test_major_tournament_weight_is_top(self):
        self.assertEqual(hy.tournament_weight_for("FIFA World Cup"), 1.0)
        self.assertEqual(hy.tournament_weight_for("UEFA Euro"), 1.0)

    def test_qualifier_weight_below_major(self):
        self.assertLess(hy.tournament_weight_for("FIFA World Cup qualification"),
                        hy.tournament_weight_for("FIFA World Cup"))

    def test_unknown_tournament_falls_through_to_default(self):
        self.assertEqual(hy.tournament_weight_for("Made Up Cup"),
                         hy.DEFAULT_TOURNAMENT_WEIGHT)


class CliInertnessTests(unittest.TestCase):
    """Subprocess-level: until an artifact exists, --hybrid has zero stdout effect."""

    def test_predict_cli_flag_off_vs_on_produce_identical_stdout(self):
        # The inertness contract is: ARTIFACT ABSENT ⇒ flag is a no-op.
        # When the artifact has been intentionally installed (preview mode),
        # the flag SHOULD add an overlay row — that's the whole feature.
        # Skip honestly rather than fail when state contradicts the premise.
        if hy.ARTIFACT.exists() and hy.META.exists():
            self.skipTest("hybrid artifact installed — flag is no longer "
                          "supposed to be inert (test premise inverted)")
        # Skip if Opta/Market overlays would fail because fixtures or ratings
        # are missing — this test exercises the predict.py CLI and so needs the
        # repo's real ratings to load. A bare repo without ratings/ wouldn't run.
        env_check = subprocess.run(
            [sys.executable, str(REPO / "scripts/predict.py"), "A4"],
            capture_output=True, text=True, encoding="utf-8")
        if env_check.returncode != 0:
            self.skipTest(f"predict.py A4 itself failed: {env_check.stderr.strip()}")
        off = env_check.stdout
        on = subprocess.run(
            [sys.executable, str(REPO / "scripts/predict.py"), "A4", "--hybrid"],
            capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(off, on)


if __name__ == "__main__":
    unittest.main()
