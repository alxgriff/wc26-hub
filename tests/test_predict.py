"""Unit tests for scripts/predict.py.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import predict as pr


def mk(name, strength, z_att=0.0, z_def=0.0):
    """Synthetic TeamRating; only strength + att/def z-scores drive the math."""
    return pr.TeamRating(
        team=name, elo=strength, futi=70.0, attack=70.0, defense=70.0,
        strength=strength, z_att=z_att, z_def=z_def,
        elo_rank=1, futi_rank=1, consensus_rank=1,
        opta_advance=None, opta_wincup=None, opta_rank=None,
        market_odds=None, market_implied=None, market_rank=None,
    )


def model(*teams, **cfg):
    return pr.RatingModel({t.team: t for t in teams}, pr.Config(**cfg), "2026-06-11")


class MatchModelTests(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        m = model(mk("A", 1900, 0.5, -0.2), mk("B", 1650, -0.3, 0.4))
        for ha in (None, "A", "B"):
            p = pr.predict_match(m, "A", "B", hfa_team=ha)
            self.assertAlmostEqual(p.p_a + p.p_draw + p.p_b, 1.0, places=6)

    def test_equal_strength_neutral_is_symmetric(self):
        m = model(mk("A", 1800), mk("B", 1800))
        p = pr.predict_match(m, "A", "B")
        self.assertAlmostEqual(p.p_a, p.p_b, places=9)
        self.assertAlmostEqual(p.total, m.config.mu0, places=9)   # zero texture
        self.assertGreater(p.p_draw, 0.20)

    def test_stronger_team_is_favored(self):
        m = model(mk("Strong", 2050), mk("Weak", 1500))
        p = pr.predict_match(m, "Strong", "Weak")
        self.assertGreater(p.p_a, p.p_b)
        self.assertGreater(p.p_a, 0.6)
        self.assertGreater(p.lambda_a, p.lambda_b)

    def test_home_advantage_helps_the_host(self):
        m = model(mk("A", 1800), mk("B", 1800))
        neutral = pr.predict_match(m, "A", "B")
        home = pr.predict_match(m, "A", "B", hfa_team="A")
        self.assertGreater(home.p_a, neutral.p_a)
        self.assertLess(home.p_b, neutral.p_b)

    def test_swap_is_mirror_image(self):
        m = model(mk("A", 1950, 0.4, -0.1), mk("B", 1700, -0.2, 0.3))
        ab = pr.predict_match(m, "A", "B")
        ba = pr.predict_match(m, "B", "A")
        self.assertAlmostEqual(ab.p_a, ba.p_b, places=9)
        self.assertAlmostEqual(ab.p_b, ba.p_a, places=9)
        self.assertAlmostEqual(ab.lambda_a, ba.lambda_b, places=9)
        self.assertAlmostEqual(ab.total, ba.total, places=9)

    def test_totals_track_attack_defense(self):
        # both sides attack-heavy / defence-light -> high total; the reverse -> low
        hi = model(mk("A", 1800, 1.5, -1.5), mk("B", 1800, 1.5, -1.5))
        lo = model(mk("C", 1800, -1.5, 1.5), mk("D", 1800, -1.5, 1.5))
        ph = pr.predict_match(hi, "A", "B")
        pl = pr.predict_match(lo, "C", "D")
        self.assertGreater(ph.total, pl.total + 1.0)
        self.assertGreater(ph.over[2.5], pl.over[2.5])
        self.assertGreater(ph.btts, pl.btts)

    def test_over_and_btts_are_probabilities(self):
        m = model(mk("A", 1900, 0.3, 0.1), mk("B", 1700, -0.2, -0.1))
        p = pr.predict_match(m, "A", "B")
        for v in (*p.over.values(), p.btts):
            self.assertTrue(0.0 <= v <= 1.0)
        self.assertGreaterEqual(p.over[1.5], p.over[2.5])   # monotone in the line
        self.assertGreaterEqual(p.over[2.5], p.over[3.5])

    def test_unknown_team_raises(self):
        m = model(mk("A", 1800), mk("B", 1800))
        with self.assertRaises(ValueError):
            pr.predict_match(m, "A", "Nobody")

    def test_dnb_is_conditional_win_probability(self):
        m = model(mk("A", 1900), mk("B", 1700))
        p = pr.predict_match(m, "A", "B")
        self.assertAlmostEqual(p.dnb_a, p.p_a / (p.p_a + p.p_b), places=9)
        self.assertGreater(p.dnb_a, 0.5)

    def test_theta_calibrated_to_elo_expectancy_curve(self):
        """Regression-lock the June 12 calibration: the model's win-plus-half-draw
        expectancy must track 1/(1+10^(-gap/400)) within 2.5pp at realistic gaps."""
        for gap in (100, 200, 300, 400):
            m = model(mk("A", 1800 + gap), mk("B", 1800))
            p = pr.predict_match(m, "A", "B")
            expect = 1 / (1 + 10 ** (-gap / 400))
            self.assertAlmostEqual(p.p_a + 0.5 * p.p_draw, expect, delta=0.025,
                                   msg=f"miscalibrated at gap {gap}")


class OverlayTests(unittest.TestCase):
    def _overlay_file(self, dirpath, rows):
        p = Path(dirpath) / "Opta_Match_Predictions.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["match_id", "p_home", "p_draw", "p_away", "source", "asof"])
            w.writerows(rows)
        return p

    def test_percent_and_fraction_inputs_both_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._overlay_file(d, [
                ["B1", 52.7, 25.3, 22.0, "opta", "2026-06-12"],     # percent
                ["D1", 0.396, 0.266, 0.338, "opta", "2026-06-12"],  # fraction
            ])
            ov = pr.load_match_overlay(p)
        for mid in ("B1", "D1"):
            self.assertAlmostEqual(sum((ov[mid]["p_home"], ov[mid]["p_draw"],
                                        ov[mid]["p_away"])), 1.0, places=3)
        self.assertAlmostEqual(ov["B1"]["p_home"], 0.527, places=4)

    def test_contract_violation_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._overlay_file(d, [["B1", 60.0, 25.0, 22.0, "opta", "x"]])  # 107
            with self.assertRaises(ValueError):
                pr.load_match_overlay(p)

    def test_missing_file_is_noop(self):
        self.assertEqual(pr.load_match_overlay(Path("nope") / "missing.csv"), {})

    def test_blend_is_equal_weight_average_and_sums_to_one(self):
        m = model(mk("A", 1800), mk("B", 1800))
        p = pr.predict_match(m, "A", "B")           # symmetric model
        row = {"p_home": 0.60, "p_draw": 0.20, "p_away": 0.20, "source": "s", "asof": ""}
        pa, pd, pb = pr.blend_wdl(p, row)
        self.assertAlmostEqual(pa + pd + pb, 1.0, places=9)
        self.assertAlmostEqual(pa, (p.p_a + 0.60) / 2, places=6)
        self.assertGreater(pa, p.p_a)               # pulled toward the source
        self.assertGreater(pa, pb)

    def test_render_shows_consensus_and_both_sources(self):
        m = model(mk("A", 1800), mk("B", 1800))
        p = pr.predict_match(m, "A", "B")
        row = {"p_home": 0.527, "p_draw": 0.253, "p_away": 0.22,
               "source": "Opta supercomputer", "asof": "2026-06-12"}
        out = pr.render_prediction(m, p, overlay_row=row)
        self.assertIn("**Consensus:**", out)
        self.assertIn("Opta supercomputer", out)
        self.assertIn("our model", out)


class RealDataTests(unittest.TestCase):
    """Integration against the committed verified ratings."""
    @classmethod
    def setUpClass(cls):
        cls.m = pr.load_ratings()

    def test_loads_all_48_teams_from_verified_elo(self):
        self.assertEqual(len(self.m.teams), 48)
        self.assertEqual(self.m.asof, "2026-06-11")
        # the corrected Elo is in use: Morocco is strong, not 45th
        self.assertLess(self.m.teams["Morocco"].consensus_rank, 20)

    def test_consensus_orders_strong_over_weak(self):
        s = self.m.teams
        self.assertGreater(s["Spain"].strength, s["Qatar"].strength)
        self.assertGreater(s["Argentina"].strength, s["Curaçao"].strength)

    def test_a_real_matchup_sums_to_one_within_contract_tolerance(self):
        p = pr.predict_match(self.m, "Spain", "Cape Verde")
        self.assertAlmostEqual(p.p_a + p.p_draw + p.p_b, 1.0, delta=0.001)
        self.assertGreater(p.p_a, 0.6)

    def test_outputs_write_expected_schema(self):
        with tempfile.TemporaryDirectory() as d:
            rcsv = Path(d) / "ratings.csv"
            tcsv = Path(d) / "team_strength.csv"
            pr.write_ratings_csv(self.m, rcsv)
            pr.write_team_strength_csv(self.m, tcsv)
            with rcsv.open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 48)
            self.assertEqual(list(rows[0].keys()), ["team", "rating", "source", "asof"])
            with tcsv.open(encoding="utf-8-sig") as f:
                trows = list(csv.DictReader(f))
            self.assertEqual(len(trows), 48)
            self.assertIn("attack", trows[0])
            self.assertIn("market_implied_pct", trows[0])
            self.assertIn("sources_diverge", trows[0])
            self.assertNotIn("zeileis_rank", trows[0])   # Zeileis fully retired

    def test_real_market_loaded_and_sane(self):
        spain = self.m.teams["Spain"]
        haiti = self.m.teams["Haiti"]
        self.assertIsNotNone(spain.market_rank)
        self.assertEqual(spain.market_rank, 1)               # market favourite
        self.assertLess(spain.market_odds, haiti.market_odds)
        # de-vigged implied probabilities are a probability measure
        total = sum(t.market_implied for t in self.m.teams.values()
                    if t.market_implied is not None)
        self.assertAlmostEqual(total, 100.0, delta=0.1)
        # tie blocks share a rank (competition ranking)
        odds_groups = {}
        for t in self.m.teams.values():
            if t.market_odds is not None:
                odds_groups.setdefault(t.market_odds, set()).add(t.market_rank)
        for odds, ranks in odds_groups.items():
            self.assertEqual(len(ranks), 1, f"tied odds {odds} got multiple ranks {ranks}")


if __name__ == "__main__":
    unittest.main()
