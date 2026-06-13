"""Unit tests for scripts/odds.py (Phase 5 contract + guards).

Run from the repo root:  python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ledger as lg
import odds as od
import predict as pr
import standings as st

NOW = datetime(2026, 6, 13, 10, 0, tzinfo=lg.ET)
TS = NOW.isoformat(timespec="seconds")


def odds_row(mid, market, sel, odds, line="", source="median/5books",
             phase="snapshot", ts=TS):
    return {"match_id": mid, "market": market, "selection": sel, "line": line,
            "odds": f"{odds:.3f}", "source": source, "phase": phase, "timestamp": ts}


def h2h_rows(mid, h, d, a, **kw):
    return [odds_row(mid, "h2h", "home", h, **kw),
            odds_row(mid, "h2h", "draw", d, **kw),
            odds_row(mid, "h2h", "away", a, **kw)]


def totals_rows(mid, line, over, under, **kw):
    return [odds_row(mid, "totals", "over", over, line=str(line), **kw),
            odds_row(mid, "totals", "under", under, line=str(line), **kw)]


def ledger_row(mid, ph, pd, pa):
    return {"match_id": mid, "source": "consensus", "p_home": f"{ph:.4f}",
            "p_draw": f"{pd:.4f}", "p_away": f"{pa:.4f}",
            "predicted_score": "1-0", "timestamp": TS}


def fake_pred(lam_a=1.5, lam_b=1.0):
    return pr.Prediction("X", "Y", None, 0.5, 0.25, 0.25, lam_a, lam_b,
                         lam_a + lam_b, (1, 0), {1.5: 0.7, 2.5: 0.5, 3.5: 0.3}, 0.5)


class DevigTests(unittest.TestCase):
    def test_multiplicative_devig_sums_to_one_and_matches_formula(self):
        implied = od.devig([2.0, 3.5, 4.0])
        self.assertAlmostEqual(sum(implied), 1.0, places=9)
        raw = [1 / 2.0, 1 / 3.5, 1 / 4.0]
        self.assertAlmostEqual(implied[0], raw[0] / sum(raw), places=9)

    def test_rejects_odds_at_or_below_one(self):
        with self.assertRaises(od.OddsError):
            od.devig([1.0, 3.0, 4.0])


class ProbOverTests(unittest.TestCase):
    def test_matches_predict_matrix(self):
        # same lambdas through predict's matrix and odds' prob_over must agree
        m = pr.RatingModel({t.team: t for t in [
            _mk("A", 1850), _mk("B", 1750)]}, pr.Config(), "x")
        p = pr.predict_match(m, "A", "B")
        self.assertAlmostEqual(od.prob_over(p.lambda_a, p.lambda_b, 2.5),
                               p.over[2.5], places=4)

    def test_integer_line_rejected(self):
        with self.assertRaises(od.OddsError):
            od.prob_over(1.5, 1.0, 3.0)


def _mk(name, strength):
    return pr.TeamRating(team=name, elo=strength, futi=70, attack=70, defense=70,
                         strength=strength, z_att=0.0, z_def=0.0, elo_rank=1,
                         futi_rank=1, consensus_rank=1, opta_advance=None,
                         opta_wincup=None, opta_rank=None, market_odds=None,
                         market_implied=None, market_rank=None)


class EvaluateTests(unittest.TestCase):
    def test_h2h_edge_against_consensus(self):
        # market: 2.50/3.30/3.10 -> implied ~ .416/.315/.335 (devigged ~.40/.30/.30)
        odds_rows = h2h_rows("D3", 2.50, 3.30, 3.10)
        ledger_rows = [ledger_row("D3", 0.46, 0.28, 0.26)]   # we like home more
        ev = od.evaluate_match("D3", odds_rows, ledger_rows, fake_pred())
        sel, line, odds3, implied, our_p, edge = ev["h2h"][0]
        self.assertEqual(sel, "home")
        self.assertAlmostEqual(our_p, 0.46, places=4)
        self.assertGreater(edge, 0.03)              # a qualifying home edge
        self.assertIn("no totals snapshot", ev["missing"][0])

    def test_totals_edge_uses_model_probability(self):
        odds_rows = totals_rows("D3", 2.5, 1.95, 1.87)
        ev = od.evaluate_match("D3", odds_rows, [], fake_pred(2.0, 1.5))
        self.assertTrue(ev["totals"])
        over = ev["totals"][0]
        self.assertEqual(over[0], "over")
        self.assertAlmostEqual(over[4], od.prob_over(2.0, 1.5, 2.5), places=6)

    def test_no_consensus_logged_skips_h2h_edge(self):
        ev = od.evaluate_match("D3", h2h_rows("D3", 2.5, 3.3, 3.1), [], fake_pred())
        self.assertEqual(ev["h2h"], [])
        self.assertTrue(any("no logged consensus" in m for m in ev["missing"]))

    def test_invalid_consensus_suppresses_h2h_edge(self):
        # a corrupt consensus row (probs sum 1.10) must NOT drive an edge or an
        # (immutable) recorded bet; it is suppressed with a distinct, loud note.
        bad = {"match_id": "D3", "source": "consensus", "p_home": "0.50",
               "p_draw": "0.30", "p_away": "0.30", "predicted_score": "1-0",
               "timestamp": TS}
        self.assertIsNone(od.consensus_probs("D3", [bad]))
        ev = od.evaluate_match("D3", h2h_rows("D3", 2.5, 3.3, 3.1), [bad], fake_pred())
        self.assertEqual(ev["h2h"], [])
        self.assertTrue(any("fails the 1.0±0.001" in m for m in ev["missing"]))
        self.assertFalse(any("no logged consensus" in m for m in ev["missing"]))

    def test_latest_snapshot_wins(self):
        old = h2h_rows("D3", 2.0, 3.0, 4.0, ts="2026-06-13T08:00:00-04:00")
        new = h2h_rows("D3", 2.5, 3.3, 3.1, ts="2026-06-13T10:00:00-04:00")
        mk = od.latest_market(old + new, "D3", "h2h")
        self.assertAlmostEqual(mk[("home", "")][0], 2.5)

    def test_mixed_lines_pair_only_matching_sides(self):
        # books quote different main totals lines; over@2.5 must never be
        # de-vigged against under@3.0. Every COMPLETE pair is evaluated as a
        # ladder; integer lines carry a push note.
        rows = [odds_row("D3", "totals", "over", 1.95, line="2.5",
                         source="median/7books"),
                odds_row("D3", "totals", "under", 1.87, line="2.5",
                         source="median/7books"),
                odds_row("D3", "totals", "over", 2.30, line="3.0",
                         source="median/2books"),
                odds_row("D3", "totals", "under", 1.62, line="3.0",
                         source="median/2books")]
        ev = od.evaluate_match("D3", rows, [], fake_pred())
        self.assertEqual(len(ev["totals"]), 4)           # both lines, both sides
        self.assertEqual(sorted({r[1] for r in ev["totals"]}), ["2.5", "3.0"])
        # de-vig stays within a line: each line's implied probabilities sum to 1
        for line in ("2.5", "3.0"):
            implied = [r[3] for r in ev["totals"] if r[1] == line]
            self.assertAlmostEqual(sum(implied), 1.0, places=9)
        self.assertTrue(any("can push" in m for m in ev["missing"]))
        # orphan line on one side only -> not evaluated
        rows_orphan = [odds_row("D3", "totals", "over", 1.95, line="2.5"),
                       odds_row("D3", "totals", "under", 1.62, line="3.0")]
        ev2 = od.evaluate_match("D3", rows_orphan, [], fake_pred())
        self.assertEqual(ev2["totals"], [])
        self.assertTrue(any("no line has both" in m for m in ev2["missing"]))


class BestBetTests(unittest.TestCase):
    def _ev(self, edge_home):
        implied = 0.40
        return {"h2h": [("home", "", 2.5, implied, implied + edge_home, edge_home)],
                "totals": [], "missing": []}

    def test_pick_when_edge_clears_threshold(self):
        pick, flags = od.best_bet(self._ev(0.05))
        self.assertIsNotNone(pick)
        self.assertEqual(pick["selection"], "home")
        self.assertEqual(flags, [])

    def test_no_bet_below_threshold(self):
        pick, flags = od.best_bet(self._ev(0.02))
        self.assertIsNone(pick)
        self.assertEqual(flags, [])

    def test_sanity_flag_blocks_implausible_edge(self):
        pick, flags = od.best_bet(self._ev(0.22))
        self.assertIsNone(pick)                      # not auto-picked
        self.assertTrue(any("implausibly large" in f for f in flags))

    def test_largest_edge_wins_across_markets(self):
        ev = {"h2h": [("home", "", 2.5, 0.40, 0.44, 0.04)],
              "totals": [("over", 2.5, 1.95, 0.50, 0.56, 0.06)], "missing": []}
        pick, _ = od.best_bet(ev)
        self.assertEqual(pick["market"], "totals")


class PickLedgerTests(unittest.TestCase):
    def _pick(self, edge=0.05):
        return {"market": "h2h", "selection": "home", "line": "", "odds": 2.5,
                "implied_p": 0.40, "our_p": 0.45, "edge": edge}

    def test_record_then_changed_price_needs_explicit_revise(self):
        # A recorded pick is a published commitment: re-pricing it silently is
        # forbidden; superseding requires the explicit revise flag.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "picks.csv"
            od.record_pick("D3", self._pick(), (2.55, "fanduel"), NOW, False, path)
            with self.assertRaises(od.OddsError):
                od.record_pick("D3", self._pick(0.06), (2.60, "betmgm"), NOW, False, path)
            od.record_pick("D3", self._pick(0.06), (2.60, "betmgm"), NOW, False, path,
                           allow_revise=True)
            picks = od.load_picks(path)
            self.assertEqual(len(picks), 1)          # superseded, not duplicated
            self.assertEqual(picks[0]["book"], "betmgm")
            self.assertEqual(picks[0]["status"], "open")
            self.assertEqual(picks[0]["stake"], "1")

    def test_post_kickoff_pick_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "picks.csv"
            with self.assertRaises(od.OddsError):
                od.record_pick("D3", self._pick(), (2.5, "x"), NOW, True, path)

    def test_settled_pick_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "picks.csv"
            od.record_pick("D3", self._pick(), (2.5, "x"), NOW, False, path)
            picks = od.load_picks(path)
            picks[0]["status"] = "won"
            od._save(picks, path, od.PICK_COLUMNS)
            with self.assertRaises(od.OddsError):
                od.record_pick("D3", self._pick(0.08), (2.6, "y"), NOW, False, path)


class ProbsValidTests(unittest.TestCase):
    """The shared 1.0±0.001 probability gate (ledger.probs_valid) used by both
    the bet-driving consensus and the site's rendered call."""

    def test_contract_boundaries(self):
        self.assertTrue(lg.probs_valid(("0.5", "0.3", "0.2")))     # strings, sum 1.0
        self.assertTrue(lg.probs_valid((0.4, 0.2995, 0.3005)))     # within ±0.001
        self.assertFalse(lg.probs_valid((0.5, 0.3, 0.3)))          # sums to 1.10
        self.assertFalse(lg.probs_valid((0.5, 0.3, 0.19)))         # sums to 0.99
        self.assertFalse(lg.probs_valid(("x", "0.3", "0.3")))      # unparseable
        self.assertFalse(lg.probs_valid((1.2, -0.1, -0.1)))        # out of [0,1]
        self.assertFalse(lg.probs_valid((0.5, 0.5)))               # wrong arity


