"""Unit tests for scripts/standings.py using synthetic results.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import csv
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import standings as st


def mk(mid, team_a, team_b, score_a=None, score_b=None):
    """Synthetic Match; played iff scores are given."""
    played = score_a is not None
    return st.Match(
        match_id=mid,
        group=mid[0],
        matchday=(int(mid[1]) + 1) // 2,
        team_a=team_a,
        team_b=team_b,
        score_a=score_a,
        score_b=score_b,
        status="played" if played else "scheduled",
    )


def order(standings, group):
    return [r.team for r in standings.groups[group].rows]


class BasicTableTests(unittest.TestCase):
    def test_stats_accumulate_and_points_gd_gf_order(self):
        # Hawks beat Crows 2-1, Owls beat Doves 1-0; rest scheduled.
        matches = [
            mk("H1", "Hawks", "Crows", 2, 1),
            mk("H2", "Owls", "Doves", 1, 0),
            mk("H3", "Hawks", "Owls"),
            mk("H4", "Crows", "Doves"),
            mk("H5", "Hawks", "Doves"),
            mk("H6", "Crows", "Owls"),
        ]
        s = st.compute_standings(matches)
        # Hawks and Owls both 3 pts, both GD +1, Hawks ahead on GF (2 v 1).
        # Crows and Doves both 0 pts and GD -1, Crows ahead on GF (1 v 0).
        self.assertEqual(order(s, "H"), ["Hawks", "Owls", "Crows", "Doves"])
        hawks = s.groups["H"].rows[0]
        self.assertEqual(
            (hawks.played, hawks.won, hawks.drawn, hawks.lost, hawks.gf, hawks.ga, hawks.gd, hawks.points),
            (1, 1, 0, 0, 2, 1, 1, 3),
        )
        self.assertEqual(s.played, 2)
        self.assertEqual(s.total, 6)

    def test_unstarted_group_is_all_zeros_alphabetical_no_notes(self):
        matches = [
            mk("I1", "Foo", "Bar"),
            mk("I2", "Baz", "Qux"),
            mk("I3", "Foo", "Baz"),
            mk("I4", "Bar", "Qux"),
            mk("I5", "Foo", "Qux"),
            mk("I6", "Bar", "Baz"),
        ]
        s = st.compute_standings(matches)
        self.assertEqual(order(s, "I"), ["Bar", "Baz", "Foo", "Qux"])
        self.assertTrue(all(r.played == 0 and r.points == 0 for r in s.groups["I"].rows))
        self.assertEqual(s.groups["I"].notes, [])


class TiebreakTests(unittest.TestCase):
    def test_two_way_head_to_head(self):
        # P and Q finish level on points (6), GD (+1) and GF (2);
        # P beat Q 1-0, so P finishes first.
        matches = [
            mk("D1", "P", "Q", 1, 0),
            mk("D2", "P", "R", 0, 1),
            mk("D3", "P", "S", 1, 0),
            mk("D4", "Q", "R", 1, 0),
            mk("D5", "Q", "S", 1, 0),
            mk("D6", "R", "S", 1, 1),
        ]
        s = st.compute_standings(matches)
        self.assertEqual(order(s, "D"), ["P", "Q", "R", "S"])
        self.assertTrue(any("head-to-head" in n for n in s.groups["D"].notes))

    def test_three_way_tie_resolved_by_recursive_head_to_head(self):
        # X, Y, Z all finish 6 pts, GD +1, GF 3. The three-team mini-table
        # drops X (mini GF 1 v 2 v 2) but leaves Y and Z level; re-applying
        # head-to-head between Y and Z alone resolves it (Y beat Z 2-1).
        matches = [
            mk("E1", "X", "Y", 1, 0),
            mk("E2", "Z", "W", 1, 0),
            mk("E3", "Y", "Z", 2, 1),
            mk("E4", "X", "W", 2, 1),
            mk("E5", "Z", "X", 1, 0),
            mk("E6", "Y", "W", 1, 0),
        ]
        s = st.compute_standings(matches)
        for r in s.groups["E"].rows[:3]:
            self.assertEqual((r.points, r.gd, r.gf), (6, 1, 3))
        self.assertEqual(order(s, "E"), ["Y", "Z", "X", "W"])

    def test_gd_outranks_gf_and_head_to_head(self):
        # U and V both finish on 4 pts. V has more goals scored (5 v 2) AND
        # won the head-to-head, but U's goal difference is better (+1 v -1),
        # and GD comes before both GF and head-to-head in the contract.
        matches = [
            mk("K1", "U", "V", 0, 1),
            mk("K2", "M", "N", 0, 1),
            mk("K3", "U", "M", 2, 0),
            mk("K4", "V", "N", 2, 4),
            mk("K5", "U", "N", 0, 0),
            mk("K6", "V", "M", 2, 2),
        ]
        s = st.compute_standings(matches)
        u, v = s.groups["K"].rows[1], s.groups["K"].rows[2]
        self.assertEqual((u.points, u.gd, u.gf), (4, 1, 2))
        self.assertEqual((v.points, v.gd, v.gf), (4, -1, 5))
        self.assertEqual(order(s, "K"), ["N", "U", "V", "M"])

    def test_points_outrank_gd(self):
        # E reaches 6 pts with GD -3; H has GD +3 but only 3 pts.
        matches = [
            mk("L1", "E", "F", 1, 0),
            mk("L2", "G", "H", 1, 0),
            mk("L3", "E", "G", 1, 0),
            mk("L4", "F", "H", 1, 0),
            mk("L5", "E", "H", 0, 5),
            mk("L6", "F", "G", 1, 0),
        ]
        s = st.compute_standings(matches)
        self.assertEqual(order(s, "L"), ["F", "E", "H", "G"])
        e, h = s.groups["L"].rows[1], s.groups["L"].rows[2]
        self.assertEqual((e.points, e.gd), (6, -3))
        self.assertEqual((h.points, h.gd), (3, 3))

    def test_fair_play_separates_when_head_to_head_cannot(self):
        # All six matches drawn 0-0: head-to-head is useless, fair play decides.
        matches = _all_draws("G", ["Alpha", "Beta", "Gamma", "Delta"])
        fp = {"Alpha": 0, "Beta": -3, "Gamma": -6, "Delta": -9}
        s = st.compute_standings(matches, fair_play=fp)
        self.assertEqual(order(s, "G"), ["Alpha", "Beta", "Gamma", "Delta"])
        self.assertTrue(any("fair play" in n for n in s.groups["G"].notes))

    def test_completed_dead_heat_flags_drawing_of_lots(self):
        matches = _all_draws("F", ["Foo", "Bar", "Baz", "Qux"])
        s = st.compute_standings(matches)
        self.assertEqual(order(s, "F"), ["Bar", "Baz", "Foo", "Qux"])  # alphabetical fallback
        self.assertTrue(any("lots" in n for n in s.groups["F"].notes))

    def test_mid_group_tie_is_flagged_provisional_not_lots(self):
        # One round played, two drawn games -> two separate 2-way ties at
        # 1 pt (the 1-1 pair and the 0-0 pair split on GF before any
        # tiebreak path runs).
        matches = [
            mk("J1", "Foo", "Bar", 1, 1),
            mk("J2", "Baz", "Qux", 0, 0),
            mk("J3", "Foo", "Baz"),
            mk("J4", "Bar", "Qux"),
            mk("J5", "Foo", "Qux"),
            mk("J6", "Bar", "Baz"),
        ]
        s = st.compute_standings(matches)
        notes = s.groups["J"].notes
        self.assertTrue(any("provisional" in n for n in notes))
        self.assertFalse(any("lots" in n for n in notes))


class ThirdPlaceTests(unittest.TestCase):
    def test_third_place_ranking_points_then_gd(self):
        matches = (
            _hierarchy_group("A", ["A1t", "A2t", "A3t", "A4t"], third_win=(2, 0))
            + _hierarchy_group("B", ["B1t", "B2t", "B3t", "B4t"], third_win=(3, 0))
            + _draw_third_group("C", ["C1t", "C2t", "C3t", "C4t"])
        )
        s = st.compute_standings(matches)
        self.assertEqual(order(s, "A")[2], "A3t")  # 3 pts, GD -2
        self.assertEqual(order(s, "B")[2], "B3t")  # 3 pts, GD -1
        self.assertEqual(order(s, "C")[2], "C3t")  # 4 pts
        self.assertEqual([r.team for r in s.third_place], ["C3t", "B3t", "A3t"])

    def test_malformed_group_excluded_from_thirds(self):
        # a group with the wrong team count must not feed the best-thirds cutline
        good = _hierarchy_group("A", ["A1t", "A2t", "A3t", "A4t"], third_win=(2, 0))
        bad = [st.Match("Z1", "Z", 1, "Z1t", "Z2t", 1, 0, "played"),
               st.Match("Z2", "Z", 1, "Z1t", "Z3t", 1, 0, "played"),
               st.Match("Z3", "Z", 1, "Z2t", "Z3t", 1, 0, "played")]  # only 3 teams
        s = st.compute_standings(good + bad)
        self.assertEqual([r.team for r in s.third_place], ["A3t"])     # Z's 3rd excluded
        self.assertTrue(any("malformed" in w for w in s.warnings))

    def test_cutline_marks_exactly_eight(self):
        # Nine groups whose third-placed teams all finish on 3 pts but with
        # strictly increasing GD (third_win n-0 -> GD n-4): position 8 is the
        # last ✅, position 9 the first team out.
        matches = []
        for n, g in enumerate("ABCDEFGHI", 1):
            matches += _hierarchy_group(g, [f"{g}1t", f"{g}2t", f"{g}3t", f"{g}4t"], third_win=(n, 0))
        s = st.compute_standings(matches)
        self.assertEqual(s.warnings, [])
        self.assertEqual([r.team for r in s.third_place],
                         [f"{g}3t" for g in "IHGFEDCBA"])
        lines = st.render_markdown(s).splitlines()
        row8 = next(l for l in lines if l.startswith("| 8 | B3t |"))
        row9 = next(l for l in lines if l.startswith("| 9 | A3t |"))
        self.assertTrue(row8.endswith("| ✅ |"))
        self.assertFalse(row9.endswith("| ✅ |"))

    def test_cutline_tie_on_all_criteria_flagged_for_lots(self):
        # Bottom two thirds (groups A and B) finish identical on every
        # criterion and straddle the cutline at positions 8/9: alphabetical
        # display order, but the lots flag must be raised.
        wins = {"A": 1, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5, "G": 6, "H": 7, "I": 8}
        matches = []
        for g, n in wins.items():
            matches += _hierarchy_group(g, [f"{g}1t", f"{g}2t", f"{g}3t", f"{g}4t"], third_win=(n, 0))
        s = st.compute_standings(matches)
        self.assertEqual([r.team for r in s.third_place[7:]], ["A3t", "B3t"])
        self.assertTrue(any("lots" in n for n in s.third_place_notes))

    def test_render_marks_qualifying_thirds(self):
        matches = (
            _hierarchy_group("A", ["A1t", "A2t", "A3t", "A4t"], third_win=(2, 0))
            + _hierarchy_group("B", ["B1t", "B2t", "B3t", "B4t"], third_win=(3, 0))
        )
        md = st.render_markdown(st.compute_standings(matches))
        self.assertIn("## Group A", md)
        self.assertIn("## Third-place ranking", md)
        self.assertIn("| 1 | B3t | B | 3 |", md)
        self.assertIn("✅", md)


class IntegrityTests(unittest.TestCase):
    def test_misspelled_team_creates_phantom_and_warning(self):
        # One fixture row missing the diacritic creates a phantom 5th team;
        # the table still renders but the warning must fire — it is the only
        # signal that the table is corrupted.
        matches = _hierarchy_group("F", ["Côte d'Ivoire", "F2t", "F3t", "F4t"], third_win=(2, 0))
        matches[2] = mk("F3", "Cote d'Ivoire", "F3t", 2, 0)
        s = st.compute_standings(matches)
        self.assertTrue(any("Group F: expected 4 teams, found 5" in w for w in s.warnings))
        self.assertEqual(len(s.groups["F"].rows), 5)  # ranking must not crash

    def test_unknown_fair_play_team_rejected(self):
        matches = _all_draws("G", ["Alpha", "Beta", "Gamma", "Delta"])
        with self.assertRaises(ValueError):
            st.compute_standings(matches, fair_play={"Alpa": -3})


class ParseTests(unittest.TestCase):
    HEADER = ("match_id,group,matchday,date_et,kickoff_et_24h,kickoff_et,team_a,team_b,"
              "stadium,city,country,tv_us,score_a,score_b,status,notes")

    def parse(self, *rows):
        text = "\n".join([self.HEADER, *rows])
        return st.parse_fixtures(csv.DictReader(io.StringIO(text)))

    def test_parse_played_and_scheduled_rows(self):
        matches = self.parse(
            "A1,A,1,2026-06-11,15:00,3:00 PM ET,Mexico,South Africa,Estadio Azteca,Mexico City,Mexico,Fox,2,0,played,",
            "A2,A,1,2026-06-11,22:00,10:00 PM ET,South Korea,Czechia,Estadio Akron,Guadalajara,Mexico,FS1,,,scheduled,",
        )
        a1, a2 = matches
        self.assertEqual((a1.team_a, a1.score_a, a1.score_b, a1.is_played), ("Mexico", 2, 0, True))
        self.assertEqual((a2.matchday, a2.score_a, a2.is_played), (1, None, False))

    def test_played_without_scores_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("A1,A,1,2026-06-11,15:00,3:00 PM ET,Mexico,South Africa,,,,,,,played,")

    def test_scheduled_with_scores_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("A1,A,1,2026-06-11,15:00,3:00 PM ET,Mexico,South Africa,,,,,1,0,scheduled,")

    def test_bad_match_id_and_duplicates_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("M1,M,1,,,,Foo,Bar,,,,,,,scheduled,")
        with self.assertRaises(ValueError):
            self.parse(
                "A1,A,1,,,,Foo,Bar,,,,,,,scheduled,",
                "A1,A,1,,,,Baz,Qux,,,,,,,scheduled,",
            )

    def test_matchday_inconsistent_with_match_id_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("A5,A,1,,,,Foo,Bar,,,,,,,scheduled,")


# ---------------------------------------------------------------- fixtures

def _all_draws(group, teams):
    a, b, c, d = teams
    pairs = [(a, b), (c, d), (a, c), (b, d), (a, d), (b, c)]
    return [mk(f"{group}{i}", x, y, 0, 0) for i, (x, y) in enumerate(pairs, 1)]


def _hierarchy_group(group, teams, third_win):
    """t1 beats everyone, t2 beats t3/t4, t3 beats t4 by `third_win`.

    Third team finishes on 3 pts having conceded 0-2 twice, so its GD is
    controlled by the third_win scoreline.
    """
    t1, t2, t3, t4 = teams
    wa, wb = third_win
    return [
        mk(f"{group}1", t1, t2, 1, 0),
        mk(f"{group}2", t3, t4, wa, wb),
        mk(f"{group}3", t1, t3, 2, 0),
        mk(f"{group}4", t2, t4, 1, 0),
        mk(f"{group}5", t1, t4, 1, 0),
        mk(f"{group}6", t2, t3, 2, 0),
    ]


def _draw_third_group(group, teams):
    """t1 sweeps; t2 and t3 draw each other and beat t4; t2 over t3 on GD.

    Third team (t3) finishes on 4 pts.
    """
    t1, t2, t3, t4 = teams
    return [
        mk(f"{group}1", t1, t2, 1, 0),
        mk(f"{group}2", t3, t4, 1, 0),
        mk(f"{group}3", t1, t3, 1, 0),
        mk(f"{group}4", t2, t4, 2, 0),
        mk(f"{group}5", t1, t4, 1, 0),
        mk(f"{group}6", t2, t3, 1, 1),
    ]


if __name__ == "__main__":
    unittest.main()
