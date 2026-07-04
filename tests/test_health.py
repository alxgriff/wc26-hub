"""Tests for scripts/health.py — the silent-stall detector. HERMETIC by design: builds
synthetic KnockoutMatch lists and a temp cards dir, never reads live data/ (a health
check whose tests assert live point-in-time state would itself break at phase
boundaries — the exact non-hermetic failure class from 2026-06-27)."""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import knockout as ko          # noqa: E402
import health                  # noqa: E402

TODAY = date(2026, 7, 4)


def _tie(no=74, date_et="2026-06-29", team_a="Germany", team_b="Paraguay",
         played=False, decided_by="", winner="", score=(None, None), reg=(None, None)):
    return ko.KnockoutMatch(no, ko.round_of(no), date_et, "16:30", "4:30 PM",
                            "Gillette Stadium", "Foxborough", "USA", "",
                            team_a, team_b, score[0], score[1], decided_by, winner,
                            "played" if played else "scheduled", "",
                            score_a_reg=reg[0], score_b_reg=reg[1])


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cards = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _card(self, no):
        (self.cards / f"M{no}.md").write_text("# card", encoding="utf-8")

    def test_past_date_unentered_tie_is_a_stall(self):
        issues = health.check_knockout([_tie()], TODAY, self.cards)
        self.assertEqual(len(issues), 1)
        self.assertIn("STALL M74", issues[0])
        self.assertIn("no result is entered", issues[0])

    def test_unresolved_tie_is_not_a_stall(self):
        # participants unknown => the stall is upstream (feeder), reported on the feeder
        issues = health.check_knockout([_tie(team_a="", team_b="")], TODAY, self.cards)
        self.assertEqual(issues, [])

    def test_played_pens_tie_without_reg_score_is_a_stall(self):
        km = _tie(played=True, decided_by="penalties", winner="B", score=(1, 1))
        issues = health.check_knockout([km], TODAY, self.cards)
        self.assertEqual(len(issues), 1)
        self.assertIn("no 90' regulation score", issues[0])

    def test_played_pens_tie_with_reg_score_is_healthy(self):
        km = _tie(played=True, decided_by="penalties", winner="B",
                  score=(1, 1), reg=(1, 1))
        self.assertEqual(health.check_knockout([km], TODAY, self.cards), [])

    def test_regulation_tie_needs_no_reg_columns(self):
        km = _tie(played=True, decided_by="regulation", winner="A", score=(2, 0))
        self.assertEqual(health.check_knockout([km], TODAY, self.cards), [])

    def test_resolved_near_tie_without_card_is_a_gap(self):
        km = _tie(no=97, date_et="2026-07-06", team_a="France", team_b="Brazil")
        issues = health.check_knockout([km], TODAY, self.cards)
        self.assertEqual(len(issues), 1)
        self.assertIn("GAP M97", issues[0])

    def test_resolved_near_tie_with_card_is_healthy(self):
        km = _tie(no=97, date_et="2026-07-06", team_a="France", team_b="Brazil")
        self._card(97)
        self.assertEqual(health.check_knockout([km], TODAY, self.cards), [])

    def test_far_future_tie_needs_no_card_yet(self):
        km = _tie(no=104, date_et="2026-07-19", team_a="France", team_b="Brazil")
        self.assertEqual(health.check_knockout([km], TODAY, self.cards), [])

    def test_healthy_slate_is_empty(self):
        played = _tie(played=True, decided_by="regulation", winner="A", score=(2, 1))
        future = _tie(no=97, date_et="2026-07-09", team_a="France", team_b="Brazil")
        self.assertEqual(health.check_knockout([played, future], TODAY, self.cards), [])


if __name__ == "__main__":
    unittest.main()
