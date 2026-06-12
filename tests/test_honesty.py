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
        self.assertEqual(html_out, "")            # standfirst carries the message
        self.assertIn("No graded calls yet", cumulative)

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


class FateTests(unittest.TestCase):
    @staticmethod
    def mk(mid, a, b, sa=None, sb=None):
        import standings as st
        played = sa is not None
        return st.Match(mid, mid[0], (int(mid[1]) + 1) // 2, a, b, sa, sb,
                        "played" if played else "scheduled")

    def test_completed_group_fates_exact(self):
        # 1st/2nd through, 4th out, 3rd unmarked (cutline is cross-group math)
        m = [self.mk("A1", "T1", "T2", 1, 0), self.mk("A2", "T3", "T4", 2, 0),
             self.mk("A3", "T1", "T3", 1, 0), self.mk("A4", "T2", "T4", 3, 0),
             self.mk("A5", "T1", "T4", 1, 0), self.mk("A6", "T2", "T3", 1, 0)]
        warnings = []
        fates, md3 = bs.compute_fates(m, warnings)
        self.assertEqual(fates.get("T1"), "through")   # 9 pts
        self.assertEqual(fates.get("T2"), "through")   # 6 pts, GD +3 vs T3 +1
        self.assertNotIn("T3", fates)                  # 3rd: thirds race, never marked
        self.assertEqual(fates.get("T4"), "out")       # 0 pts, 4th
        self.assertEqual(md3, {})                      # 0 unplayed != MD3 state
        self.assertEqual(warnings, [])

    def test_mid_group_clinch_on_points_alone(self):
        # After MD2: T1 has 6 (massive GD), worst case stays top on points
        m = [self.mk("B1", "T1", "T2", 5, 0), self.mk("B2", "T3", "T4", 0, 0),
             self.mk("B3", "T1", "T3", 5, 0), self.mk("B4", "T2", "T4", 1, 1),
             self.mk("B5", "T1", "T4"), self.mk("B6", "T2", "T3")]
        fates, md3 = bs.compute_fates(m, [])
        self.assertEqual(fates.get("T1"), "through")
        self.assertNotIn("T2", fates)                  # alive, not decided
        self.assertNotIn("T4", fates)                  # can still reach 3rd+
        self.assertIn("B", md3)                        # 2 unplayed -> MD3 state

    def test_scenario_block_renders_both_teams(self):
        m = [self.mk("B1", "T1", "T2", 5, 0), self.mk("B2", "T3", "T4", 0, 0),
             self.mk("B3", "T1", "T3", 5, 0), self.mk("B4", "T2", "T4", 1, 1),
             self.mk("B5", "T1", "T4"), self.mk("B6", "T2", "T3")]
        _, md3 = bs.compute_fates(m, [])
        out = bs.render_scenario_block(md3["B"], "T1", "T4")
        self.assertIn("9 possible outcomes", out)
        self.assertIn("margin-dependent", out)
        self.assertIn("Win:", out)
        self.assertIn(">T1<", out)
        self.assertIn(">T4<", out)

    def test_fate_classes_and_sr_text_on_group_card(self):
        import standings as st
        m = [self.mk("A1", "T1", "T2", 1, 0), self.mk("A2", "T3", "T4", 2, 0),
             self.mk("A3", "T1", "T3", 1, 0), self.mk("A4", "T2", "T4", 3, 0),
             self.mk("A5", "T1", "T4", 1, 0), self.mk("A6", "T2", "T3", 1, 0)]
        fates, _ = bs.compute_fates(m, [])
        s = st.compute_standings(m)
        card = bs.render_group_card(s.groups["A"], {}, 0, fates=fates)
        self.assertIn("fate-through", card)
        self.assertIn("fate-out", card)
        self.assertIn("qualified for the Round of 32", card)   # sr-only text
        self.assertIn("— eliminated", card)