class SettleTests(unittest.TestCase):
    def _setup(self, d, selection="home", market="h2h", line=""):
        path = Path(d) / "picks.csv"
        pick = {"market": market, "selection": selection, "line": line,
                "odds": 2.5, "implied_p": 0.40, "our_p": 0.45, "edge": 0.05}
        od.record_pick("D3", pick, (2.50, "book"), NOW, False, path)
        return path

    def _match(self, sa, sb):
        return st.Match("D3", "D", 2, "X", "Y", sa, sb, "played")

    def _fix(self, kickoff="13:00", date_et="2026-06-13"):
        # closing snapshots default to TS (2026-06-13 10:00 ET); a 13:00 kickoff
        # sits inside CLOSING_WINDOW so the close counts for CLV.
        return {"match_id": "D3", "date_et": date_et, "kickoff_et_24h": kickoff}

    def test_winning_h2h_pick_pays_odds_minus_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._setup(d)
            od.settle_picks([self._match(2, 0)], [], path)
            p = od.load_picks(path)[0]
            self.assertEqual(p["status"], "won")
            self.assertEqual(p["units"], "+1.50")

    def test_losing_pick_costs_one_unit(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._setup(d)
            od.settle_picks([self._match(0, 0)], [], path)
            p = od.load_picks(path)[0]
            self.assertEqual(p["status"], "lost")
            self.assertEqual(p["units"], "-1.00")

    def test_totals_settlement(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._setup(d, selection="over", market="totals", line="2.5")
            od.settle_picks([self._match(2, 1)], [], path)   # 3 goals > 2.5
            self.assertEqual(od.load_picks(path)[0]["status"], "won")

    def test_clv_from_closing_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._setup(d)
            closing = h2h_rows("D3", 2.20, 3.40, 3.40, phase="closing")
            od.settle_picks([self._match(2, 0)], closing, path,
                            fixtures_rows=[self._fix()])
            p = od.load_picks(path)[0]
            # closing home implied (devig 2.20/3.40/3.40) ≈ .436 vs snapshot .40
            self.assertTrue(p["clv_pp"].startswith("+3"))

    def test_stale_closing_snapshot_ignored_for_clv(self):
        # a row tagged "closing" but logged ~a week before kickoff (the June-12
        # bulk-snapshot bug) is NOT a real close: CLV must stay blank, not wrong.
        with tempfile.TemporaryDirectory() as d:
            path = self._setup(d)
            closing = h2h_rows("D3", 2.20, 3.40, 3.40, phase="closing")  # ts = June 13 10:00
            od.settle_picks([self._match(2, 0)], closing, path,
                            fixtures_rows=[self._fix(kickoff="21:00", date_et="2026-06-20")])
            self.assertEqual(od.load_picks(path)[0]["clv_pp"], "")

    def test_clv_blank_without_fixtures_rows(self):
        # no kickoff to verify the close against -> never invent a CLV figure
        with tempfile.TemporaryDirectory() as d:
            path = self._setup(d)
            closing = h2h_rows("D3", 2.20, 3.40, 3.40, phase="closing")
            od.settle_picks([self._match(2, 0)], closing, path)
            self.assertEqual(od.load_picks(path)[0]["clv_pp"], "")

    def test_missing_closing_leaves_clv_blank(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._setup(d)
            od.settle_picks([self._match(2, 0)], [], path, fixtures_rows=[self._fix()])
            self.assertEqual(od.load_picks(path)[0]["clv_pp"], "")


class ApiMappingTests(unittest.TestCase):
    FIXTURE_ROWS = [{"match_id": "A4", "team_a": "Mexico", "team_b": "South Korea"}]

    def _event(self, home="Korea Republic", away="Mexico"):
        return {"home_team": home, "away_team": away, "bookmakers": [
            {"key": "fanduel", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": home, "price": 3.1},
                    {"name": "Draw", "price": 3.2},
                    {"name": away, "price": 2.3}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": 1.95, "point": 2.5},
                    {"name": "Under", "price": 1.87, "point": 2.5}]}]},
            {"key": "betmgm", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": home, "price": 3.0},
                    {"name": "Draw", "price": 3.25},
                    {"name": away, "price": 2.35}]}]},
        ]}

    def test_alias_names_and_reversed_home_away_map_correctly(self):
        rows, lines = od.snapshot_from_api([self._event()], self.FIXTURE_ROWS,
                                           "snapshot", NOW)
        # "Korea Republic" (API home) is fixtures team_b -> selection 'away'
        med = {(r["market"], r["selection"]): float(r["odds"])
               for r in rows if r["source"].startswith("median")}
        self.assertAlmostEqual(med[("h2h", "away")], 3.05, places=3)   # median 3.1/3.0
        self.assertAlmostEqual(med[("h2h", "home")], 2.325, places=3)  # Mexico
        best = {(r["market"], r["selection"]): r["source"]
                for r in rows if r["source"].startswith("best:")}
        self.assertEqual(best[("h2h", "away")], "best:fanduel")

    def test_unknown_team_reported_never_guessed(self):
        rows, lines = od.snapshot_from_api([self._event(home="Corea")],
                                           self.FIXTURE_ROWS, "snapshot", NOW)
        self.assertEqual(rows, [])
        self.assertTrue(any("UNMATCHED" in l for l in lines))

    def test_in_play_event_not_logged(self):
        ev = self._event()
        ev["commence_time"] = "2026-06-13T13:00:00Z"   # 9:00 ET < NOW (10:00 ET)
        rows, lines = od.snapshot_from_api([ev], self.FIXTURE_ROWS, "snapshot", NOW)
        self.assertEqual(rows, [])
        self.assertTrue(any("kicked off" in l for l in lines))


