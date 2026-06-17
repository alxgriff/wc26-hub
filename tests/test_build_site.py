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
        self.assertIn('class="matchup"', self.page)   # fixture-board matchup link
        self.assertIn(">D1t</span>", self.page)        # both sides rendered
        self.assertIn(">D2t</span>", self.page)
        self.assertIn("☾", self.page)                  # late-cap moon flag

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
        # the third-place table is still a horizontal scroll region; the slate is
        # now a responsive grid (no longer a scroll region)
        self.assertEqual(self.page.count('tabindex="0"'), 1)
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


class OutcomeGridTests(unittest.TestCase):
    def test_returns_empty_when_no_info(self):
        self.assertEqual(bs.render_outcome_grid(None, "Brazil", "Morocco"), "")

    def test_returns_empty_when_lambdas_missing(self):
        info = {"p_a": 0.55, "p_draw": 0.26, "p_b": 0.19}
        self.assertEqual(bs.render_outcome_grid(info, "Brazil", "Morocco"), "")

    def test_renders_details_element_with_lambdas(self):
        info = {"lambda_a": 1.51, "lambda_b": 0.76}
        html = bs.render_outcome_grid(info, "Brazil", "Morocco")
        self.assertIn("<details", html)
        self.assertIn("outcome-grid-wrap", html)
        self.assertIn("1.5100", html)
        self.assertIn("0.7600", html)

    def test_team_names_in_buttons_and_js(self):
        info = {"lambda_a": 1.2, "lambda_b": 0.9}
        html = bs.render_outcome_grid(info, "Brazil", "Morocco")
        self.assertIn("Brazil win</button>", html)
        self.assertIn("Morocco win</button>", html)
        self.assertIn("Brazil win", html)  # in JS LABELS
        self.assertIn("Morocco win", html)

    def test_html_escaping_in_buttons(self):
        info = {"lambda_a": 1.0, "lambda_b": 1.0}
        html = bs.render_outcome_grid(info, "R&B FC", "X's XI")
        self.assertIn("R&amp;B FC win</button>", html)
        # single quote must be JS-escaped in the LABELS string
        self.assertIn(r"X\'s XI win", html)

    def test_js_str_escapes_single_quote_and_backslash(self):
        self.assertEqual(bs._js_str("Côte d'Ivoire"), "Côte d\\'Ivoire")
        self.assertEqual(bs._js_str("back\\slash"), "back\\\\slash")
        self.assertEqual(bs._js_str("plain"), "plain")

    def test_script_tag_present(self):
        info = {"lambda_a": 1.51, "lambda_b": 0.76}
        html = bs.render_outcome_grid(info, "Brazil", "Morocco")
        self.assertIn("<script>", html)
        self.assertIn("</script>", html)

    def test_no_unsubstituted_placeholders(self):
        info = {"lambda_a": 1.51, "lambda_b": 0.76}
        html = bs.render_outcome_grid(info, "Brazil", "Morocco")
        self.assertNotIn("__LA__", html)
        self.assertNotIn("__LB__", html)
        self.assertNotIn("__TEAM_A__", html)
        self.assertNotIn("__TEAM_B__", html)


class SweatFahrenheitTests(unittest.TestCase):
    """render_sweat and _sweat_blurb display temperatures in °F, not °C."""

    def _info(self, temp_c=30.0, rh=60.0, wbgt=28.0, delta_a=8.0, delta_b=-2.0,
              dis_a=60, dis_b=10, sf=55, severity="High", mhi=60):
        return {
            "temp_c": temp_c, "rh_pct": rh, "wbgt_est": wbgt,
            "delta_a": delta_a, "delta_b": delta_b,
            "dis_a": dis_a, "dis_b": dis_b,
            "sf": sf, "severity": severity, "mhi": mhi,
            "source": "forecast", "as_of": "2026-06-14 06:00",
            "climate_controlled": False,
        }

    def test_temp_displayed_in_fahrenheit(self):
        html = bs.render_sweat(self._info(temp_c=30.0), "Brazil", "Morocco")
        self.assertIn("86°F", html)       # 30*9/5+32 = 86
        self.assertNotIn("30°C", html)

    def test_wbgt_displayed_in_fahrenheit(self):
        html = bs.render_sweat(self._info(wbgt=28.0), "Brazil", "Morocco")
        self.assertIn("82.4°F", html)     # 28*9/5+32 = 82.4
        self.assertNotIn("28°C", html)

    def test_team_delta_displayed_in_fahrenheit(self):
        html = bs.render_sweat(self._info(delta_a=10.0, delta_b=-5.0), "Scotland", "Panama")
        self.assertIn("+18.0°F vs home", html)   # 10*9/5 = 18
        self.assertIn("-9.0°F vs home", html)     # -5*9/5 = -9

    def test_crimson_threshold_still_uses_celsius(self):
        # delta_a=5.0°C (exactly on threshold) → crimson bar
        html = bs.render_sweat(self._info(delta_a=5.0), "Scotland", "Panama")
        self.assertIn("cond-dis-hot", html)
        # delta_a=4.9°C (just under) → no crimson
        html2 = bs.render_sweat(self._info(delta_a=4.9), "Scotland", "Panama")
        self.assertNotIn("cond-dis-hot", html2)

    def test_blurb_uses_fahrenheit(self):
        html = bs.render_sweat(self._info(sf=60, delta_a=8.0, delta_b=1.0,
                                          dis_a=70, dis_b=10), "Scotland", "Panama")
        self.assertIn("°F", html)
        self.assertNotIn("°C", html)

    def test_no_celsius_anywhere_in_output(self):
        html = bs.render_sweat(self._info(), "Brazil", "Morocco")
        self.assertNotIn("°C", html)

    def test_climate_controlled_unchanged(self):
        info = {"climate_controlled": True}
        html = bs.render_sweat(info, "Brazil", "Morocco")
        self.assertIn("climate-controlled", html)
        self.assertNotIn("°C", html)
        self.assertNotIn("°F", html)


if __name__ == "__main__":
    unittest.main()
