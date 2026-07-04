"""Tests for scripts/fetch_ko_results.py — the ESPN shootout-winner pass.
No network: an injected opener returns canned ESPN scoreboard JSON. The contract under
test: a LEVEL knockout result is entered with the shootout winner read from ESPN's
explicit shootoutScore (never inferred from the 90'+ET score); a level tie ESPN shows
no tally for falls back to the manual-entry report; decisive ties the Odds API window
expired past are swept up, with AET read from ESPN's status."""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import knockout as ko              # noqa: E402
import fetch_ko_reg_scores as fk   # noqa: E402
import fetch_ko_results as fr      # noqa: E402

TODAY = date(2026, 7, 4)


def _tie(no=74, team_a="Germany", team_b="Paraguay", date_et="2026-06-29",
         stadium="Gillette Stadium", city="Foxborough"):
    """A resolved, unplayed knockout tie awaiting its result."""
    return ko.KnockoutMatch(no, ko.round_of(no), date_et, "16:30", "4:30 PM",
                            stadium, city, "USA", "", team_a, team_b,
                            None, None, "", "", "scheduled", "")


def _sb_event(home, away, home_score, away_score, status="STATUS_FINAL_PEN",
              shootout=None, completed=True):
    """An ESPN scoreboard event: competitors carry score + (optionally) shootoutScore."""
    def comp(name, score, so):
        c = {"team": {"displayName": name}, "score": str(score)}
        if so is not None:
            c["shootoutScore"] = so
        return c
    so_h, so_a = shootout if shootout else (None, None)
    return {"id": "1", "competitions": [{
        "status": {"type": {"completed": completed, "name": status}},
        "competitors": [comp(home, home_score, so_h), comp(away, away_score, so_a)]}]}


def _opener(*events):
    payload = json.dumps({"events": list(events)}).encode()
    calls = []

    def opener(url):
        calls.append(url)
        return payload
    opener.calls = calls
    return opener


class ExtractScoreboardResultTests(unittest.TestCase):
    def test_penalty_tie_reads_shootout_tally(self):
        comp = _sb_event("Germany", "Paraguay", 1, 1, shootout=(3, 4))["competitions"][0]
        res = fk.extract_scoreboard_result(comp, "Germany", "Paraguay")
        self.assertEqual(res["score"], (1, 1))
        self.assertEqual(res["shootout"], (3, 4))
        self.assertEqual(res["status_name"], "STATUS_FINAL_PEN")

    def test_decisive_has_no_shootout(self):
        comp = _sb_event("France", "Sweden", 3, 0, status="STATUS_FULL_TIME")["competitions"][0]
        res = fk.extract_scoreboard_result(comp, "France", "Sweden")
        self.assertEqual(res["score"], (3, 0))
        self.assertIsNone(res["shootout"])

    def test_not_completed_returns_none(self):
        comp = _sb_event("Germany", "Paraguay", 0, 0, completed=False)["competitions"][0]
        self.assertIsNone(fk.extract_scoreboard_result(comp, "Germany", "Paraguay"))

    def test_alias_normalised(self):
        # ESPN 'Congo DR' must canon-join to 'DR Congo'
        comp = _sb_event("England", "Congo DR", 2, 1,
                         status="STATUS_FULL_TIME")["competitions"][0]
        res = fk.extract_scoreboard_result(comp, "England", "DR Congo")
        self.assertEqual(res["score"], (2, 1))

    def test_team_mismatch_returns_none(self):
        comp = _sb_event("Germany", "Paraguay", 1, 1, shootout=(3, 4))["competitions"][0]
        self.assertIsNone(fk.extract_scoreboard_result(comp, "Spain", "Italy"))


class ApplyEspnResultsTests(unittest.TestCase):
    def test_enters_penalty_result_with_espn_shootout_winner(self):
        opener = _opener(_sb_event("Germany", "Paraguay", 1, 1, shootout=(3, 4)))
        updated, lines = fr.apply_espn_results([_tie()], opener=opener, today=TODAY)
        km = ko.by_no(updated)[74]
        self.assertTrue(km.is_played)
        self.assertEqual((km.score_a, km.score_b), (1, 1))
        self.assertEqual(km.decided_by, "penalties")
        self.assertEqual(km.winner_team, "Paraguay")            # from the shootout, 3-4
        self.assertTrue(any("shootout 3–4 (ESPN)" in ln for ln in lines))

    def test_level_without_shootout_tally_reports_manual_entry(self):
        # never guessed: a level game ESPN can't settle stays scheduled + gets the report
        opener = _opener(_sb_event("Germany", "Paraguay", 1, 1, shootout=None))
        updated, lines = fr.apply_espn_results([_tie()], opener=opener, today=TODAY)
        self.assertFalse(ko.by_no(updated)[74].is_played)
        self.assertTrue(any("enter manually" in ln for ln in lines))

    def test_sweeps_decisive_tie_and_reads_aet(self):
        opener = _opener(_sb_event("Germany", "Paraguay", 2, 1, status="STATUS_FINAL_AET"))
        updated, lines = fr.apply_espn_results([_tie()], opener=opener, today=TODAY)
        km = ko.by_no(updated)[74]
        self.assertTrue(km.is_played)
        self.assertEqual(km.decided_by, "extra_time")
        self.assertEqual(km.winner_team, "Germany")
        self.assertIsNone(km.reg_score)                         # 90' score comes separately

    def test_decisive_defaults_to_regulation(self):
        opener = _opener(_sb_event("Germany", "Paraguay", 2, 0, status="STATUS_FULL_TIME"))
        updated, _ = fr.apply_espn_results([_tie()], opener=opener, today=TODAY)
        km = ko.by_no(updated)[74]
        self.assertEqual(km.decided_by, "regulation")
        self.assertEqual(km.reg_score, (2, 0))                  # regulation => 90' == final

    def test_future_or_unresolved_ties_fetch_nothing(self):
        opener = _opener()
        future = _tie(no=97, team_a="France", team_b="Brazil", date_et="2026-07-09",
                      stadium="Gillette Stadium", city="Foxborough")
        unresolved = _tie(no=89, team_a="", team_b="", date_et="2026-07-04",
                          stadium="Lincoln Financial Field", city="Philadelphia")
        updated, lines = fr.apply_espn_results([future, unresolved],
                                               opener=opener, today=TODAY)
        self.assertEqual(opener.calls, [])                      # no network call at all
        self.assertEqual(lines, [])

    def test_in_progress_game_left_open(self):
        opener = _opener(_sb_event("Germany", "Paraguay", 1, 0, status="STATUS_IN_PROGRESS",
                                   completed=False))
        updated, lines = fr.apply_espn_results([_tie(date_et="2026-07-04")],
                                               opener=opener, today=TODAY)
        self.assertFalse(ko.by_no(updated)[74].is_played)
        self.assertEqual(lines, ["0 knockout result(s) entered from ESPN"])


if __name__ == "__main__":
    unittest.main()