class AsianHandicapTests(unittest.TestCase):
    # toy margin distribution from home's perspective:
    # P(-1)=0.2, P(0)=0.3, P(+1)=0.3, P(+2)=0.2
    M = {-1: 0.2, 0: 0.3, 1: 0.3, 2: 0.2}

    def test_quarter_line_components(self):
        self.assertEqual(od._ah_components(-0.75), [-1.0, -0.5])
        self.assertEqual(od._ah_components(0.25), [0.0, 0.5])
        self.assertEqual(od._ah_components(-1.0), [-1.0])
        self.assertEqual(od._ah_components(0.5), [0.5])

    def test_half_line_prob(self):
        # home -0.5: win iff margin >= 1 -> 0.5; lose otherwise -> 0.5
        self.assertAlmostEqual(od.ah_prob(self.M, -0.5), 0.5, places=9)

    def test_integer_line_excludes_pushes(self):
        # home -1: win iff margin >= 2 (0.2), push at 1 (0.3), lose else (0.5)
        w, l = od.ah_effective(self.M, -1.0)
        self.assertAlmostEqual(w, 0.2, places=9)
        self.assertAlmostEqual(l, 0.5, places=9)
        self.assertAlmostEqual(od.ah_prob(self.M, -1.0), 0.2 / 0.7, places=9)

    def test_quarter_line_averages_components(self):
        # home -0.75 = half at -0.5 (W=.5,L=.5) + half at -1 (W=.2,L=.5)
        w, l = od.ah_effective(self.M, -0.75)
        self.assertAlmostEqual(w, 0.35, places=9)
        self.assertAlmostEqual(l, 0.5, places=9)

    def test_home_and_away_sides_are_complementary(self):
        p_home = od.ah_prob(self.M, -0.5)
        away = {-m: p for m, p in self.M.items()}
        self.assertAlmostEqual(od.ah_prob(away, 0.5), 1 - p_home, places=9)

    def test_margin_dist_sums_to_one_and_matches_wdl(self):
        m = od.margin_dist(1.5, 1.0)
        self.assertAlmostEqual(sum(m.values()), 1.0, places=6)
        # P(margin > 0) must equal the predict matrix home-win probability
        model = pr.RatingModel({t.team: t for t in [_mk("A", 1850), _mk("B", 1750)]},
                               pr.Config(), "x")
        p = pr.predict_match(model, "A", "B")
        m2 = od.margin_dist(p.lambda_a, p.lambda_b)
        self.assertAlmostEqual(sum(v for k, v in m2.items() if k > 0), p.p_a, places=4)


