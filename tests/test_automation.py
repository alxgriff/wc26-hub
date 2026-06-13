"""Tests for the daily-automation scripts (results, blurb fact pack, news
prompt, discipline loader). API calls are isolated behind tested seams."""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_site as bs
import fetch_results as fr
import fetch_news as fn
import stakes_blurb as sb

REPO = Path(__file__).resolve().parents[1]

HEADER = ("match_id,group,matchday,date_et,kickoff_et_24h,kickoff_et,team_a,team_b,"
          "stadium,city,country,tv_us,score_a,score_b,status,notes")
ROWS = [
    "B1,B,1,2026-06-12,15:00,3:00 PM,Canada,Bosnia and Herzegovina,BMO Field,Toronto,Canada,Fox,,,scheduled,",
    "B2,B,1,2026-06-13,15:00,3:00 PM,Qatar,Switzerland,Levi's Stadium,Santa Clara,USA,Fox,,,scheduled,",
    "A1,A,1,2026-06-11,15:00,3:00 PM,Mexico,South Africa,Estadio Azteca,Mexico City,Mexico,,2,0,played,",
]


def _event(home, away, scores=None, completed=True):
    ev = {"home_team": home, "away_team": away, "completed": completed}
    if scores is not None:
        ev["scores"] = [{"name": n, "score": s} for n, s in scores]
    return ev


class FetchResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                               encoding="utf-8")
        self.tmp.write("\n".join([HEADER, *ROWS]) + "\n")
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_completed_event_enters_result(self):
        status = fr.apply_completed(
            [_event("Canada", "Bosnia and Herzegovina",
                    [("Canada", "2"), ("Bosnia and Herzegovina", "1")])],
            self.path)
        self.assertTrue(any("B1: Canada 2–1" in s for s in status))
        text = self.path.read_text(encoding="utf-8-sig")
        self.assertIn("Canada,Bosnia and Herzegovina", text)
        self.assertIn("2,1,played", text)

    def test_unmatched_event_is_reported_not_guessed(self):
        status = fr.apply_completed(
            [_event("Krakozhia", "Bosnia and Herzegovina",
                    [("Krakozhia", "1"), ("Bosnia and Herzegovina", "0")])],
            self.path)
        self.assertTrue(any("UNMATCHED" in s for s in status))
        self.assertIn(",,scheduled", self.path.read_text(encoding="utf-8-sig"))

    def test_already_played_left_untouched(self):
        status = fr.apply_completed(
            [_event("Mexico", "South Africa",
                    [("Mexico", "9"), ("South Africa", "9")])],
            self.path)
        self.assertTrue(any("0 result(s)" in s for s in status))
        self.assertIn("2,0,played", self.path.read_text(encoding="utf-8-sig"))

    def test_incomplete_event_ignored_and_dry_run_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8-sig")
        fr.apply_completed(
            [_event("Canada", "Bosnia and Herzegovina", completed=False),
             _event("Qatar", "Switzerland", [("Qatar", "1"), ("Switzerland", "1")])],
            self.path, dry_run=True)
        self.assertEqual(before, self.path.read_text(encoding="utf-8-sig"))