class TotalsLadderTests(unittest.TestCase):
    def test_totals_probs_integer_line_has_push_and_sums_to_one(self):
        over, push, under = od.totals_probs(1.3, 1.2, 2.0)
        self.assertGreater(push, 0.1)            # P(total == 2) is substantial
        self.assertAlmostEqual(over + push + under, 1.0, places=9)

    def test_totals_probs_half_line_no_push_matches_prob_over(self):
        over, push, under = od.totals_probs(1.3, 1.2, 2.5)
        self.assertEqual(push, 0.0)
        self.assertAlmostEqual(over, od.prob_over(1.3, 1.2, 2.5), places=12)

    @staticmethod
    def _odds_rows(*lines):
        rows = []
        for line, o_over, o_under in lines:
            for sel, o in (("over", o_over), ("under", o_under)):
                rows.append({"match_id": "X1", "market": "totals", "selection": sel,
                             "line": line, "odds": f"{o:.3f}",
                             "source": "median/5books", "phase": "snapshot",
                             "timestamp": "2026-06-13T07:00:00-04:00"})
        return rows

    def test_integer_line_now_evaluated_with_push_note(self):
        from types import SimpleNamespace
        pred = SimpleNamespace(lambda_a=1.3, lambda_b=1.2)
        ev = od.evaluate_match("X1", self._odds_rows(("2.0", 1.90, 1.90)), [], pred)
        self.assertEqual(len(ev["totals"]), 2)
        sels = {(s, l) for s, l, *_ in ev["totals"]}
        self.assertEqual(sels, {("over", "2.0"), ("under", "2.0")})
        over_row = next(r for r in ev["totals"] if r[0] == "over")
        self.assertAlmostEqual(over_row[3] + ev["totals"][1][3], 1.0, places=9)
        self.assertTrue(any("can push" in m for m in ev["missing"]))

    def test_full_ladder_every_paired_line_evaluated(self):
        from types import SimpleNamespace
        pred = SimpleNamespace(lambda_a=1.3, lambda_b=1.2)
        ev = od.evaluate_match("X1", self._odds_rows(
            ("1.5", 1.45, 2.75), ("2.5", 2.05, 1.85), ("3.5", 3.60, 1.30)), [], pred)
        lines = sorted({l for _s, l, *_ in ev["totals"]})
        self.assertEqual(lines, ["1.5", "2.5", "3.5"])
        self.assertEqual(len(ev["totals"]), 6)

    def test_integer_line_push_settles_as_refund(self):
        import standings as st
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            path = Path(f.name)
        path.unlink()
        od.record_pick("X1", {"market": "totals", "selection": "over", "line": "2.0",
                              "odds": 1.90, "implied_p": 0.5, "our_p": 0.56,
                              "edge": 0.06}, (1.90, "book"), NOW, False, path)
        match = st.Match("X1", "X", 1, "A", "B", 1, 1, "played")  # total = 2
        lines = od.settle_picks([match], [], picks_path=path)
        picks = od.load_picks(path)
        path.unlink()
        self.assertEqual(picks[0]["status"], "push")
        self.assertEqual(picks[0]["units"], "+0.00")
        self.assertTrue(any("push" in l for l in lines))

    def test_projection_line_renders_on_market_block(self):
        info = {"evaluation": {"h2h": [], "totals": [], "spreads": [], "btts": [],
                               "missing": []},
                "pick": None, "flags": [], "best_prices": {}, "recorded": [],
                "threshold": 0.03, "snapshot_ts": "2026-06-13T07:00:00-04:00",
                "projection": {"total": 2.41,
                               "over": {"1.5": 0.78, "2.5": 0.45, "3.5": 0.21}}}
        out = bs.render_market(info, "A", "B", None)
        self.assertIn("model projection", out)
        self.assertIn("<b>2.41</b> total goals", out)
        self.assertIn("over 1.5 <b>78%</b>", out)
        self.assertIn("over 3.5 <b>21%</b>", out)


if __name__ == "__main__":
    unittest.main()
