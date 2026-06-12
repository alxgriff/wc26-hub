"""Unit tests for scripts/build_site.py and standings.to_dict."""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import standings as st
import build_edition as be
import build_site as bs
from test_standings import mk, _hierarchy_group


def nine_groups():
    matches = []
    for n, g in enumerate("ABCDEFGHI", 1):
        matches += _hierarchy_group(g, [f"{g}1t", f"{g}2t", f"{g}3t", f"{g}4t"],
                                    third_win=(n, 0))
    return matches


class ToDictTests(unittest.TestCase):
    def test_schema_and_qualifying_flags(self):
        s = st.compute_standings(nine_groups())
        d = st.to_dict(s)
        self.assertEqual(d["schema"], 1)
        self.assertEqual(d["played"], 54)
        self.assertEqual(d["total"], 54)
        self.assertEqual(len(d["groups"]), 9)
        a1 = d["groups"]["A"]["rows"][0]
        self.assertEqual((a1["pos"], a1["team"], a1["pts"]), (1, "A1t", 9))
        thirds = d["third_place"]["rows"]
        self.assertEqual(len(thirds), 9)
        self.assertTrue(all(r["qualifying"] for r in thirds[:8]))
        self.assertFalse(thirds[8]["qualifying"])
        json.dumps(d)  # must be JSON-serializable as-is

    def test_round_trips_notes_and_warnings(self):
        matches = _hierarchy_group("F", ["Côte d'Ivoire", "F2t", "F3t", "F4t"],
                                   third_win=(2, 0))
        matches[2] = mk("F3", "Cote d'Ivoire", "F3t", 2, 0)
        d = st.to_dict(st.compute_standings(matches))
        self.assertTrue(any("expected 4 teams" in w for w in d["warnings"]))


class FormTests(unittest.TestCase):
    def test_form_letters_indexed_by_matchday(self):
        matches = [
            mk("A1", "X", "Y", 2, 0),       # MD1: X wins
            mk("A2", "Z", "W", 1, 1),       # MD1: draw
            mk("A3", "X", "Z"),             # MD2: unplayed
            mk("A4", "Y", "W"),
            mk("A5", "X", "W", 0, 1),       # MD3 played before MD2 entered
            mk("A6", "Y", "Z"),
        ]
        forms = bs.form_by_team(matches)
        self.assertEqual(forms["X"], ["W", None, "L"])
        self.assertEqual(forms["Y"], ["L", None, None])
        self.assertEqual(forms["Z"], ["D", None, None])
        self.assertEqual(forms["W"], ["D", None, "W"])

    def test_form_rejects_out_of_range_matchday(self):
        bad = st.Match("A1", "A", 4, "X", "Y", 1, 0, "played")
        with self.assertRaises(ValueError):
            bs.form_by_team([bad])

    def test_form_rejects_duplicate_matchday_result(self):
        matches = [mk("A1", "X", "Y", 1, 0),
                   st.Match("A2", "A", 1, "Z", "X", 1, 0, "played")]
        with self.assertRaises(ValueError):
            bs.form_by_team(matches)


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matches = nine_groups()
        cls.rows = [
            {"match_id": "A1", "group": "A", "team_a": "A1t", "team_b": "A2t",
             "kickoff_et": "3:00 PM", "kickoff_et_24h": "15:00", "tv_us": "Fox",
             "stadium": "Estadio Azteca", "city": "Mexico City",
             "status": "scheduled", "_late_cap": False,
             "_editorial": date(2026, 6, 12)},
            {"match_id": "D2", "group": "D", "team_a": "D1t", "team_b": "D2t",
             "kickoff_et": "12:00 AM", "kickoff_et_24h": "00:00", "tv_us": "FS1",
             "stadium": "BC Place", "city": "Vancouver",
             "status": "scheduled", "_late_cap": True,
             "_editorial": date(2026, 6, 12)},
        ]
        cls.page, cls.data = bs.build_page(
            cls.matches, cls.rows, date(2026, 6, 12), "2026-06-12 08:30")

    def test_page_has_all_group_cards_and_nav(self):
        for g in "ABCDEFGHI":
            self.assertIn(f'id="group-{g.lower()}"', self.page)
            self.assertIn(f'href="#group-{g.lower()}"', self.page)

    def test_cutline_rendered_once_between_8_and_9(self):
        self.assertEqual(self.page.count("the cutline — top 8 advance"), 1)
        cut = self.page.index("the cutline")
        # the 8th third (B3t) is rendered before the cut, the 9th (A3t) after
        self.assertLess(self.page.index('title="B3t"'), cut)
        thirds_section = self.page[self.page.index('id="thirds"'):]
        self.assertGreater(thirds_section.index('title="A3t"'),
                           thirds_section.index("the cutline"))

    def test_slate_includes_moon_flag_for_late_cap(self):
        self.assertIn("D1t v D2t", self.page)
        self.assertIn("☾", self.page)

    def test_accessibility_basics_present(self):
        for needle in ('lang="en"', 'name="viewport"', '<caption class="sr-only">',
                       'scope="col"', 'scope="row"', "prefers-reduced-motion"):
            self.assertIn(needle, self.page)

    def test_embedded_json_parses_and_matches(self):
        start = self.page.index('id="standings-data">') + len('id="standings-data">')
        end = self.page.index("</script>", start)
        raw = self.page[start:end]
        self.assertNotIn("<", raw)  # every < is <-escaped in the embed
        embedded = json.loads(raw)
        self.assertEqual(embedded["played"], self.data["played"])
        self.assertEqual(embedded["slate_date"], "2026-06-12")

    def test_html_escaping_of_team_names(self):
        matches = [mk("A1", "R&B United", "X's XI", 1, 0),
                   mk("A2", "C", "D"), mk("A3", "R&B United", "C"),
                   mk("A4", "X's XI", "D"), mk("A5", "R&B United", "D"),
                   mk("A6", "X's XI", "C")]
        page, _ = bs.build_page(matches, [], date(2026, 6, 12), "t")
        self.assertIn("R&amp;B United", page)
        self.assertNotIn("R&B United<", page)

    def test_no_unsubstituted_placeholders(self):
        import re
        leftovers = re.findall(r"\$[a-z_]+", self.page)
        self.assertEqual(leftovers, [])

    def test_pre_tournament_date_renders_preview_not_negative(self):
        page, _ = bs.build_page(self.matches, [], date(2026, 6, 1), "t")
        self.assertIn("Preview", page)
        self.assertNotIn("No. -", page)

    def test_keyboard_scroll_regions_present(self):
        self.assertEqual(self.page.count('tabindex="0"'), 2)
        self.assertIn("<main>", self.page)


class CliTests(unittest.TestCase):
    def test_cli_missing_template_is_clean_error(self):
        with tempfile.TemporaryDirectory() as td:
            rc = bs.main(["--date", "2026-06-12", "--out-dir", td,
                          "--template", str(Path(td) / "nope.html")])
            self.assertEqual(rc, 1)

    def test_cli_writes_index_and_data(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            rc = bs.main(["--date", "2026-06-12", "--out-dir", str(out)])
            self.assertEqual(rc, 0)
            self.assertTrue((out / "index.html").exists())
            data = json.loads((out / "data.json").read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], 1)
            self.assertEqual(len(data["groups"]), 12)


if __name__ == "__main__":
    unittest.main()
