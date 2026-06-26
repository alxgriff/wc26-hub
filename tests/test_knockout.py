"""Tests for scripts/knockout.py — the knockout-stage data contract + loader."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import knockout as ko  # noqa: E402


def row(match_no, **kw):
    """A knockout CSV row dict with blank defaults; override via kwargs."""
    base = {
        "match_no": str(match_no), "round": "", "date_et": "2026-06-28",
        "kickoff_et_24h": "15:00", "kickoff_et": "3:00 PM", "stadium": "",
        "city": "", "country": "", "tv_us": "", "team_a": "", "team_b": "",
        "score_a": "", "score_b": "", "decided_by": "", "winner": "",
        "status": "scheduled", "notes": "",
    }
    base.update({k: ("" if v is None else str(v)) for k, v in kw.items()})
    return base


def parse_one(match_no, **kw):
    return ko.parse_knockout([row(match_no, **kw)])[0]


class RoundOfTests(unittest.TestCase):
    def test_boundaries(self):
        cases = {73: "R32", 88: "R32", 89: "R16", 96: "R16", 97: "QF",
                 100: "QF", 101: "SF", 102: "SF", 103: "3RD", 104: "Final"}
        for n, want in cases.items():
            self.assertEqual(ko.round_of(n), want, n)

    def test_out_of_range_raises(self):
        for n in (72, 105, 0, -1):
            with self.assertRaises(ValueError):
                ko.round_of(n)

    def test_match_number_space_matches_bracket(self):
        # the module-level assert already guards this; re-state it explicitly
        self.assertEqual(
            frozenset(range(73, 105)),
            frozenset(ko.bk.R32_TEMPLATE) | frozenset(ko.bk.BRACKET_TREE)
            | {ko.bk.THIRD_PLACE_MATCH})


class SlotLabelTests(unittest.TestCase):
    def test_r32_winner_runnerup_third(self):
        self.assertEqual(ko.slot_labels(73), ("Runner-up A", "Runner-up B"))
        self.assertEqual(ko.slot_labels(74), ("Winner E", "Best 3rd (A/B/C/D/F)"))
        self.assertEqual(ko.slot_labels(75), ("Winner F", "Runner-up C"))

    def test_later_rounds_name_feeders(self):
        self.assertEqual(ko.slot_labels(89), ("Winner M74", "Winner M77"))
        self.assertEqual(ko.slot_labels(104), ("Winner M101", "Winner M102"))

    def test_third_place_names_semi_losers(self):
        self.assertEqual(ko.slot_labels(103), ("Loser M101", "Loser M102"))


class ParseValidTests(unittest.TestCase):
    def test_scheduled_minimal(self):
        m = parse_one(73, status="scheduled")
        self.assertFalse(m.is_played)
        self.assertFalse(m.participants_known)
        self.assertIsNone(m.winner_team)
        self.assertEqual(m.round, "R32")          # filled from match_no

    def test_scheduled_with_resolved_participants(self):
        m = parse_one(73, team_a="France", team_b="Brazil", status="scheduled")
        self.assertTrue(m.participants_known)
        self.assertIsNone(m.winner_team)           # known but not played

    def test_played_regulation(self):
        m = parse_one(73, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                      decided_by="regulation", winner="A", status="played")
        self.assertTrue(m.is_played)
        self.assertEqual(m.winner_team, "France")
        self.assertEqual(m.loser_team, "Brazil")

    def test_played_extra_time(self):
        m = parse_one(81, team_a="Spain", team_b="Italy", score_a=2, score_b=1,
                      decided_by="extra_time", winner="A", status="played")
        self.assertEqual(m.winner_team, "Spain")

    def test_played_penalties_winner_is_pen_side(self):
        m = parse_one(73, team_a="France", team_b="Brazil", score_a=1, score_b=1,
                      decided_by="penalties", winner="B", status="played")
        self.assertEqual(m.winner_team, "Brazil")   # level after play, won the shootout
        self.assertEqual(m.loser_team, "France")

    def test_stadium_in_canon_ok(self):
        m = parse_one(73, stadium="MetLife Stadium", status="scheduled")
        self.assertEqual(m.stadium, "MetLife Stadium")


class ParseRejectTests(unittest.TestCase):
    def _bad(self, **kw):
        with self.assertRaises(ValueError):
            parse_one(**kw)

    def test_scheduled_with_score(self):
        self._bad(match_no=73, score_a=1, status="scheduled")

    def test_scheduled_with_winner(self):
        self._bad(match_no=73, winner="A", status="scheduled")

    def test_played_level_must_be_penalties(self):
        self._bad(match_no=73, team_a="France", team_b="Brazil", score_a=1, score_b=1,
                  decided_by="regulation", winner="A", status="played")

    def test_played_unequal_cannot_be_penalties(self):
        self._bad(match_no=73, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                  decided_by="penalties", winner="A", status="played")

    def test_played_winner_contradicts_score(self):
        self._bad(match_no=73, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                  decided_by="regulation", winner="B", status="played")

    def test_played_without_participants(self):
        self._bad(match_no=73, score_a=2, score_b=1, decided_by="regulation",
                  winner="A", status="played")

    def test_played_without_winner(self):
        self._bad(match_no=73, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                  decided_by="regulation", status="played")

    def test_played_without_decided_by(self):
        self._bad(match_no=73, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                  winner="A", status="played")

    def test_out_of_range_match_no(self):
        self._bad(match_no=72, status="scheduled")

    def test_non_integer_match_no(self):
        self._bad(match_no="A1", status="scheduled")

    def test_round_inconsistent_with_match_no(self):
        self._bad(match_no=73, round="R16", status="scheduled")

    def test_unknown_stadium_rejected(self):
        self._bad(match_no=73, stadium="Wembley Stadium", status="scheduled")

    def test_same_team_both_sides(self):
        self._bad(match_no=73, team_a="France", team_b="France", status="scheduled")

    def test_duplicate_match_no(self):
        with self.assertRaises(ValueError):
            ko.parse_knockout([row(73), row(73)])


class HelperTests(unittest.TestCase):
    def test_results_dict_only_played_with_winner(self):
        rows = [
            row(73, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                decided_by="regulation", winner="A", status="played"),
            row(74, team_a="Spain", team_b="Italy", score_a=1, score_b=1,
                decided_by="penalties", winner="B", status="played"),
            row(75, team_a="Germany", team_b="Japan", status="scheduled"),
        ]
        matches = ko.parse_knockout(rows)
        self.assertEqual(ko.results_dict(matches), {73: "France", 74: "Italy"})

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(ko.load_knockout(Path(tempfile.gettempdir()) / "nope_ko_xyz.csv"), [])

    def test_write_then_load_round_trip(self):
        rows = [
            row(73, date_et="2026-06-28", kickoff_et_24h="15:00", kickoff_et="3:00 PM",
                stadium="SoFi Stadium", city="Inglewood", country="USA",
                team_a="France", team_b="Brazil", score_a=2, score_b=1,
                decided_by="extra_time", winner="A", status="played", notes="thriller"),
            row(104, stadium="MetLife Stadium", city="East Rutherford", country="USA",
                status="scheduled"),
        ]
        matches = ko.parse_knockout(rows)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "knockout.csv"
            ko.write_knockout(p, matches)
            again = ko.load_knockout(p)
        self.assertEqual(len(again), 2)
        m73 = ko.by_no(again)[73]
        self.assertEqual((m73.team_a, m73.team_b, m73.score_a, m73.score_b,
                          m73.decided_by, m73.winner, m73.status, m73.notes),
                         ("France", "Brazil", 2, 1, "extra_time", "A", "played", "thriller"))
        self.assertEqual(ko.by_no(again)[104].status, "scheduled")


def _locked(home, away):
    return {"home": home, "away": away, "home_provisional": False, "away_provisional": False}


def _proj(overrides):
    """A minimal bracket.project-shaped dict: all 16 R32 slots blank/provisional by
    default, plus the real winner-of tree, with `overrides` merged onto named R32 slots."""
    r32 = {m: {"home": None, "away": None, "home_provisional": True,
               "away_provisional": True, "match": m} for m in range(73, 89)}
    for m, e in overrides.items():
        r32[m].update(e)
    return {"r32": r32, "tree": {m: list(p) for m, p in ko.bk.BRACKET_TREE.items()},
            "third_place_match": ko.bk.THIRD_PLACE_MATCH, "warnings": []}


class MaterializeTeamsTests(unittest.TestCase):
    def test_r32_locked_sides_fill(self):
        proj = _proj({73: _locked("France", "Brazil")})
        out = ko.materialize_teams(proj, ko.parse_knockout([row(73)]))
        m = ko.by_no(out)[73]
        self.assertEqual((m.team_a, m.team_b), ("France", "Brazil"))

    def test_r32_provisional_side_stays_blank(self):
        # home locked, away a concrete-but-unsealed team -> only the locked side is written
        proj = _proj({74: {"home": "Spain", "away": "Portugal",
                           "home_provisional": False, "away_provisional": True}})
        out = ko.materialize_teams(proj, ko.parse_knockout([row(74)]))
        m = ko.by_no(out)[74]
        self.assertEqual((m.team_a, m.team_b), ("Spain", ""))

    def test_r32_unstarted_blank(self):
        out = ko.materialize_teams(_proj({}), ko.parse_knockout([row(75)]))
        m = ko.by_no(out)[75]
        self.assertEqual((m.team_a, m.team_b), ("", ""))

    def test_r16_fills_from_played_feeders(self):
        # M89 = winners of M74 & M77; both feeders played -> M89's participants resolve
        proj = _proj({74: _locked("France", "Brazil"), 77: _locked("Spain", "Italy")})
        rows = [
            row(74, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                decided_by="regulation", winner="A", status="played"),
            row(77, team_a="Spain", team_b="Italy", score_a=2, score_b=0,
                decided_by="regulation", winner="A", status="played"),
            row(89),
        ]
        out = ko.materialize_teams(proj, ko.parse_knockout(rows))
        m89 = ko.by_no(out)[89]
        self.assertEqual((m89.team_a, m89.team_b), ("France", "Spain"))

    def test_r16_one_feeder_unplayed_stays_blank(self):
        proj = _proj({74: _locked("France", "Brazil"), 77: _locked("Spain", "Italy")})
        rows = [
            row(74, team_a="France", team_b="Brazil", score_a=2, score_b=1,
                decided_by="regulation", winner="A", status="played"),
            row(89),
        ]
        out = ko.materialize_teams(proj, ko.parse_knockout(rows))
        self.assertEqual((ko.by_no(out)[89].team_a, ko.by_no(out)[89].team_b), ("", ""))

    def test_played_rows_untouched(self):
        # a played row keeps its recorded participants even if the projection disagrees
        proj = _proj({73: _locked("Germany", "Argentina")})
        rows = [row(73, team_a="France", team_b="Brazil", score_a=1, score_b=0,
                    decided_by="regulation", winner="A", status="played")]
        out = ko.materialize_teams(proj, ko.parse_knockout(rows))
        m = ko.by_no(out)[73]
        self.assertEqual((m.team_a, m.team_b, m.winner_team), ("France", "Brazil", "France"))


class EnterResultTests(unittest.TestCase):
    def _resolved(self):
        return ko.parse_knockout([row(73, team_a="France", team_b="Brazil")])

    def test_decisive_infers_winner_and_regulation(self):
        upd, msg = ko.enter_ko_result(self._resolved(), 73, 2, 1)
        m = ko.by_no(upd)[73]
        self.assertEqual((m.status, m.decided_by, m.winner, m.winner_team),
                         ("played", "regulation", "A", "France"))
        self.assertIn("France advance", msg)

    def test_extra_time_preserved(self):
        upd, _ = ko.enter_ko_result(self._resolved(), 73, 2, 1, decided_by="extra_time")
        self.assertEqual(ko.by_no(upd)[73].decided_by, "extra_time")

    def test_level_requires_penalties_and_winner(self):
        with self.assertRaises(ValueError):
            ko.enter_ko_result(self._resolved(), 73, 1, 1)                       # nothing set
        with self.assertRaises(ValueError):
            ko.enter_ko_result(self._resolved(), 73, 1, 1, decided_by="regulation", winner="A")
        upd, _ = ko.enter_ko_result(self._resolved(), 73, 1, 1,
                                    decided_by="penalties", winner="B")
        self.assertEqual(ko.by_no(upd)[73].winner_team, "Brazil")

    def test_winner_contradicting_score_rejected(self):
        with self.assertRaises(ValueError):
            ko.enter_ko_result(self._resolved(), 73, 2, 1, winner="B")

    def test_overwrite_refused_without_force(self):
        played, _ = ko.enter_ko_result(self._resolved(), 73, 2, 1)
        with self.assertRaises(ValueError):
            ko.enter_ko_result(played, 73, 1, 1, decided_by="penalties", winner="A")
        again, _ = ko.enter_ko_result(played, 73, 3, 0, force=True)
        self.assertEqual(ko.by_no(again)[73].score_a, 3)

    def test_unresolved_participants_rejected(self):
        with self.assertRaises(ValueError):
            ko.enter_ko_result(ko.parse_knockout([row(73)]), 73, 2, 1)

    def test_unknown_match_no_rejected(self):
        with self.assertRaises(ValueError):
            ko.enter_ko_result(self._resolved(), 999, 1, 0)


class FetchKoMatcherTests(unittest.TestCase):
    def test_matches_resolved_tie_by_team_set_order_independent(self):
        import fetch_ko_results as fk
        matches = ko.parse_knockout([row(73, team_a="France", team_b="Brazil"), row(74)])
        km = fk._match_ko_event({"home_team": "Brazil", "away_team": "France",
                                 "completed": True}, matches)
        self.assertEqual(km.match_no, 73)

    def test_unresolved_or_foreign_event_returns_none(self):
        import fetch_ko_results as fk
        matches = ko.parse_knockout([row(73, team_a="France", team_b="Brazil"), row(74)])
        self.assertIsNone(fk._match_ko_event(
            {"home_team": "Spain", "away_team": "Italy"}, matches))     # not a tie here
        self.assertIsNone(fk._match_ko_event(
            {"home_team": "Winner E", "away_team": "x"}, matches))       # 74 unresolved


class CommittedScheduleTests(unittest.TestCase):
    """Smoke-guard the committed data/knockout.csv stays contract-valid all tournament."""
    def test_live_file_loads_and_is_complete(self):
        warns: list = []
        matches = ko.load_knockout(ko.KNOCKOUT_CSV, warns)
        if not matches:
            self.skipTest("no knockout.csv committed yet")
        self.assertEqual(len(matches), 32)
        self.assertEqual(sorted(m.match_no for m in matches), list(range(73, 105)))
        self.assertEqual(warns, [])                       # schedule fully populated
        for m in matches:                                 # every venue joins the Sweat-Factor canon
            self.assertIn(m.stadium, ko._venue_canon())


if __name__ == "__main__":
    unittest.main()