class AhSettlementTests(unittest.TestCase):
    def test_full_win_and_loss(self):
        self.assertEqual(od.ah_settle_units(2, -1.5, 2.0), (1.0, "won"))
        self.assertEqual(od.ah_settle_units(0, -0.5, 2.0), (-1.0, "lost"))

    def test_integer_push_returns_stake(self):
        units, status = od.ah_settle_units(1, -1.0, 2.0)
        self.assertAlmostEqual(units, 0.0)
        self.assertEqual(status, "push")

    def test_quarter_half_win(self):
        # home -0.25, drew 0-0 from selection perspective margin 0:
        # half at 0 -> push, half at -0.5 -> loss => -0.5u half-lost
        units, status = od.ah_settle_units(0, -0.25, 2.0)
        self.assertAlmostEqual(units, -0.5)
        self.assertEqual(status, "half-lost")
        # margin +1 at -0.75 with odds 2.10: half at -0.5 wins (+0.55),
        # half at -1.0 pushes (0) => +0.55 half-won
        units, status = od.ah_settle_units(1, -0.75, 2.10)
        self.assertAlmostEqual(units, 0.55, places=9)
        self.assertEqual(status, "half-won")

    def test_settle_spreads_pick_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "picks.csv"
            pick = {"market": "spreads", "selection": "home", "line": -0.75,
                    "odds": 2.10, "implied_p": 0.48, "our_p": 0.53, "edge": 0.05}
            od.record_pick("D3", pick, (2.10, "book"), NOW, False, path)
            m = st.Match("D3", "D", 2, "X", "Y", 2, 1, "played")   # margin +1
            od.settle_picks([m], [], path)
            p = od.load_picks(path)[0]
            self.assertEqual(p["status"], "half-won")
            self.assertEqual(p["units"], "+0.55")

    def test_units_summary_counts_pushes(self):
        picks = [{"status": "won", "units": "+1.50", "clv_pp": ""},
                 {"status": "push", "units": "+0.00", "clv_pp": ""},
                 {"status": "half-lost", "units": "-0.50", "clv_pp": ""}]
        out = od.units_summary(picks)
        self.assertIn("1W-1L-1P", out)
        self.assertIn("+1.00u", out)


