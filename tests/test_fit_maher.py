"""Tests for scripts/fit_maher.py — the Maher-form total fit + curve helpers.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import predict as pr
import fit_maher as fm


def mk(name, strength, z_att, z_def):
    return pr.TeamRating(team=name, elo=strength, futi=70.0, attack=70.0, defense=70.0,
                         strength=strength, z_att=z_att, z_def=z_def, elo_rank=1,
                         futi_rank=1, consensus_rank=1, opta_advance=None,
                         opta_wincup=None, opta_rank=None, market_odds=None,
                         market_implied=None, market_rank=None)


class TotalConsistencyTests(unittest.TestCase):
    """fit_maher._total MUST equal predict_match.total at the same params — the fit
    would optimise the wrong objective the moment these two drift apart."""

    CASES = [(1.5, 1.0, -1.0, -1.5, 1.0, 0.30, 2.45),   # the fitted params, a mismatch
             (0.3, 0.3, 0.3, 0.3, 1.0, 0.20, 2.6),      # symmetric: blend is a no-op
             (0.5, -0.2, -0.3, 0.4, 0.5, 0.25, 2.5),    # partial blend
             (0.0, 0.0, 0.0, 0.0, 0.0, 0.20, 2.6)]      # inert default

    def test_total_matches_predict_match(self):
        for za, zd, zba, zbd, w, al, mu in self.CASES:
            m = pr.RatingModel({"A": mk("A", 1900, za, zd), "B": mk("B", 1600, zba, zbd)},
                               pr.Config(maher_w=w, alpha=al, mu0=mu), "x")
            p = pr.predict_match(m, "A", "B")
            self.assertAlmostEqual(p.total, fm._total(mu, al, w, za - zbd, zba - zd),
                                   places=12, msg=f"{(za, zd, zba, zbd, w, al, mu)}")


class CurveHelperTests(unittest.TestCase):
    def test_bin_lo_floors_and_clamps(self):
        self.assertEqual(fm._bin_lo(0.50), 0.50)
        self.assertEqual(fm._bin_lo(0.547), 0.50)        # floors into its bin
        self.assertEqual(fm._bin_lo(0.55), 0.55)
        self.assertEqual(fm._bin_lo(0.999), 0.95)        # top bin
        self.assertEqual(fm._bin_lo(0.40), 0.50)         # clamps below 0.5

    def test_empirical_curve_bins_filters_and_rates(self):
        rows = ([{"e_fav": 0.52, "total": 2, "fav_goals": 1, "dog_goals": 1}] * 50
                + [{"e_fav": 0.93, "total": 5, "fav_goals": 5, "dog_goals": 0}] * 10)
        cur = fm.empirical_curve(rows)
        self.assertIn(0.50, cur)                          # 50 >= MIN_BIN kept
        self.assertNotIn(0.90, cur)                       # 10 < MIN_BIN dropped
        self.assertAlmostEqual(cur[0.50]["total"], 2.0)
        self.assertAlmostEqual(cur[0.50]["draw"], 1.0)    # all level
        self.assertAlmostEqual(cur[0.50]["fav_win"], 0.0)

    def test_model_total_curve_uses_total_formula(self):
        feats = [{"e_fav": 0.85, "h_a": 2.0, "h_b": -1.0}]
        c = fm.model_total_curve(feats, 2.45, 0.30, 1.0)
        lo = fm._bin_lo(0.85)                            # bin key via the same floor
        self.assertAlmostEqual(c[lo], fm._total(2.45, 0.30, 1.0, 2.0, -1.0))


if __name__ == "__main__":
    unittest.main()
