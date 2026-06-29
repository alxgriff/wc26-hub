"""Tests for scripts/fetch_ko_reg_scores.py — the ESPN 90-minute (regulation) score feed.
No network: an injected opener returns canned ESPN scoreboard + summary JSON."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import knockout as ko          # noqa: E402
import fetch_ko_reg_scores as fk  # noqa: E402


def _summary_competition(home, away, home_ls, away_ls, completed=True):
    """An ESPN summary `header.competitions[0]`-shaped dict."""
    def comp(name, ls):
        return {"team": {"displayName": name},
                "linescores": [{"displayValue": str(v)} for v in ls],
                "score": str(sum(ls))}
    return {"status": {"type": {"completed": completed, "name": "STATUS_FINAL_PEN"}},
            "competitors": [comp(home, home_ls), comp(away, away_ls)]}


def _played_pen_tie(no=97, team_a="France", team_b="Brazil"):
    """A knockout tie played to penalties with NO regulation score yet (needs the ESPN fetch)."""
    return ko.KnockoutMatch(no, ko.round_of(no), "2026-07-09", "16:00", "4:00 PM",
                            "Gillette Stadium", "Foxborough", "USA", "Fox", team_a, team_b,
                            1, 1, "penalties", "A", "played", "")


class ExtractRegScoreTests(unittest.TestCase):
    def test_extra_time_takes_first_two_halves(self):
        # 90' = 1+0 / 0+1 = 1-1; later entries are ET + shootout and must be excluded
        comp = _summary_competition("France", "Brazil", [1, 0, 0, 1, 5], [0, 1, 0, 1, 4])
        self.assertEqual(fk.extract_reg_score(comp, "France", "Brazil"), (1, 1))

    def test_regulation_two_halves(self):
        comp = _summary_competition("France", "Brazil", [2, 1], [0, 1])
        self.assertEqual(fk.extract_reg_score(comp, "France", "Brazil"), (3, 1))

    def test_alias_normalised(self):
        # ESPN 'Congo DR' must canon-join to 'DR Congo'
        comp = _summary_competition("England", "Congo DR", [1, 0, 0, 1], [0, 1, 1, 0])
        self.assertEqual(fk.extract_reg_score(comp, "England", "DR Congo"), (1, 1))

    def test_not_completed_returns_none(self):
        comp = _summary_competition("France", "Brazil", [1, 0], [0, 1], completed=False)
        self.assertIsNone(fk.extract_reg_score(comp, "France", "Brazil"))

    def test_team_mismatch_returns_none(self):
        comp = _summary_competition("France", "Brazil", [1, 0], [0, 1])
        self.assertIsNone(fk.extract_reg_score(comp, "Spain", "Italy"))


class ApplyRegScoresTests(unittest.TestCase):
    def _opener(self, event_id="999", home="France", away="Brazil",
                home_ls=(1, 0, 0, 1, 5), away_ls=(0, 1, 0, 1, 4)):
        scoreboard = {"events": [{"id": event_id, "competitions": [{"competitors": [
            {"team": {"displayName": home}}, {"team": {"displayName": away}}]}]}]}
        summary = {"header": {"competitions": [
            _summary_competition(home, away, list(home_ls), list(away_ls))]}}

        def opener(url):
            return json.dumps(scoreboard if "scoreboard" in url else summary).encode()
        return opener

    def test_sets_reg_score_for_et_tie(self):
        km = _played_pen_tie()
        self.assertIsNone(km.reg_score)
        updated, lines = fk.apply_reg_scores([km], opener=self._opener())
        self.assertEqual(ko.by_no(updated)[97].reg_score, (1, 1))   # 90' from ESPN
        self.assertTrue(any("regulation (90') score 1–1" in ln for ln in lines))

    def test_regulation_tie_needs_no_fetch(self):
        # a regulation-decided tie derives its 90' score -> not in `need`, never fetched
        km = ko.KnockoutMatch(97, "QF", "2026-07-09", "16:00", "4:00 PM", "Gillette Stadium",
                              "Foxborough", "USA", "Fox", "France", "Brazil", 2, 1,
                              "regulation", "A", "played", "")
        called = []
        updated, lines = fk.apply_reg_scores([km], opener=lambda u: called.append(u) or b"{}")
        self.assertEqual(called, [])                                # no network call
        self.assertIn("no knockout ties need a regulation score", lines[0])

    def test_unfound_event_leaves_open(self):
        km = _played_pen_tie()
        opener = self._opener(home="Spain", away="Italy")           # scoreboard has a different tie
        updated, lines = fk.apply_reg_scores([km], opener=opener)
        self.assertIsNone(ko.by_no(updated)[97].reg_score)
        self.assertTrue(any("event not found" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