class DisciplineTests(unittest.TestCase):
    def test_fair_play_points_math(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as f:
            f.write("match_id,team,yellows,second_yellow_reds,direct_reds,yellow_plus_reds\n")
            f.write("A1,Mexico,2,0,1,0\n")       # -2 + -4 = -6
            f.write("A1,South Africa,1,1,0,0\n")  # -1 + -3 = -4
            f.write("A2,Mexico,1,0,0,0\n")        # cumulative: -7
            path = Path(f.name)
        try:
            fp = bs.load_discipline(path)
            self.assertEqual(fp, {"Mexico": -7, "South Africa": -4})
        finally:
            path.unlink()

    def test_missing_file_is_empty(self):
        self.assertEqual(bs.load_discipline(Path("does/not/exist.csv")), {})

    def test_missing_card_column_raises_not_silently_zero(self):
        # a renamed/dropped card column must stop-and-report, not score 0 silently
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as f:
            f.write("match_id,team,yellows,direct_reds\n")   # missing two card columns
            f.write("A1,Mexico,2,1\n")
            path = Path(f.name)
        try:
            with self.assertRaises(ValueError):
                bs.load_discipline(path)
        finally:
            path.unlink()


class BlurbFactPackTests(unittest.TestCase):
    def test_fact_pack_grounds_the_slate(self):
        pack = sb.build_fact_pack(date(2026, 6, 13), REPO / "data" / "fixtures.csv")
        self.assertIn("C1: Brazil vs Morocco", pack)
        self.assertIn("Published consensus", pack)     # June 13 slate is logged
        self.assertIn("1. ", pack)                     # group table lines
        self.assertNotIn("injur", pack.lower())        # no news channel here

    def test_rest_day_pack(self):
        pack = sb.build_fact_pack(date(2026, 7, 30), REPO / "data" / "fixtures.csv")
        self.assertIn("No matches", pack)


class SlatePickTests(unittest.TestCase):
    def test_pickline_renders_on_slate_chip(self):
        today = [{"match_id": "D1", "team_a": "United States", "team_b": "Paraguay",
                  "kickoff_et": "9:00 PM", "tv_us": "Fox", "stadium": "SoFi Stadium",
                  "city": "Inglewood", "status": "scheduled", "_late_cap": False}]
        out = bs.render_slate(today, picks={"D1": "Paraguay +11.2%"})
        self.assertIn("pickline", out)
        self.assertIn("best bet: Paraguay +11.2%", out)
        # and absent when no pick
        self.assertNotIn("pickline", bs.render_slate(today))


class WireTests(unittest.TestCase):
    DIGEST = """<!-- AUTO-GATHERED banner -->

> ⚠️ **Auto-gathered news digest — UNVERIFIED.**

### B2: Qatar vs Switzerland

- Akram Afif trained fully on Friday (source: https://example.com/afif)

### C1: Brazil vs Morocco

No verifiable updates found.
"""

    def test_load_wire_maps_sections_latest_wins(self):
        with tempfile.TemporaryDirectory() as td:
            nd = Path(td)
            (nd / "2026-06-12.md").write_text(self.DIGEST, encoding="utf-8")
            (nd / "2026-06-13.md").write_text(
                "### C1: Brazil vs Morocco\n\n- Neymar back in training "
                "(source: https://example.com/ney)\n", encoding="utf-8")
            wire = bs.load_wire(nd)
        self.assertEqual(wire["B2"][0], "2026-06-12")
        self.assertIn("Afif", wire["B2"][1])
        self.assertEqual(wire["C1"][0], "2026-06-13")   # later digest wins
        self.assertIn("Neymar", wire["C1"][1])
        self.assertNotIn("UNVERIFIED", wire["B2"][1])   # banner not in sections

    def test_render_wire_frames_as_reporting_with_clickable_sources(self):
        out = bs.render_wire(("2026-06-12",
                              "- Afif trained fully (source: https://example.com/afif)"))
        self.assertIn("The Wire", out)
        self.assertIn("not verified by the hub", out)
        self.assertIn('<a href="https://example.com/afif"', out)

    def test_no_digest_renders_nothing(self):
        self.assertEqual(bs.render_wire(None), "")
        self.assertEqual(bs.load_wire(Path("does/not/exist")), {})


class NewsPromptTests(unittest.TestCase):
    def test_prompt_lists_slate_only(self):
        prompt = fn.build_prompt(date(2026, 6, 13), REPO / "data" / "fixtures.csv")
        self.assertIn("B2: Qatar vs Switzerland", prompt)
        self.assertIn("D2: Australia vs Türkiye", prompt)  # 🌙 belongs to June 13
        self.assertNotIn("E1", prompt)

    def test_no_slate_returns_none(self):
        self.assertIsNone(fn.build_prompt(date(2026, 7, 30),
                                          REPO / "data" / "fixtures.csv"))


if __name__ == "__main__":
    unittest.main()
