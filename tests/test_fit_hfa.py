"""Tests for fit_hfa.py (home-advantage measurement) and the per-host HFA table.

The host table must REPRODUCE the flat behaviour when unset (regression guard, per
the spec's acceptance criterion) and override per host when set; the loader must
apply a persisted hfa/hfa_by_host.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fit_hfa as fh    # noqa: E402
import predict as pr    # noqa: E402


def _toy(cfg=None):
    cfg = cfg or pr.Config()

    def tr(name):
        return pr.TeamRating(team=name, elo=1800.0, futi=1800.0, attack=0.0, defense=0.0,
                             strength=1800.0, z_att=0.0, z_def=0.0, elo_rank=1, futi_rank=1,
                             consensus_rank=1, opta_advance=None, opta_wincup=None,
                             opta_rank=None, market_odds=None, market_implied=None,
                             market_rank=None)
    return pr.RatingModel({"A": tr("A"), "B": tr("B")}, cfg, "test")


class HfaToEloTests(unittest.TestCase):
    def test_zero_edge_is_zero_elo(self):
        self.assertAlmostEqual(fh.hfa_to_elo(0.0, 190.0), 0.0, places=9)

    def test_positive_edge_roundtrips_through_the_model(self):
        h = fh.hfa_to_elo(0.455, 190.0)
        self.assertGreater(h, 0.0)
        p = pr.predict_match(_toy(pr.Config(hfa=h)), "A", "B", hfa_team="A")
        self.assertAlmostEqual(p.lambda_a - p.lambda_b, 0.455, places=2)  # ~the input edge


class OlsTests(unittest.TestCase):
    def test_recovers_constant_intercept(self):
        X = [[1.0, g] for g in (-0.5, 0.0, 0.5, -0.2, 0.3)]
        Y = [1.0] * 5                       # constant home GD -> intercept 1, slope 0
        b = fh._ols(X, Y)
        self.assertAlmostEqual(b[0], 1.0, places=6)


class HostTableTests(unittest.TestCase):
    def test_none_table_reproduces_flat(self):     # spec acceptance: flat 60 reproduced
        flat = pr.predict_match(_toy(pr.Config(hfa=60.0)), "A", "B", hfa_team="A")
        tbl = pr.predict_match(_toy(pr.Config(hfa=60.0, hfa_by_host={"A": 60.0, "B": 60.0})),
                               "A", "B", hfa_team="A")
        self.assertAlmostEqual(flat.p_a, tbl.p_a, places=12)

    def test_per_host_value_overrides_flat(self):
        low = pr.predict_match(_toy(pr.Config(hfa=60.0)), "A", "B", hfa_team="A").p_a
        high = pr.predict_match(_toy(pr.Config(hfa=60.0, hfa_by_host={"A": 150.0})),
                                "A", "B", hfa_team="A").p_a
        self.assertGreater(high, low)              # bigger host bonus -> host more favoured

    def test_host_not_in_table_falls_back_to_flat(self):
        a = pr.predict_match(_toy(pr.Config(hfa=60.0)), "A", "B", hfa_team="A").p_a
        b = pr.predict_match(_toy(pr.Config(hfa=60.0, hfa_by_host={"Z": 150.0})),
                             "A", "B", hfa_team="A").p_a
        self.assertAlmostEqual(a, b, places=12)


class CalibrationHfaTests(unittest.TestCase):
    def test_calibration_applies_hfa_and_host_table(self):
        with tempfile.TemporaryDirectory() as d:
            cal = Path(d) / "calibration.json"
            cal.write_text(json.dumps({"hfa": 55.0, "hfa_by_host": {"Mexico": 70.0}}),
                           encoding="utf-8")
            orig = pr.CALIBRATION
            pr.CALIBRATION = cal
            try:
                m = pr.load_ratings()
                self.assertEqual(m.config.hfa, 55.0)
                self.assertEqual(m.config.hfa_by_host["Mexico"], 70.0)
            finally:
                pr.CALIBRATION = orig


if __name__ == "__main__":
    unittest.main()
