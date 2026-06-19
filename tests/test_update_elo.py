"""Tests for the nightly Elo roll-forward (scripts/update_elo.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_elo as U


class FormulaTests(unittest.TestCase):
    def test_gd_multiplier_steps(self):
        self.assertEqual(U.gd_mult(0), 1.0)
        self.assertEqual(U.gd_mult(1), 1.0)
        self.assertEqual(U.gd_mult(2), 1.5)
        self.assertEqual(U.gd_mult(3), 1.75)
        self.assertAlmostEqual(U.gd_mult(5), 1.75 + 2 / 8.0)   # +1/8 per goal past 3

    def test_expected_symmetry_and_bounds(self):
        self.assertAlmostEqual(U.expected(1500, 1500), 0.5)
        self.assertAlmostEqual(U.expected(1900, 1500) + U.expected(1500, 1900), 1.0, places=9)
        self.assertGreater(U.expected(1800, 1500), 0.5)

    def test_delta_is_zero_sum(self):
        # a win for A over an equal B: A gains exactly what B loses
        ra = rb = 1500.0
        delta = U.K_WC * U.gd_mult(1) * (1.0 - U.expected(ra, rb))
        self.assertAlmostEqual(delta, U.K_WC * 0.5)            # K*(1-0.5) at even strength


class RollTests(unittest.TestCase):
    """Integration against the committed VERIFIED baseline + fixtures.csv."""
    @classmethod
    def setUpClass(cls):
        cls.base, cls.elo, cls.n, cls.last = U.roll()

    def test_rolls_some_played_games(self):
        self.assertGreater(self.n, 0)

    def test_all_48_teams_present(self):
        self.assertEqual(len(self.elo), len(self.base))
        self.assertEqual(set(self.elo), set(self.base))

    def test_total_elo_conserved(self):
        # every game moves +d/-d, so the field total is invariant (host edge doesn't break it)
        self.assertAlmostEqual(sum(self.elo.values()), sum(self.base.values()), places=4)

    def test_md1_winner_rose(self):
        # USA beat Paraguay 4-1 in MD1 -> USA up, Paraguay down vs the baseline
        self.assertGreater(self.elo["United States"], self.base["United States"])
        self.assertLess(self.elo["Paraguay"], self.base["Paraguay"])

    def test_as_of_cutoff_is_leak_free(self):
        # rolling only through games before the tournament's first ET date applies fewer games
        _, _, n_early, _ = U.roll(as_of="2026-06-12")
        self.assertLess(n_early, self.n)

    def test_roll_is_deterministic(self):
        _, elo2, n2, _ = U.roll()
        self.assertEqual(n2, self.n)
        self.assertEqual(elo2, self.elo)


if __name__ == "__main__":
    unittest.main()