class BttsAndSpreadsEvalTests(unittest.TestCase):
    def test_spreads_evaluated_and_eligible_for_best_bet(self):
        rows = [odds_row("D3", "spreads", "home", 2.10, line="-0.5"),
                odds_row("D3", "spreads", "away", 1.80, line="0.5")]
        ev = od.evaluate_match("D3", rows, [], fake_pred(2.0, 1.0))   # strong home
        self.assertEqual(len(ev["spreads"]), 2)
        sel, line, o, imp, our_p, edge = ev["spreads"][0]
        self.assertEqual((sel, line), ("home", "-0.5"))
        self.assertAlmostEqual(our_p, od.ah_prob(od.margin_dist(2.0, 1.0), -0.5), places=9)
        pick, _ = od.best_bet(ev, threshold=0.001)
        self.assertIsNotNone(pick)

    def test_btts_evaluated_from_model(self):
        rows = [odds_row("D3", "btts", "yes", 1.85), odds_row("D3", "btts", "no", 1.95)]
        ev = od.evaluate_match("D3", rows, [], fake_pred())
        self.assertEqual(ev["btts"][0][0], "yes")
        self.assertAlmostEqual(ev["btts"][0][4], fake_pred().btts, places=9)

    def test_absent_optional_markets_stay_silent(self):
        ev = od.evaluate_match("D3", h2h_rows("D3", 2.5, 3.3, 3.1),
                               [ledger_row("D3", 0.4, 0.3, 0.3)], fake_pred())
        self.assertEqual(ev["spreads"], [])
        self.assertEqual(ev["btts"], [])
        self.assertFalse(any("spreads" in m or "btts" in m.lower()
                             for m in ev["missing"]))


class RenderTests(unittest.TestCase):
    def test_no_bet_render(self):
        ev = {"h2h": [("home", "", 2.5, 0.40, 0.41, 0.01)], "totals": [],
              "missing": ["no totals snapshot"]}
        out = od.render_odds_section("D3", ev, None, [], {})
        self.assertIn("**No bet**", out)
        self.assertIn("no totals snapshot", out)

    def test_pick_render_shows_best_price_and_paper_units(self):
        ev = {"h2h": [("home", "", 2.5, 0.40, 0.45, 0.05)], "totals": [], "missing": []}
        pick = {"market": "h2h", "selection": "home", "line": "", "odds": 2.5,
                "implied_p": 0.40, "our_p": 0.45, "edge": 0.05}
        out = od.render_odds_section("D3", ev, pick, [],
                                     {("h2h", "home", ""): (2.55, "fanduel")})
        self.assertIn("Best bet: home", out)
        self.assertIn("best price 2.55 (fanduel)", out)
        self.assertIn("Flat 1u (paper)", out)


if __name__ == "__main__":
    unittest.main()
