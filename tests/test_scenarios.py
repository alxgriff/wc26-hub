"""Unit tests for scripts/scenarios.py.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import scenarios as sc
import standings as st


def mk(mid, team_a, team_b, score_a=None, score_b=None):
    played = score_a is not None
    return st.Match(mid, mid[0], (int(mid[1]) + 1) // 2, team_a, team_b,
                    score_a, score_b, "played" if played else "scheduled")


def counts(report, team):
    return next(ts.counts for ts in report.teams if ts.team == team)


# A Group A entering MD3. MD1+MD2 played; MD3 = {A5: L vs M, A6: B vs D}.
#   L beat B and D -> 6 pts (+4) ;  M beat B and D -> 6 pts (+2)
#   B and D lost both -> 0 pts (-3 each)
# L and M are out of reach for B/D (6 pts vs a max of 3), so:
#   (a) L and M are locked top-2 in all 9 combinations,
#   (c) B and D can never reach the top two in any combination,
#   (b) the B-D draw leaves them level on points AND goal difference (both drew,
#       so GD is known and equal) -> a genuine margin-dependent 3rd/4th split.
GROUP_A = [
    mk("A1", "L", "B", 2, 0),
    mk("A2", "L", "D", 2, 0),
    mk("A3", "M", "B", 1, 0),
    mk("A4", "M", "D", 1, 0),
    mk("A5", "L", "M"),          # MD3, unplayed
    mk("A6", "B", "D"),          # MD3, unplayed
]


class ScenarioCoreTests(unittest.TestCase):
    def setUp(self):
        self.report = sc.enumerate_scenarios("A", GROUP_A)

    def test_nine_combinations(self):
        self.assertEqual(self.report.n_combos, 9)
        self.assertEqual(len(self.report.unplayed), 2)

    def test_a_leader_is_locked_top_two_in_all_nine(self):
        # 'through' now spans the exact seeds: first + second + seed-TBD top2 = all 9.
        for t in ("L", "M"):
            c = counts(self.report, t)
            self.assertEqual(c["first"] + c["second"] + c["top2"], 9)
        # the 1st-vs-2nd nuance is populated: L wins the group unless it loses to M.
        lc = counts(self.report, "L")
        self.assertGreater(lc["first"], 0)
        self.assertGreater(lc["second"], 0)

    def test_c_trailers_can_never_reach_top_two(self):
        # "eliminated" = out of the top two in every combo (a side can always win
        # to at least tie for 3rd, so nobody is ever *guaranteed* 4th).
        for t in ("B", "D"):
            c = counts(self.report, t)
            self.assertEqual(c["first"] + c["second"] + c["top2"], 0)

    def test_b_margin_dependent_combo_exists(self):
        # B and D both drawing leaves them level on points and (known) GD -> margin.
        self.assertGreater(counts(self.report, "B")["margin"], 0)
        self.assertGreater(counts(self.report, "D")["margin"], 0)

    def test_counts_each_sum_to_nine(self):
        for ts in self.report.teams:
            self.assertEqual(sum(ts.counts.values()), 9)


class GdResolutionTests(unittest.TestCase):
    """A points-tie is resolved only when goal-difference intervals are disjoint."""

    def _two_team_tail(self, b_gd_setup, d_gd_setup):
        # L, M dominate (top two). B and D enter level on points; their CURRENT
        # goal differences are set by the chosen MD2 scorelines so we can probe
        # whether a draw-draw tail is decided (disjoint GD) or margin-dependent.
        matches = [
            mk("A1", "L", "B", 5, 0), mk("A2", "L", "D", 5, 0),
            mk("A3", "M", "B", *b_gd_setup), mk("A4", "M", "D", *d_gd_setup),
            mk("A5", "L", "M"), mk("A6", "B", "D"),
        ]
        return sc.enumerate_scenarios("A", matches)

    def test_draw_draw_with_unequal_known_gd_is_decided(self):
        # B loses 0-0? no: give B a better current GD than D via MD2 margins.
        # B lost 1-2 to M (GD contribution -1), D lost 0-3 to M (-3); both also
        # lost 0-5 to L. So current GD: B = -6, D = -8. A B-D DRAW keeps both GDs
        # known and unequal -> B is decidably 3rd, D 4th (not margin-dependent).
        report = self._two_team_tail((2, 1), (3, 0))   # M 2-1 B, M 3-0 D
        b, d = counts(report, "B"), counts(report, "D")
        # In the three B-D draw combos, B should be a clean 3rd and D a clean 4th.
        self.assertEqual(b["margin"], 0)
        self.assertEqual(d["margin"], 0)
        self.assertGreaterEqual(b["third"], 3)
        self.assertGreaterEqual(d["out"], 3)

    def test_draw_draw_with_equal_gd_is_margin_dependent(self):
        # Symmetric scorelines -> B and D enter on identical GD. A B-D draw then
        # ties them on points AND GD -> GF would decide, which is unknown -> margin.
        report = self._two_team_tail((1, 0), (1, 0))   # M 1-0 B, M 1-0 D
        self.assertGreater(counts(report, "B")["margin"], 0)
        self.assertGreater(counts(report, "D")["margin"], 0)


class SeedUndecidedTests(unittest.TestCase):
    """The 1st-vs-2nd split: two teams through but with the seed still on goal
    difference land in the 'top2 (seed TBD)' bucket, not 'first'/'second'."""

    def test_top2_seed_tbd_when_two_leaders_finish_level(self):
        # P and Q draw their head-to-head (MD1) and enter MD3 level on points AND GD.
        # In the combo where both win their MD3 game they finish level on 7 pts with
        # overlapping GD -> through, but 1st-vs-2nd unresolved -> the seed-TBD bucket.
        g = [
            mk("A1", "P", "Q", 0, 0), mk("A2", "R", "S", 0, 0),
            mk("A3", "P", "R", 1, 0), mk("A4", "Q", "S", 1, 0),
            mk("A5", "P", "S"), mk("A6", "Q", "R"),
        ]
        rep = sc.enumerate_scenarios("A", g)
        self.assertGreater(counts(rep, "P")["top2"], 0)
        self.assertGreater(counts(rep, "Q")["top2"], 0)
        for t in ("P", "Q", "R", "S"):           # buckets still partition the 9 combos
            self.assertEqual(sum(counts(rep, t).values()), 9)


class StakesAndRenderTests(unittest.TestCase):
    def setUp(self):
        self.report = sc.enumerate_scenarios("A", GROUP_A)

    def test_locked_leader_stakes_show_first_vs_second(self):
        stakes = next(ts.stakes for ts in self.report.teams if ts.team == "L")
        self.assertEqual(len(stakes), 3)                     # Win / Draw / Loss
        # always through, and the 1st-vs-2nd nuance now surfaces: every line names a
        # seed; L wins the group unless it loses to M (then runners-up).
        self.assertTrue(all(("1st" in s or "2nd" in s) for s in stakes))
        self.assertTrue(any("1st" in s for s in stakes))
        self.assertTrue(any("2nd" in s for s in stakes))

    def test_trailer_stakes_condition_on_the_other_game(self):
        stakes = next(ts.stakes for ts in self.report.teams if ts.team == "B")
        joined = " | ".join(stakes)
        self.assertIn("Win:", joined)
        self.assertIn("Loss:", joined)
        # B's draw outcome hinges on the L-M game being irrelevant but the result
        # is margin/elimination depending on its own + D's — at least one line
        # must reference goal difference or elimination.
        self.assertTrue(any("margin-dependent" in s or "eliminated" in s for s in stakes))

    def test_render_markdown_has_the_required_sections(self):
        md = sc.render_markdown(self.report)
        for heading in ("# Group A — Matchday 3 scenarios", "## Current table",
                        "## Where each team can finish", "## What each team needs"):
            self.assertIn(heading, md)
        self.assertIn("| Team | 1st | 2nd | Top 2 (seed TBD) | 3rd | Out (4th) | Margin |", md)


class CliTests(unittest.TestCase):
    def test_cli_runs_on_repo_fixtures(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sc.main(["A"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("Group A — Matchday 3 scenarios", text)
        self.assertIn("Third-place race (live)", text)

    def test_cli_rejects_bad_group(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = sc.main(["Z"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
