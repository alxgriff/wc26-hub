"""Unit tests for scripts/site_content.py and the full-site build."""

import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import site_content as sc
import build_site as bs

REPO = Path(__file__).resolve().parents[1]


class SlugTests(unittest.TestCase):
    def test_canon_slugs(self):
        self.assertEqual(sc.slugify("Côte d'Ivoire"), "cote-divoire")
        self.assertEqual(sc.slugify("Türkiye"), "turkiye")
        self.assertEqual(sc.slugify("Bosnia and Herzegovina"), "bosnia-and-herzegovina")
        self.assertEqual(sc.slugify("Curaçao"), "curacao")
        self.assertEqual(sc.slugify("United States"), "united-states")

    def test_slugs_unique_across_canon(self):
        import standings as st
        matches = st.load_fixtures(REPO / "data" / "fixtures.csv")
        teams = {m.team_a for m in matches} | {m.team_b for m in matches}
        slugs = [sc.slugify(t) for t in teams]
        self.assertEqual(len(slugs), len(set(slugs)))


class KbParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles, cls.warnings = sc.parse_kb(REPO / "kb" / "2026_fifa_world_cup_guide.md")

    def test_all_48_teams_parsed(self):
        self.assertEqual(len(self.profiles), 48)
        import standings as st
        matches = st.load_fixtures(REPO / "data" / "fixtures.csv")
        fixture_teams = {m.team_a for m in matches} | {m.team_b for m in matches}
        self.assertEqual(set(self.profiles), fixture_teams)

    def test_every_profile_complete(self):
        for team, p in self.profiles.items():
            self.assertTrue(p.tactical, f"{team}: no tactical preview")
            self.assertTrue(p.squad, f"{team}: no squad")
            self.assertTrue(p.key_player, f"{team}: no key player")
            self.assertIn("Manager", p.facts, f"{team}: no manager fact")

    def test_strap_facts_keep_full_history_line(self):
        mex = self.profiles["Mexico"]
        self.assertIn("Recent finish", mex.facts["World Cup history"])
        self.assertEqual(mex.facts["Projected XI shape"], "4-3-3")
        self.assertEqual(mex.group, "A")

    def test_squad_positions(self):
        mex = self.profiles["Mexico"]
        self.assertEqual(mex.squad[0][0], "Goalkeepers")
        self.assertIn("Raúl Rangel", mex.squad[0][1])


class CardParseTests(unittest.TestCase):
    def test_inline_label_format(self):
        card = ("## A2: 🇰🇷 South Korea vs Czechia 🇨🇿\n"
                "**Group A, MD1** | Thu June 11\n\n"
                "**The Matchup:** Two back-three teams.\nSecond line.\n\n"
                "**Key Duel:** Kim vs Schick.\n"
                "**Stakes:** *[Edition day]*\n"
                "**Margin Notes:** Koubek, 74.\n")
        header, sections = sc.parse_card(card)
        labels = [l for l, _ in sections]
        self.assertIn("The Matchup", labels)
        body = dict(sections)
        self.assertIn("Second line.", body["The Matchup"])
        self.assertEqual(body["Key Duel"], "Kim vs Schick.")
        # the strap line lands in the preamble, not dropped
        self.assertIn("Group A, MD1", body.get("", ""))

    def test_h2_section_format(self):
        card = ("# 🇨🇦 Canada vs Bosnia 🇧🇦\n"
                "**Group B, Matchday 1** | Friday\n\n"
                "## The Matchup\nMirror-match prose.\n\n"
                "## Key Duel\n**Koné** vs scrum.\n\n"
                "## Stakes\n*[Edition day]*\n")
        header, sections = sc.parse_card(card)
        body = dict(sections)
        self.assertEqual(body["The Matchup"], "Mirror-match prose.")
        self.assertIn("Koné", body["Key Duel"])

    def test_md3_combined_call_odds_label(self):
        card = ("### B5: Switzerland vs Canada (3:00 PM ET)\n"
                "**The Matchup:** Title bout.\n"
                "**The Call / Odds & Best Bet:** *[Model pending — lean: draw.]*\n")
        _, sections = sc.parse_card(card)
        body = dict(sections)
        self.assertIn("The Call / Odds & Best Bet", body)


class MdToHtmlTests(unittest.TestCase):
    def test_escape_first(self):
        out = sc.md_to_html("a <script>bad</script> & **bold**")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<strong>bold</strong>", out)

    def test_bullets_and_paragraphs(self):
        out = sc.md_to_html("Para one.\n\n- item *one*\n- item two\n\nPara two.")
        self.assertIn("<ul><li>item <em>one</em></li><li>item two</li></ul>", out)
        self.assertEqual(out.count("<p>"), 2)


class FullSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.warnings = bs.build_site(cls.out, date(2026, 6, 12), "2026-06-12 09:00",
                                     predictor=None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_page_counts(self):
        self.assertEqual(len(list((self.out / "teams").glob("*.html"))), 48)
        self.assertEqual(len(list((self.out / "matches").glob("*.html"))), 72)
        self.assertTrue((self.out / "index.html").exists())

    def test_no_kb_warnings(self):
        self.assertEqual([w for w in self.warnings if w.startswith("kb:")], [])

    def test_internal_links_resolve(self):
        href = re.compile(r'href="((?:\.\./)?(?:teams|matches)/[^"#]+)"')
        for page in [self.out / "index.html",
                     self.out / "teams" / "mexico.html",
                     self.out / "matches" / "A1.html"]:
            text = page.read_text(encoding="utf-8")
            for target in href.findall(text):
                resolved = (page.parent / target).resolve()
                self.assertTrue(resolved.exists(), f"{page.name} -> {target} missing")

    def test_match_page_placeholder_call_without_predictor(self):
        a2 = (self.out / "matches" / "A2.html").read_text(encoding="utf-8")
        self.assertIn("Model pending", a2)
        self.assertIn("Pre-baked lean", a2)
        self.assertIn("South Korea", a2)
        self.assertIn("2–1", a2)  # played scoreline shown

    def test_team_page_contains_profile_and_fixtures(self):
        mex = (self.out / "teams" / "mexico.html").read_text(encoding="utf-8")
        self.assertIn("Tactical profile", mex)
        self.assertIn("Raúl Jiménez", mex)
        self.assertIn("matches/A1.html", mex)
        self.assertIn("2–0 W", mex)
        self.assertIn("Group A", mex)

    def test_index_links_teams_and_matches(self):
        idx = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="teams/mexico.html"', idx)
        self.assertIn('href="matches/B1.html"', idx)

    def test_info_injection_renders_probbar_and_grading(self):
        info = {"p_a": 0.5, "p_draw": 0.3, "p_b": 0.2,
                "modal_score": (1, 0), "total": 2.4,
                "over25": 0.45, "btts": 0.4,
                "hfa": None, "consensus": True, "source": "Opta"}
        import standings as st
        import build_edition as be
        s = st.compute_standings(st.load_fixtures(REPO / "data" / "fixtures.csv"))
        rows = be.read_rows(REPO / "data" / "fixtures.csv")
        page = bs.render_match_page(rows[0], s, {}, REPO / "cards", info,
                                    bs._site_css())  # A1, played 2-0
        self.assertIn("probbar", page)
        self.assertIn("50%", page)
        self.assertIn("2-source consensus", page)
        self.assertIn("Graded:", page)         # played match gets the call graded
        self.assertIn("Brier", page)

    def test_md2_page_of_unstarted_group_has_accurate_stakes(self):
        b3 = (self.out / "matches" / "B3.html").read_text(encoding="utf-8")
        self.assertNotIn("opens with this matchday", b3)
        self.assertIn("hasn&#x27;t kicked off yet", b3)

    def test_machine_prefixes_stripped_from_quotes(self):
        c1 = (self.out / "matches" / "C1.html").read_text(encoding="utf-8")
        self.assertNotIn("Model pending — lean:", c1)
        self.assertNotIn("markets to watch:", c1)
        i5 = (self.out / "matches" / "I5.html").read_text(encoding="utf-8")
        # MD3 combined Call/Odds lean must not be duplicated as a markets quote
        self.assertNotIn("Markets to watch (pre-baked)", i5)

    def test_clean_slot(self):
        self.assertEqual(bs._clean_slot("*[Model pending — lean: Bosnia narrowly.]*"),
                         "Bosnia narrowly.")
        self.assertEqual(bs._clean_slot("*[Phase 3 — markets to watch: the draw.]*"),
                         "the draw.")
        self.assertIsNone(bs._clean_slot(None))

    def test_stale_pages_removed_on_rebuild(self):
        stale = self.out / "teams" / "old-team-name.html"
        stale.write_text("stale", encoding="utf-8")
        warnings = bs.build_site(self.out, date(2026, 6, 12), "t", predictor=None)
        self.assertFalse(stale.exists())
        self.assertTrue(any("stale" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
