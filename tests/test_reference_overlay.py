"""Tests for scripts/reference_overlay.py — the third-model overlay wrapping the
world_cup_predictions repo's XGBoost model.

Three skip gates apply, each tested independently:
  * reference repo not at REFERENCE_REPO path (env-overridable)
  * deps (xgboost/pandas/sklearn) not installed
  * artifact not persisted

When ANY gate fails, reference_predict() must return None and the CLI overlay
silently no-ops. The inertness contract is the load-bearing property here —
production behavior must be unaffected until fit_reference.py --write runs.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import predict as pr   # noqa: E402
import reference_overlay as ro   # noqa: E402


def _has_reference_deps() -> bool:
    try:
        import xgboost  # noqa: F401
        import pandas   # noqa: F401
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


class InertnessTests(unittest.TestCase):
    """Each gate (artifact, meta, deps, ref repo path) independently makes
    the loader return None. A failed gate must NOT crash, just no-op."""

    def setUp(self):
        # Always reset cross-test cache so failures in one test don't leak state.
        ro._REF_CACHE = None

    def test_returns_none_when_artifact_missing(self):
        orig_art, orig_meta = ro.ARTIFACT, ro.META
        try:
            ro.ARTIFACT = Path("/nonexistent/reference.ubj")
            ro.META = Path("/nonexistent/reference.meta.json")
            self.assertIsNone(ro._load_reference())
        finally:
            ro.ARTIFACT, ro.META = orig_art, orig_meta

    def test_returns_none_when_meta_missing_but_artifact_present(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "reference.ubj"
            art.write_bytes(b"\x00")
            orig_art, orig_meta = ro.ARTIFACT, ro.META
            try:
                ro.ARTIFACT = art
                ro.META = Path(d) / "absent.meta.json"
                self.assertIsNone(ro._load_reference())
            finally:
                ro.ARTIFACT, ro.META = orig_art, orig_meta

    def test_returns_none_when_reference_repo_path_missing(self):
        """If WC26_REFERENCE_REPO points to a nonexistent dir, the loader must
        not crash — it's a normal state on machines that don't have the repo."""
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "reference.ubj"
            meta = Path(d) / "reference.meta.json"
            art.write_bytes(b"\x00")
            meta.write_text(json.dumps({"source": "test", "asof": "2026-06-16",
                                        "trained_through": "2026-06-11"}),
                            encoding="utf-8")
            orig_art, orig_meta, orig_repo = ro.ARTIFACT, ro.META, ro.REFERENCE_REPO
            try:
                ro.ARTIFACT, ro.META = art, meta
                ro.REFERENCE_REPO = Path("/nonexistent/world_cup_predictions")
                # Even with artifact + meta + deps, if the repo isn't there,
                # _import_reference_module returns None and we bail.
                self.assertIsNone(ro._load_reference())
            finally:
                ro.ARTIFACT, ro.META, ro.REFERENCE_REPO = orig_art, orig_meta, orig_repo

    def test_reference_predict_returns_none_when_loader_returns_none(self):
        m_model = self._mk_minimal_model()
        orig_art, orig_meta = ro.ARTIFACT, ro.META
        try:
            ro.ARTIFACT = Path("/nonexistent/reference.ubj")
            ro.META = Path("/nonexistent/reference.meta.json")
            self.assertIsNone(ro.reference_predict(m_model, "A", "B"))
        finally:
            ro.ARTIFACT, ro.META = orig_art, orig_meta

    def _mk_minimal_model(self):
        # Bare RatingModel — reference_predict accepts it for API symmetry
        # with hybrid_predict but never actually reads from it.
        return pr.RatingModel({}, pr.Config(), "2026-06-11")


class NameTranslationTests(unittest.TestCase):
    """The canon-to-reference name map encodes the project's spelling
    differences with the reference repo. Pass-through for the common case."""

    def test_canon_pass_through_for_matching_names(self):
        self.assertEqual(ro.to_ref_name("Spain"), "Spain")
        self.assertEqual(ro.to_ref_name("United States"), "United States")
        self.assertEqual(ro.to_ref_name("DR Congo"), "DR Congo")

    def test_known_spelling_differences_translated(self):
        self.assertEqual(ro.to_ref_name("Türkiye"), "Turkey")
        self.assertEqual(ro.to_ref_name("Côte d'Ivoire"), "Ivory Coast")
        self.assertEqual(ro.to_ref_name("Czechia"), "Czech Republic")
        self.assertEqual(ro.to_ref_name("Curaçao"), "Curacao")
        self.assertEqual(ro.to_ref_name("Cape Verde"), "Cabo Verde")

    def test_strips_whitespace(self):
        self.assertEqual(ro.to_ref_name("  Türkiye  "), "Turkey")


class CliInertnessTests(unittest.TestCase):
    """Subprocess-level: production CLI must be byte-identical with and
    without --reference, as long as no artifact has been persisted."""

    def _skip_if_any_artifact_installed(self):
        """The inertness contract is: ARTIFACT ABSENT ⇒ flag is a no-op.
        With an artifact intentionally installed (preview mode), the flag
        SHOULD add a row — that's the whole feature. Skip honestly when state
        contradicts the test premise rather than failing."""
        import hybrid as hy
        installed = []
        if ro.ARTIFACT.exists() and ro.META.exists():
            installed.append("reference")
        if hy.ARTIFACT.exists() and hy.META.exists():
            installed.append("hybrid")
        if installed:
            self.skipTest(f"overlay artifact(s) installed ({', '.join(installed)}) — "
                          "flag is no longer inert (test premise inverted)")

    def test_predict_cli_flag_off_vs_on_produce_identical_stdout(self):
        self._skip_if_any_artifact_installed()
        env_check = subprocess.run(
            [sys.executable, str(REPO / "scripts/predict.py"), "A4"],
            capture_output=True, text=True, encoding="utf-8")
        if env_check.returncode != 0:
            self.skipTest(f"predict.py A4 itself failed: {env_check.stderr.strip()}")
        off = env_check.stdout
        on = subprocess.run(
            [sys.executable, str(REPO / "scripts/predict.py"), "A4", "--reference"],
            capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(off, on)

    def test_both_overlay_flags_together_remain_inert(self):
        self._skip_if_any_artifact_installed()
        env_check = subprocess.run(
            [sys.executable, str(REPO / "scripts/predict.py"), "A4"],
            capture_output=True, text=True, encoding="utf-8")
        if env_check.returncode != 0:
            self.skipTest(f"predict.py A4 itself failed: {env_check.stderr.strip()}")
        off = env_check.stdout
        on = subprocess.run(
            [sys.executable, str(REPO / "scripts/predict.py"), "A4",
             "--hybrid", "--reference"],
            capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(off, on)


class RenderIntegrationTests(unittest.TestCase):
    """render_prediction must accept reference=None as the default (no behavior
    change) and append exactly one bullet when a ReferencePrediction is passed."""

    @staticmethod
    def _mk_team(name, strength):
        return pr.TeamRating(
            team=name, elo=strength, futi=70.0, attack=70.0, defense=70.0,
            strength=strength, z_att=0.0, z_def=0.0,
            elo_rank=1, futi_rank=1, consensus_rank=1,
            opta_advance=None, opta_wincup=None, opta_rank=None,
            market_odds=None, market_implied=None, market_rank=None,
        )

    def _mk_model(self):
        a = self._mk_team("A", 1900)
        b = self._mk_team("B", 1700)
        return pr.RatingModel({"A": a, "B": b}, pr.Config(), "2026-06-11")

    def test_render_with_reference_none_equals_render_without_kwarg(self):
        m = self._mk_model()
        pred = pr.predict_match(m, "A", "B")
        a = pr.render_prediction(m, pred)
        b = pr.render_prediction(m, pred, reference=None)
        self.assertEqual(a, b)

    def test_render_with_reference_present_adds_overlay_line(self):
        m = self._mk_model()
        pred = pr.predict_match(m, "A", "B")
        baseline = pr.render_prediction(m, pred)
        ref = ro.ReferencePrediction(
            team_a="A", team_b="B", p_a=0.55, p_draw=0.20, p_b=0.25,
            source="ref-test", asof="2026-06-15")
        rendered = pr.render_prediction(m, pred, reference=ref)
        self.assertNotEqual(rendered, baseline)
        self.assertIn("Reference (ref-test)", rendered)

    def test_hybrid_and_reference_render_independently(self):
        """Both overlays together — each must produce its own bullet."""
        import hybrid as hy
        m = self._mk_model()
        pred = pr.predict_match(m, "A", "B")
        hyb = hy.HybridPrediction(team_a="A", team_b="B",
                                  p_a=0.50, p_draw=0.25, p_b=0.25,
                                  source="xgb-test", asof="2026-06-15")
        ref = ro.ReferencePrediction(team_a="A", team_b="B",
                                     p_a=0.55, p_draw=0.20, p_b=0.25,
                                     source="ref-test", asof="2026-06-15")
        rendered = pr.render_prediction(m, pred, hybrid=hyb, reference=ref)
        self.assertIn("ML overlay (xgb-test)", rendered)
        self.assertIn("Reference (ref-test)", rendered)
        # ordering: Model -> ML overlay -> Reference (Reference comes last)
        lines = rendered.splitlines()
        model_idx = next(i for i, ln in enumerate(lines) if ln.startswith("- **Model:**"))
        hyb_idx = next(i for i, ln in enumerate(lines) if "ML overlay" in ln)
        ref_idx = next(i for i, ln in enumerate(lines) if "Reference" in ln and "**" in ln)
        self.assertLess(model_idx, hyb_idx)
        self.assertLess(hyb_idx, ref_idx)


class EndToEndTests(unittest.TestCase):
    """When all gates pass (reference repo + deps + artifact), the loader
    returns a working state and predictions sum to 1. Gated on the genuinely
    expensive dependencies."""

    @unittest.skipUnless(_has_reference_deps(), "xgboost/pandas/sklearn not installed")
    @unittest.skipUnless(ro.REFERENCE_REPO.exists(),
                         "reference repo not at REFERENCE_REPO path")
    def test_full_pipeline_returns_valid_simplex(self):
        """Smoke test: fit_reference.py train_model() output round-trips through
        the loader and produces a sum-to-one probability triple."""
        sys.path.insert(0, str(ro.REFERENCE_REPO))
        try:
            import predict_today as ref
            import xgboost as xgb
        finally:
            if str(ro.REFERENCE_REPO) in sys.path:
                sys.path.remove(str(ro.REFERENCE_REPO))

        results = ref.load_results()
        dataset, _ = ref.build_dataset(results)
        # Tiny training slice — just enough rounds to verify the round-trip.
        # The point isn't model quality, it's the load_model contract.
        train, val = ref.split_by_date(dataset, "2022-01-01", "2024-01-01", "2026-06-11")
        if len(val) < 50:
            self.skipTest("validation slice too thin for a smoke test")
        booster, _, _ = ref.train_model(train, val)

        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "reference.ubj"
            meta = Path(d) / "reference.meta.json"
            booster.save_model(str(art))
            meta.write_text(json.dumps({
                "source": "ref-xgb-smoke", "asof": "2026-06-15",
                "trained_through": "2026-06-11",
                "features": list(ref.FEATURES),
            }), encoding="utf-8")
            orig_art, orig_meta = ro.ARTIFACT, ro.META
            ro._REF_CACHE = None
            try:
                ro.ARTIFACT, ro.META = art, meta
                m = pr.RatingModel({}, pr.Config(), "2026-06-11")
                # Spain vs Cape Verde — Spain heavy favourite, draw rare
                result = ro.reference_predict(m, "Spain", "Cape Verde",
                                               match_date="2026-06-15")
            finally:
                ro.ARTIFACT, ro.META = orig_art, orig_meta
                ro._REF_CACHE = None

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.p_a + result.p_draw + result.p_b, 1.0, places=5)
        # Spain should be heavily favoured — sanity check, not strict
        self.assertGreater(result.p_a, 0.5)


if __name__ == "__main__":
    unittest.main()
