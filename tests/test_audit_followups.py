"""Tests for the 2026-06-20 audit follow-ups: draw-bias caution + corpus sync."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_site as B
import corpus_sync as CS


class DrawCautionTests(unittest.TestCase):
    def test_win_side_h2h_flagged(self):
        self.assertTrue(B._h2h_win_side("h2h", "home"))
        self.assertTrue(B._h2h_win_side("h2h", "away"))

    def test_draw_and_other_markets_not_flagged(self):
        self.assertFalse(B._h2h_win_side("h2h", "draw"))     # draw pick has the opposite, safe bias
        self.assertFalse(B._h2h_win_side("totals", "over"))
        self.assertFalse(B._h2h_win_side("spreads", "home"))

    def test_caution_text_mentions_draws_and_md3(self):
        self.assertIn("draw", B._DRAW_AUDIT_CAUTION.lower())
        self.assertIn("MD3", B._DRAW_AUDIT_CAUTION)


class CorpusSyncTests(unittest.TestCase):
    def test_wc_rows_are_played_and_tagged(self):
        wc = CS.wc_rows()
        self.assertGreater(len(wc), 0)
        for r in wc:
            self.assertEqual(r["tournament"], CS.WC_TOURNAMENT)   # not "Friendly" => kept by load_curated
            int(r["home_score"]); int(r["away_score"])            # valid integer scores
            self.assertIn(r["neutral"], ("TRUE", "FALSE"))

    def test_merge_appends_and_upserts(self):
        # empty base -> all WC games added
        merged, n = CS.merge_wc([])
        self.assertEqual(len(merged), n)
        self.assertGreater(n, 0)
        # a STALE NA row for a real WC fixture is replaced (upsert), not duplicated
        w = CS.wc_rows()[0]
        stale = {"date": w["date"], "home_team": w["home_team"], "away_team": w["away_team"],
                 "home_score": "NA", "away_score": "NA", "tournament": "FIFA World Cup"}
        merged2, _ = CS.merge_wc([stale])
        same_fixture = [r for r in merged2 if r["date"] == w["date"]
                        and r["home_team"] == w["home_team"] and r["away_team"] == w["away_team"]]
        self.assertEqual(len(same_fixture), 1)                    # no duplicate
        self.assertNotEqual(same_fixture[0]["home_score"], "NA")  # the played version won

    def test_merge_preserves_unrelated_rows(self):
        base = [{"date": "1999-01-01", "home_team": "Narnia", "away_team": "Oz",
                 "home_score": "1", "away_score": "0", "tournament": "Friendly"}]
        merged, _ = CS.merge_wc(base)
        self.assertTrue(any(r.get("home_team") == "Narnia" for r in merged))


if __name__ == "__main__":
    unittest.main()
