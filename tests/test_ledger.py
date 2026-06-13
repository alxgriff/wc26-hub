"""Unit tests for scripts/ledger.py (PLAN.md Phase 4 requirements + guards).

Run from the repo root:  python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ledger as lg
import standings as st


def row(mid, src="consensus", ph=0.5, pd=0.3, pa=0.2, score="1-0", ts="2026-06-12T10:00:00-04:00"):
    return {"match_id": mid, "source": src, "p_home": f"{ph:.4f}", "p_draw": f"{pd:.4f}",
            "p_away": f"{pa:.4f}", "predicted_score": score, "timestamp": ts}


def played(mid, a, b, sa, sb):
    return st.Match(mid, mid[0], (int(mid[1]) + 1) // 2, a, b, sa, sb, "played")


class LogCliTests(unittest.TestCase):
    """`ledger.py log` must exit non-zero when a slate prediction was MISSED
    (not logged before kickoff) so the daily health gate goes red."""

    def _run_with(self, lines):
        orig = lg.log_slate
        lg.log_slate = lambda *a, **k: lines
        try:
            return lg.main(["log", "2026-06-12"])
        finally:
            lg.log_slate = orig

    def test_missed_prediction_exits_nonzero(self):
        rc = self._run_with(["D1 X vs Y: MISSED — prediction not logged before kickoff (...)"])
        self.assertEqual(rc, 1)

    def test_all_logged_exits_zero(self):
        rc = self._run_with(["D1 X vs Y: logged 2 row(s)",
                             "D2 P vs Q: already logged (unchanged)"])
        self.assertEqual(rc, 0)


class BrierTests(unittest.TestCase):
    def test_certain_correct_call_scores_zero(self):
        self.assertAlmostEqual(lg.brier((1.0, 0.0, 0.0), 0), 0.0)

    def test_certain_wrong_call_scores_two(self):
        self.assertAlmostEqual(lg.brier((1.0, 0.0, 0.0), 2), 2.0)

    def test_uniform_scores_two_thirds(self):
        third = 1 / 3
        self.assertAlmostEqual(lg.brier((third, third, third), 1), 2 / 3, places=9)

    def test_outcome_index(self):
        self.assertEqual(lg.outcome_index(2, 0), 0)
        self.assertEqual(lg.outcome_index(1, 1), 1)
        self.assertEqual(lg.outcome_index(0, 3), 2)


class UpsertTests(unittest.TestCase):
    def test_idempotent_relog_does_not_duplicate(self):
        rows, ch1 = lg.upsert_prediction([], row("B1"), set(), False)
        rows, ch2 = lg.upsert_prediction(rows, row("B1"), set(), False)
        self.assertTrue(ch1)
        self.assertFalse(ch2)            # identical re-log = no-op
        self.assertEqual(len(rows), 1)   # never double-logged

    def test_prekickoff_revision_updates_in_place(self):
        rows, _ = lg.upsert_prediction([], row("B1", ph=0.5, pd=0.3, pa=0.2), set(), False)
        rows, ch = lg.upsert_prediction(rows, row("B1", ph=0.6, pd=0.25, pa=0.15), set(), False)
        self.assertTrue(ch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["p_home"], "0.6000")

    def test_sources_are_independent_rows(self):
        rows, _ = lg.upsert_prediction([], row("B1", src="model"), set(), False)
        rows, _ = lg.upsert_prediction(rows, row("B1", src="consensus"), set(), False)
        self.assertEqual(len(rows), 2)

    def test_post_kickoff_new_prediction_refused(self):
        with self.assertRaises(lg.LedgerError):
            lg.upsert_prediction([], row("B1"), set(), kickoff_passed=True)

    def test_post_kickoff_revision_refused_but_identical_relog_ok(self):
        rows, _ = lg.upsert_prediction([], row("B1"), set(), False)
        # identical re-log after kickoff: harmless no-op (idempotent rebuilds)
        rows, ch = lg.upsert_prediction(rows, row("B1"), set(), kickoff_passed=True)
        self.assertFalse(ch)
        with self.assertRaises(lg.LedgerError):
            lg.upsert_prediction(rows, row("B1", ph=0.9, pd=0.05, pa=0.05), set(),
                                 kickoff_passed=True)

    def test_played_match_rows_are_immutable(self):
        rows, _ = lg.upsert_prediction([], row("B1"), set(), False)
        with self.assertRaises(lg.LedgerError):
            lg.upsert_prediction(rows, row("B1", ph=0.9), {"B1"}, False)
        with self.assertRaises(lg.LedgerError):
            lg.upsert_prediction([], row("A1"), {"A1"}, False)   # nor added late


class GradingTests(unittest.TestCase):
    def test_grades_only_played_matches_with_consensus_rows(self):
        matches = [played("A1", "X", "Y", 2, 0),
                   st.Match("A2", "A", 1, "P", "Q", None, None, "scheduled")]
        ledger = [row("A1", ph=0.7, pd=0.2, pa=0.1),     # consensus, correct
                  row("A2"), row("A1", src="model")]
        graded = lg.grade(matches, ledger)
        self.assertEqual(set(graded), {"A1"})
        g = graded["A1"]
        self.assertEqual(g["outcome"], 0)
        self.assertTrue(g["correct"])
        self.assertAlmostEqual(g["brier"], 0.3**2 + 0.2**2 + 0.1**2, places=9)

    def test_wrong_call_marked_incorrect(self):
        matches = [played("A1", "X", "Y", 0, 1)]         # away win
        graded = lg.grade(matches, [row("A1", ph=0.7, pd=0.2, pa=0.1)])
        self.assertFalse(graded["A1"]["correct"])
        self.assertGreater(graded["A1"]["brier"], 1.0)

    def test_cumulative_line(self):
        matches = [played("A1", "X", "Y", 2, 0), played("A2", "P", "Q", 1, 1)]
        ledger = [row("A1", ph=1.0, pd=0.0, pa=0.0),     # Brier 0
                  row("A2", ph=1.0, pd=0.0, pa=0.0)]     # Brier 2
        line = lg.cumulative_line(matches, ledger)
        self.assertIn("2 graded", line)
        self.assertIn("1 correct", line)
        self.assertIn("1.000", line)                     # mean of 0 and 2


class FileRoundTripTests(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ledger.csv"
            lg.save_ledger([row("B1"), row("B1", src="model")], p)
            back = lg.load_ledger(p)
            self.assertEqual(len(back), 2)
            self.assertEqual(back[0]["match_id"], "B1")
            self.assertEqual(back[0]["p_home"], "0.5000")

    def test_missing_file_is_empty(self):
        self.assertEqual(lg.load_ledger(Path("nope") / "missing.csv"), [])


if __name__ == "__main__":
    unittest.main()
