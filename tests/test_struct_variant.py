"""Tests for scripts/struct_variant.py — the confederation-tuned structural overlay.

Inertness: no config file => tuned_predict() returns None (the production app is
unchanged). The "no-op reproduces baseline" guarantee: with w_futi=w_elo=1, no
offset, no market, the tuned model's W/D/L must equal the production model's —
proving the reweight math mirrors load_ratings exactly. And a CAF offset must
raise a CAF team's win probability.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import predict as pr            # noqa: E402
import struct_variant as sv     # noqa: E402


def _real_model():
    try:
        return pr.load_ratings()
    except Exception:
        return None


class InertnessTests(unittest.TestCase):
    def setUp(self):
        sv._CACHE.clear()

    def test_load_config_none_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sv._load_config(Path(d) / "nope.json"))

    def test_load_config_none_when_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "struct_variant.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(sv._load_config(p))

    def test_load_config_list_format(self):
        """A JSON array of variant dicts is loaded as-is."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "struct_variant.json"
            p.write_text('[{"label":"A"},{"label":"B"}]', encoding="utf-8")
            result = sv._load_config(p)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["label"], "A")

    def test_load_config_single_dict_wrapped(self):
        """Old single-dict format is wrapped in a list for backwards compat."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "struct_variant.json"
            p.write_text('{"label":"X"}', encoding="utf-8")
            result = sv._load_config(p)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["label"], "X")

    def test_tuned_predict_none_when_config_absent(self):
        m = _real_model()
        if m is None:
            self.skipTest("ratings unavailable")
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sv.tuned_predict(m, "Ghana", "Panama",
                                               config_path=Path(d) / "absent.json"))


class TunedBehaviourTests(unittest.TestCase):
    def setUp(self):
        sv._CACHE.clear()
        self.m = _real_model()
        if self.m is None:
            self.skipTest("ratings unavailable")

    def _cfg(self, d, **kw):
        p = Path(d) / "struct_variant.json"
        p.write_text(json.dumps(kw), encoding="utf-8")
        return p

    def test_noop_reproduces_production_wdl(self):
        """w_futi=w_elo=1, no offset, no market => identical to production."""
        a, b = "Brazil", "Morocco"
        prod = pr.predict_match(self.m, a, b)
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, label="x", w_futi=1.0, w_elo=1.0, w_market=0.0,
                            conf_offset={})
            sv._CACHE.clear()
            results = sv.tuned_predict(self.m, a, b, config_path=cfg)
        self.assertIsNotNone(results)
        self.assertEqual(len(results), 1)
        t = results[0]
        self.assertAlmostEqual(t.p_a, prod.p_a, places=9)
        self.assertAlmostEqual(t.p_draw, prod.p_draw, places=9)
        self.assertAlmostEqual(t.p_b, prod.p_b, places=9)

    def test_caf_offset_raises_caf_win_prob(self):
        """Boosting CAF strength must increase a CAF team's win probability."""
        a, b = "Ghana", "Panama"     # Ghana is CAF, Panama is CONCACAF
        base = pr.predict_match(self.m, a, b)
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, label="x", w_futi=1.0, w_elo=1.0,
                            conf_offset={"CAF": 100})
            sv._CACHE.clear()
            results = sv.tuned_predict(self.m, a, b, config_path=cfg)
        t = results[0]
        self.assertGreater(t.p_a, base.p_a)       # Ghana's win prob up
        self.assertLess(t.p_b, base.p_b)          # Panama's down
        self.assertAlmostEqual(t.p_a + t.p_draw + t.p_b, 1.0, places=9)

    def test_label_propagates(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, label="Structural (conf-tuned)", conf_offset={"CAF": 20})
            sv._CACHE.clear()
            results = sv.tuned_predict(self.m, "Egypt", "Belgium", config_path=cfg)
        self.assertEqual(results[0].source, "Structural (conf-tuned)")

    def test_futi_tilt_changes_output(self):
        """A 70/30 Futi tilt should move probabilities off the production values."""
        a, b = "Morocco", "Brazil"   # Morocco: Futi >> Elo, so a Futi tilt lifts it
        base = pr.predict_match(self.m, a, b)
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d, label="x", w_futi=0.7, w_elo=0.3, conf_offset={})
            sv._CACHE.clear()
            results = sv.tuned_predict(self.m, a, b, config_path=cfg)
        t = results[0]
        self.assertNotAlmostEqual(t.p_a, base.p_a, places=3)
        self.assertGreater(t.p_a, base.p_a)       # Morocco lifted by trusting Futi more

    def test_multi_variant_returns_all(self):
        """A list config returns one TunedPrediction per variant."""
        import json
        a, b = "Brazil", "Morocco"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "struct_variant.json"
            p.write_text(json.dumps([
                {"label": "V1", "w_futi": 1.0, "w_elo": 1.0, "conf_offset": {}},
                {"label": "V2", "w_futi": 1.0, "w_elo": 0.0, "conf_offset": {}},
            ]), encoding="utf-8")
            sv._CACHE.clear()
            results = sv.tuned_predict(self.m, a, b, config_path=p)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "V1")
        self.assertEqual(results[1].source, "V2")
        # Futi-only (V2) should give different probabilities than 50/50 (V1)
        self.assertNotAlmostEqual(results[0].p_a, results[1].p_a, places=3)


if __name__ == "__main__":
    unittest.main()
