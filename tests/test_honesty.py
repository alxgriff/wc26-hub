"""Tests for the honesty batch: pick immutability, recording gates,
knockout-rematch guards, and ledger-based grading inputs."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_site as bs
import odds as od

ET = timezone(timedelta(hours=-4))
NOW = datetime(2026, 6, 13, 8, 0, tzinfo=ET)

PICK = {"market": "h2h", "selection": "away", "line": "",
        "odds": 3.85, "implied_p": 0.25, "our_p": 0.36, "edge": 0.11}


class RecordPickImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        self.tmp.close()
        self.path = Path(self.tmp.name)
        self.path.unlink()  # odds.load_picks treats missing as empty

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def record(self, odds_v=4.00, when=NOW, **kw):
        return od.record_pick("D1", PICK, (odds_v, "betonlineag"), when,
                              kickoff_passed=False, picks_path=self.path, **kw)

    def test_identical_rerecord_is_noop_preserving_original_timestamp(self):
        first = self.record(when=NOW)
        self.assertIn("@ 4.00", first)
        second = self.record(when=NOW + timedelta(hours=3))
        self.assertIn("unchanged", second)
        picks = od.load_picks(self.path)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["timestamp"], NOW.isoformat(timespec="seconds"))

    def test_changed_price_refused_without_revise(self):
        self.record(odds_v=4.00)
        with self.assertRaises(od.OddsError) as ctx:
            self.record(odds_v=3.60, when=NOW + timedelta(hours=3))
        self.assertIn("--revise", str(ctx.exception))
        self.assertEqual(od.load_picks(self.path)[0]["odds"], "4.00")

    def test_revise_flag_supersedes_explicitly(self):
        self.record(odds_v=4.00)
        msg = self.record(odds_v=3.60, when=NOW + timedelta(hours=3),
                          allow_revise=True)
        self.assertIn("REVISED", msg)
        self.assertEqual(od.load_picks(self.path)[0]["odds"], "3.60")

    def test_settled_pick_immutable_even_with_revise(self):
        self.record(odds_v=4.00)
        picks = od.load_picks(self.path)
        picks[0]["status"] = "won"
        od._save(picks, self.path, od.PICK_COLUMNS)
        with self.assertRaises(od.OddsError):
            self.record(odds_v=3.60, allow_revise=True)


class SnapshotFreshnessTests(unittest.TestCase):
    ROWS = [{"match_id": "D1", "market": "totals", "phase": "snapshot",
             "timestamp": (NOW - timedelta(hours=2)).isoformat(timespec="seconds")},
            {"match_id": "D1", "market": "h2h", "phase": "snapshot",
             "timestamp": (NOW - timedelta(hours=20)).isoformat(timespec="seconds")},
            {"match_id": "D1", "market": "h2h", "phase": "closing",
             "timestamp": NOW.isoformat(timespec="seconds")}]

    def test_age_from_latest_snapshot(self):
        self.assertAlmostEqual(od._snapshot_age_hours(self.ROWS, "D1", NOW),
                               2.0, places=2)
        self.assertIsNone(od._snapshot_age_hours(self.ROWS, "Z9", NOW))

    def test_market_filter_prevents_fresh_totals_vouching_for_stale_h2h(self):
        age = od._snapshot_age_hours(self.ROWS, "D1", NOW, market="h2h")
        self.assertAlmostEqual(age, 20.0, places=2)

    def test_naive_timestamp_treated_as_stale_not_crash(self):
        rows = [{"match_id": "D1", "market": "h2h", "phase": "snapshot",
                 "timestamp": "2026-06-13T06:00:00"}]  # tz-naive
        self.assertIsNone(od._snapshot_age_hours(rows, "D1", NOW))


class MatchEventDateGuardTests(unittest.TestCase):
    FIXTURES = [{"match_id": "C1", "team_a": "Brazil", "team_b": "Morocco",
                 "date_et": "2026-06-13"}]

    def _event(self, commence):
        return {"home_team": "Brazil", "away_team": "Morocco",
                "commence_time": commence}

    def test_same_day_event_matches(self):
        ev = self._event("2026-06-13T22:00:00Z")
        self.assertIsNotNone(od._match_event(ev, self.FIXTURES))

    def test_knockout_rematch_weeks_later_does_not_match(self):
        ev = self._event("2026-07-04T20:00:00Z")
        self.assertIsNone(od._match_event(ev, self.FIXTURES))

    def test_missing_commence_time_still_matches_by_teams(self):
        ev = {"home_team": "Brazil", "away_team": "Morocco"}
        self.assertIsNotNone(od._match_event(ev, self.FIXTURES))


class LoggedCallTests(unittest.TestCase):
    FIXTURE = {"match_id": "D1", "date_et": "2026-06-12", "kickoff_et_24h": "21:00"}

    @staticmethod
    def ledger(rows):
        import ledger as lg
        return {"rows": rows, "published": "consensus", "brier": lg.brier,
                "kickoff_dt": lg.kickoff_dt, "now": lg.now_et()}

    @staticmethod
    def row(ts="2026-06-12T15:04:05-04:00", **kw):
        base = {"match_id": "D1", "source": "consensus", "p_home": "0.3758",
                "p_draw": "0.2667", "p_away": "0.3575", "predicted_score": "1-1",
                "timestamp": ts}
        base.update(kw)
        return base

    def test_returns_verified_pre_kickoff_consensus(self):
        warnings = []
        info = bs._logged_call("D1", self.ledger([self.row()]), self.FIXTURE, warnings)
        self.assertTrue(info["logged"])
        self.assertAlmostEqual(info["p_a"], 0.3758)
        self.assertEqual(info["predicted_score"], "1-1")
        self.assertEqual(warnings, [])

    def test_none_when_never_logged(self):
        self.assertIsNone(bs._logged_call("A2", self.ledger([self.row()]),
                                          self.FIXTURE, []))

    def test_post_kickoff_row_refused(self):
        warnings = []
        info = bs._logged_call("D1", self.ledger(
            [self.row(ts="2026-06-12T22:30:00-04:00")]), self.FIXTURE, warnings)
        self.assertIsNone(info)
        self.assertTrue(any("at/after kickoff" in w for w in warnings))

    def test_unverifiable_timestamp_refused(self):
        warnings = []
        info = bs._logged_call("D1", self.ledger([self.row(ts="not-a-time")]),
                               self.FIXTURE, warnings)
        self.assertIsNone(info)
        self.assertTrue(any("cannot verify" in w for w in warnings))

    def test_invalid_probabilities_refused(self):
        warnings = []
        info = bs._logged_call("D1", self.ledger([self.row(p_home="0.9")]),
                               self.FIXTURE, warnings)  # sums to 1.52
        self.assertIsNone(info)
        self.assertTrue(any("contract" in w for w in warnings))

    def test_last_row_wins_matching_ledger_grade(self):
        rows = [self.row(p_home="0.10", p_draw="0.10", p_away="0.80"),
                self.row()]
        info = bs._logged_call("D1", self.ledger(rows), self.FIXTURE, [])
        self.assertAlmostEqual(info["p_a"], 0.3758)


class GradedRenderingTests(unittest.TestCase):
    def test_brier_is_ledger_sum_form(self):
        import ledger as lg
        info = {"p_a": 0.3758, "p_draw": 0.2667, "p_b": 0.3575,
                "predicted_score": "1-1", "logged_ts": "2026-06-12T15:04:05-04:00",
                "logged": True, "brier_fn": lg.brier}
        out = bs.render_call(info, "United States", "Paraguay", None, result=(2, 1))
        self.assertIn("Brier <b>0.589</b>", out)   # sum form, not /3
        self.assertIn("coin-flip", out)

    def test_awaiting_state_freezes_logged_call_without_grading(self):
        info = {"p_a": 0.3758, "p_draw": 0.2667, "p_b": 0.3575,
                "predicted_score": "1-1", "logged_ts": "2026-06-12T15:04:05-04:00",
                "logged": True, "awaiting": True}
        out = bs.render_call(info, "United States", "Paraguay", None)
        self.assertIn("Awaiting result", out)
        self.assertNotIn("Graded:", out)


class RecordRenderingTests(unittest.TestCase):
    @staticmethod
    def fixtures():
        import standings as st
        from datetime import date as _date
        matches = [st.Match("A1", "A", 1, "Mexico", "South Africa", 2, 0, "played"),
                   st.Match("A2", "A", 1, "South Korea", "Czechia", 2, 1, "played")]
        rows = [{"match_id": "A1", "team_a": "Mexico", "team_b": "South Africa",
                 "_editorial": _date(2026, 6, 11)},
                {"match_id": "A2", "team_a": "South Korea", "team_b": "Czechia",
                 "_editorial": _date(2026, 6, 11)}]
        return matches, rows

    @staticmethod
    def ledger_for(ledger_rows):
        import ledger as lg
        return {"rows": ledger_rows, "published": "consensus", "brier": lg.brier,
                "kickoff_dt": lg.kickoff_dt, "grade": lg.grade,
                "cumulative": lg.cumulative_line, "now": lg.now_et()}

    def test_calls_table_with_day_subtotal_and_brier(self):
        matches, rows = self.fixtures()
        ledger_rows = [{"match_id": "A1", "source": "consensus", "p_home": "0.6",
                        "p_draw": "0.25", "p_away": "0.15",
                        "predicted_score": "2-0", "timestamp": "t"}]
        html_out, cumulative = bs.render_record_calls(
            matches, rows, self.ledger_for(ledger_rows))
        self.assertIn("Mexico v South Africa", html_out)
        self.assertIn("60%/25%/15%", html_out)
        self.assertIn("subtotal", html_out)
        self.assertIn("0.245", html_out)   # 0.4^2 + 0.25^2 + 0.15^2
        self.assertIn("hit-y", html_out)   # 60% favorite landed
        self.assertIn("cumulative Brier 0.245", cumulative)

    def test_no_grades_renders_honest_empty_state(self):
        matches, rows = self.fixtures()
        html_out, cumulative = bs.render_record_calls(
            matches, rows, self.ledger_for([]))
        self.assertIn("No graded calls yet", html_out)

    def test_overnight_grades_and_flags_missing_results(self):
        from datetime import date as _date
        matches, _ = self.fixtures()
        rows = [{"match_id": "A1", "team_a": "Mexico", "team_b": "South Africa",
                 "score_a": "2", "score_b": "0", "status": "played",
                 "_editorial": _date(2026, 6, 11), "_late_cap": False,
                 "kickoff_et_24h": "15:00", "date_et": "2026-06-11"},
                {"match_id": "A2", "team_a": "South Korea", "team_b": "Czechia",
                 "status": "scheduled", "_editorial": _date(2026, 6, 11),
                 "_late_cap": False, "kickoff_et_24h": "22:00",
                 "date_et": "2026-06-11"}]
        ledger_rows = [{"match_id": "A1", "source": "consensus", "p_home": "0.6",
                        "p_draw": "0.25", "p_away": "0.15",
                        "predicted_score": "", "timestamp": "t"}]
        out = bs.render_overnight(rows, _date(2026, 6, 12), matches,
                                  self.ledger_for(ledger_rows))
        self.assertIn("Overnight — Thursday, June 11", out)
        self.assertIn("logged 60% on the result", out)
        self.assertIn("Brier 0.245", out)
        self.assertIn("result not yet entered", out)

    def test_no_yesterday_matches_renders_nothing(self):
        from datetime import date as _date
        out = bs.render_overnight([], _date(2026, 7, 30), [], None)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
