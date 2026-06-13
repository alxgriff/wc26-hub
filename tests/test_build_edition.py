"""Unit tests for scripts/build_edition.py using synthetic cards + tmp fixtures.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_edition as be
import standings as st


# ---- synthetic card files (one card per format) ----------------------------

MD1 = """# Match Cards — Matchday 1

---

## A1: Team Alpha vs Team Beta
**Group A, MD1** | Thu June 11 — 3:00 PM ET | Somewhere

**The Matchup:** Alpha press, Beta sit.

**Stakes:** *[Edition day placeholder.]*
**The Call:** *[Model pending — lean Alpha.]*
**Odds & Best Bet:** *[Phase 3 — watch the draw.]*

**Margin Notes:** Alpha trivia.

---

## A2: Team Gamma vs Team Delta
**Group A, MD1** | later

**The Matchup:** Gamma vs Delta.

**Stakes:** *[placeholder]*
**The Call:** *[pending]*
**Odds & Best Bet:** *[Phase 3]*

**Margin Notes:** Gamma trivia.
"""

MD3 = """# Match Cards — Matchday 3

---

## June 24 — Group A decided

### A5: Team Alpha vs Team Beta (9:00 PM ET, Fox — Somewhere)
**The Matchup:** The decider.
**Stakes:** *[Edition day — scenarios.]*
**The Call / Odds & Best Bet:** *[Model pending.]*
**Margin Notes:** Decider trivia.

### A6: Team Gamma vs Team Delta (9:00 PM ET, FS1 — Elsewhere)
**The Matchup:** Simultaneous.
**Stakes:** *[Edition day.]*
**The Call / Odds & Best Bet:** *[pending]*
**Margin Notes:** more trivia.
"""

TEMPLATE = """# Match Card Template + Samples

## The template
Boilerplate about the nine sections, naming no team.

---
---

# Canada vs Bosnia and Herzegovina
**Group B, Matchday 1** | Friday — 3:00 PM ET (Fox) | BMO Field, Toronto

## The Matchup
A mirror match.

## Stakes
*[Edition day placeholder for Canada.]*

## The Call
*[Pending aggregate model.]*

## Odds & Best Bet
*[Phase 3 — market snapshot.]*

## Margin Notes
- Canada trivia.

---
---

## Pre-bake status

| Batch | Status |
|---|---|
| samples | done |
"""


def write_cards(tmp: Path, **files: str) -> Path:
    cards = tmp / "cards"
    cards.mkdir()
    for name, text in files.items():
        (cards / f"{name}.md").write_text(text, encoding="utf-8")
    return cards


# ---- (a) card extraction for all three formats -----------------------------

class CardExtractionTests(unittest.TestCase):
    def test_md1_id_format(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md1=MD1)
            card, src = be.extract_card("A1", "Team Alpha", "Team Beta", cards)
            self.assertEqual(src, "md1.md")
            self.assertIn("## A1: Team Alpha vs Team Beta", card)
            self.assertIn("**The Matchup:** Alpha press", card)
            # sliced at the next card, not bleeding into A2
            self.assertNotIn("A2:", card)
            self.assertNotIn("Team Gamma", card)

    def test_md3_id_format_nested_under_day_heading(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md3=MD3)
            card, src = be.extract_card("A5", "Team Alpha", "Team Beta", cards)
            self.assertEqual(src, "md3.md")
            self.assertIn("### A5: Team Alpha vs Team Beta", card)
            self.assertNotIn("### A6", card)
            self.assertNotIn("## June 24", card)  # day heading not pulled in

    def test_template_h1_by_team_names_with_md1_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            # md1 exists but does NOT contain B1 — must fall through to template.md
            cards = write_cards(Path(d), md1=MD1, template=TEMPLATE)
            card, src = be.extract_card("B1", "Canada", "Bosnia and Herzegovina", cards)
            self.assertEqual(src, "template.md")
            self.assertIn("# Canada vs Bosnia and Herzegovina", card)
            self.assertIn("## Stakes", card)
            self.assertNotIn("Pre-bake status", card)   # stopped at the status table
            self.assertNotIn("United States", card)


# ---- (b) the 🌙 late-cap editorial-date mapping ----------------------------

FIX_HEADER = "match_id,group,matchday,date_et,kickoff_et_24h,kickoff_et,team_a,team_b,stadium,city,country,tv_us,score_a,score_b,status,notes"


def _fix_row(mid, date_et, k24, ket, a="X", b="Y"):
    g = mid[0]
    md = (int(mid[1]) + 1) // 2
    return f"{mid},{g},{md},{date_et},{k24},{ket},{a},{b},Stad,City,USA,Fox,,,scheduled,"


class LateCapMappingTests(unittest.TestCase):
    def setUp(self):
        rows = "\n".join([
            FIX_HEADER,
            _fix_row("D1", "2026-06-12", "21:00", "9:00 PM"),
            _fix_row("D2", "2026-06-14", "00:00", "12:00 AM"),   # 🌙 → June 13
            _fix_row("E1", "2026-06-14", "13:00", "1:00 PM"),    # plain June 14
            _fix_row("J2", "2026-06-17", "00:00", "12:00 AM"),   # 🌙 → June 16
            _fix_row("F4", "2026-06-21", "00:00", "12:00 AM"),   # 🌙 → June 20
        ]) + "\n"
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        self.tmp.write(rows)
        self.tmp.close()
        self.rows = be.read_rows(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink()

    def test_each_late_cap_lands_on_previous_evening(self):
        self.assertEqual(be.editorial_date_of("2026-06-14", "00:00"), date(2026, 6, 13))
        self.assertEqual(be.editorial_date_of("2026-06-17", "00:00"), date(2026, 6, 16))
        self.assertEqual(be.editorial_date_of("2026-06-21", "00:00"), date(2026, 6, 20))

    def test_d2_appears_on_june_13_not_june_14(self):
        june13 = [r["match_id"] for r in be.select_matches(self.rows, date(2026, 6, 13))]
        june14 = [r["match_id"] for r in be.select_matches(self.rows, date(2026, 6, 14))]
        self.assertIn("D2", june13)
        self.assertNotIn("D2", june14)
        self.assertEqual(june14, ["E1"])           # plain row only
        self.assertIn("J2", [r["match_id"] for r in be.select_matches(self.rows, date(2026, 6, 16))])
        self.assertIn("F4", [r["match_id"] for r in be.select_matches(self.rows, date(2026, 6, 20))])

    def test_late_cap_sorts_after_evening_games(self):
        # the 🌙 game kicks off at 00:00 but must sort after the evening slate
        # it shares an edition with — the sort key pushes midnight to +24h.
        evening = {"_late_cap": False, "kickoff_et_24h": "21:00"}
        midnight = {"_late_cap": True, "kickoff_et_24h": "00:00"}
        self.assertGreater(be._kickoff_sort_key(midnight), be._kickoff_sort_key(evening))

    def test_live_fixtures_late_cap_set_is_exactly_the_sanctioned_three(self):
        repo = Path(__file__).resolve().parents[1]
        rows = be.read_rows(repo / "data" / "fixtures.csv")
        midnight = {r["match_id"] for r in rows
                    if (r.get("kickoff_et_24h") or "").strip() == "00:00"}
        self.assertEqual(midnight, be.EXPECTED_LATE_CAPS)   # two-way: no more, no fewer

    def test_unexpected_midnight_id_is_not_shifted(self):
        # a stray 00:00 on a non-sanctioned id stays on its own date
        self.assertEqual(be.editorial_date_of("2026-06-14", "00:00", "E1"), date(2026, 6, 14))
        self.assertEqual(be.editorial_date_of("2026-06-14", "00:00", "D2"), date(2026, 6, 13))

    def test_kickoff_sort_key_tolerates_malformed(self):
        self.assertEqual(be._kickoff_sort_key({"kickoff_et_24h": "TBD"}), 99 * 60)
        self.assertGreater(be._kickoff_sort_key({"kickoff_et_24h": "TBD"}),
                           be._kickoff_sort_key({"kickoff_et_24h": "21:00"}))

    def test_overnight_played_with_blank_score_flags_not_dash(self):
        rows = [{"match_id": "A1", "team_a": "X", "team_b": "Y", "status": "played",
                 "score_a": "", "score_b": "", "notes": "",
                 "_editorial": date(2026, 6, 11), "_late_cap": False}]
        out = "\n".join(be._overnight_section(rows, date(2026, 6, 11)))
        self.assertIn("result not yet entered", out)


# ---- (c) Stakes replacement leaves The Call / Odds untouched ---------------

class StakesInjectionTests(unittest.TestCase):
    BODY = "_Group A — table_\n\n| table |\n\nAlpha lead on 3 pts."

    def test_inline_layout_preserves_call_and_odds(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md1=MD1)
            card, _ = be.extract_card("A1", "Team Alpha", "Team Beta", cards)
        out, replaced = be.inject_stakes(card, self.BODY)
        self.assertTrue(replaced)
        self.assertIn("Alpha lead on 3 pts.", out)
        self.assertNotIn("*[Edition day placeholder.]*", out)
        # the other two live slots are byte-identical to the source card
        self.assertIn("**The Call:** *[Model pending — lean Alpha.]*", out)
        self.assertIn("**Odds & Best Bet:** *[Phase 3 — watch the draw.]*", out)

    def test_section_layout_preserves_call_and_odds(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), template=TEMPLATE)
            card, _ = be.extract_card("B1", "Canada", "Bosnia and Herzegovina", cards)
        out, replaced = be.inject_stakes(card, self.BODY)
        self.assertTrue(replaced)
        self.assertIn("## Stakes", out)
        self.assertIn("Alpha lead on 3 pts.", out)
        self.assertNotIn("*[Edition day placeholder for Canada.]*", out)
        self.assertIn("## The Call", out)
        self.assertIn("*[Pending aggregate model.]*", out)
        self.assertIn("*[Phase 3 — market snapshot.]*", out)

    def test_no_slot_reports_not_replaced(self):
        out, replaced = be.inject_stakes("## Card\n\nNo stakes here.\n", self.BODY)
        self.assertFalse(replaced)
        self.assertNotIn(self.BODY, out)


# ---- (d) missing card → placeholder + warning, not a crash -----------------

def _full_group_a_rows(opener_date="2026-06-12"):
    """A complete, unplayed Group A so compute_standings is warning-free."""
    return "\n".join([
        FIX_HEADER,
        _fix_row("A1", opener_date, "15:00", "3:00 PM", "Alpha", "Beta"),
        _fix_row("A2", "2026-06-18", "21:00", "9:00 PM", "Gamma", "Delta"),
        _fix_row("A3", "2026-06-18", "12:00", "12:00 PM", "Beta", "Gamma"),
        _fix_row("A4", "2026-06-18", "15:00", "3:00 PM", "Alpha", "Delta"),
        _fix_row("A5", "2026-06-24", "21:00", "9:00 PM", "Beta", "Delta"),
        _fix_row("A6", "2026-06-24", "21:00", "9:00 PM", "Alpha", "Gamma"),
    ]) + "\n"


class MissingCardTests(unittest.TestCase):
    def _build(self, cards_dir):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write(_full_group_a_rows())
            path = f.name
        try:
            rows = be.read_rows(path)
            standings = st.compute_standings(st.load_fixtures(path))
            return be.build_edition(date(2026, 6, 12), rows, standings, cards_dir)
        finally:
            Path(path).unlink()

    def test_missing_card_yields_placeholder_and_warning(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / "cards"
            empty.mkdir()
            edition, warnings = self._build(empty)        # A1 card absent
        self.assertIn("Card not found", edition)
        self.assertIn("## A1: Alpha vs Beta", edition)
        self.assertTrue(any("A1" in w and "no card" in w for w in warnings))
        # placeholder still gets a factual Stakes block, never invented prose
        self.assertIn("Group A opens", edition)

    def test_present_card_fills_stakes_no_warning(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md1=MD1)
            edition, warnings = self._build(cards)
        self.assertIn("## A1: Team Alpha vs Team Beta", edition)
        self.assertIn("Group A opens", edition)            # stakes injected
        self.assertNotIn("*[Edition day placeholder.]*", edition)
        self.assertIn("**The Call:** *[Model pending — lean Alpha.]*", edition)
        self.assertEqual(warnings, [])


# ---- Stakes sentence enriches once results roll in -------------------------

def _played_group_a():
    """Group A after two matchdays, with real separation:
    Alpha 6 (2-0-0, +4) · Gamma 2 (0-2-0, 0) · Delta 1 (0-1-1, -2) · Beta 1 (0-1-1, -2)."""
    M = st.Match
    return st.compute_standings([
        M("A1", "A", 1, "Alpha", "Beta", 2, 0, "played"),
        M("A2", "A", 1, "Gamma", "Delta", 1, 1, "played"),
        M("A3", "A", 2, "Beta", "Gamma", 0, 0, "played"),
        M("A4", "A", 2, "Alpha", "Delta", 3, 1, "played"),
        M("A5", "A", 3, "Beta", "Delta", None, None, "scheduled"),
        M("A6", "A", 3, "Alpha", "Gamma", None, None, "scheduled"),
    ])


class StakesContextTests(unittest.TestCase):
    def test_zero_played_is_the_bare_opener(self):
        s = st.compute_standings([
            st.Match("A1", "A", 1, "Alpha", "Beta", None, None, "scheduled"),
            st.Match("A2", "A", 1, "Gamma", "Delta", None, None, "scheduled"),
        ])
        self.assertEqual(be.stakes_sentence(s.groups["A"], "Alpha", "Beta"),
                         "Group A opens with this matchday — all four teams start on 0 points.")

    def test_split_cutline_reports_positions_records_and_gap(self):
        gt = _played_group_a().groups["A"]
        out = be.stakes_sentence(gt, "Gamma", "Delta")   # 2nd vs 3rd
        self.assertIn("After 4 matches in Group A:", out)
        self.assertIn("Gamma 2nd on 2 pts (0-2-0, 0 GD)", out)
        self.assertIn("Delta 3rd on 1 pt (0-1-1, -2 GD)", out)
        self.assertIn("Alpha lead the group on 6 pts", out)
        self.assertIn("Gamma hold a top-two spot", out)
        self.assertIn("Delta 1 pt back of the cutline", out)
        self.assertIn("best eight third-placed teams also advance", out)
        # strictly descriptive — no outcome-scenario verbs
        for banned in ("would", "if they", "clinch", "through if", "needs"):
            self.assertNotIn(banned, out.lower())

    def test_both_inside_top_two(self):
        gt = _played_group_a().groups["A"]
        out = be.stakes_sentence(gt, "Alpha", "Gamma")   # 1st vs 2nd
        self.assertIn("both hold top-two places as it stands", out)


# ---- MD3 days route the Stakes slot through scenarios.py -------------------

def _played(mid, a, b, sa, sb, date_et="2026-06-18"):
    g, md = mid[0], (int(mid[1]) + 1) // 2
    return f"{mid},{g},{md},{date_et},15:00,3:00 PM,{a},{b},Stad,City,USA,Fox,{sa},{sb},played,"


def _sched_md3(mid, a, b):
    g, md = mid[0], (int(mid[1]) + 1) // 2
    return f"{mid},{g},{md},2026-06-24,21:00,9:00 PM,{a},{b},Stad,City,USA,Fox,,,scheduled,"


# Group A through MD2; MD3 = {A5: Team Alpha vs Team Beta, A6: Team Gamma vs Team Delta}.
GROUP_A_MD3_CSV = "\n".join([
    FIX_HEADER,
    _played("A1", "Team Alpha", "Team Gamma", 2, 0),
    _played("A2", "Team Beta", "Team Delta", 1, 0),
    _played("A3", "Team Alpha", "Team Delta", 1, 0),
    _played("A4", "Team Beta", "Team Gamma", 1, 1),
    _sched_md3("A5", "Team Alpha", "Team Beta"),
    _sched_md3("A6", "Team Gamma", "Team Delta"),
]) + "\n"


class Md3IntegrationTests(unittest.TestCase):
    def _build_md3_edition(self):
        import tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write(GROUP_A_MD3_CSV)
            path = f.name
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md3=MD3)
            try:
                rows = be.read_rows(path)
                matches = st.load_fixtures(path)
                standings = st.compute_standings(matches)
                return be.build_edition(date(2026, 6, 24), rows, standings, cards, matches=matches)
            finally:
                Path(path).unlink()

    def test_md3_card_carries_scenario_stakes_not_the_factual_sentence(self):
        edition, warnings = self._build_md3_edition()
        self.assertEqual(warnings, [])
        # scenario content is present...
        self.assertIn("kick off simultaneously", edition)
        self.assertIn("| Team | Top 2 | 3rd | Out | Margin |", edition)
        self.assertIn("**Team Alpha:**", edition)
        self.assertTrue(any(k in edition for k in ("Win:", "through (top 2)")))
        # ...and the non-MD3 factual one-liner is NOT used on this day
        self.assertNotIn("After 4 matches in Group A:", edition)
        # The Call / Odds slot in the md3 card is still untouched
        self.assertIn("**The Call / Odds & Best Bet:** *[Model pending.]*", edition)

    def test_without_matches_md3_falls_back_to_factual(self):
        # same fixtures, but build_edition called without `matches` -> factual stakes
        import tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write(GROUP_A_MD3_CSV)
            path = f.name
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md3=MD3)
            try:
                rows = be.read_rows(path)
                standings = st.compute_standings(st.load_fixtures(path))
                edition, _ = be.build_edition(date(2026, 6, 24), rows, standings, cards)
            finally:
                Path(path).unlink()
        self.assertNotIn("kick off simultaneously", edition)
        self.assertIn("After 4 matches in Group A:", edition)   # factual fallback


# ---- Phase 4: The Call injection + Overnight grading ------------------------

CALL_BODY = "**Alpha 61% · Draw 23% · Beta 16%** — consensus. Predicted score **1–0**."


class CallInjectionTests(unittest.TestCase):
    def test_inline_layout_fills_call_keeps_lean_and_odds(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md1=MD1)
            card, _ = be.extract_card("A1", "Team Alpha", "Team Beta", cards)
        out, ok = be.inject_call(card, CALL_BODY)
        self.assertTrue(ok)
        self.assertIn(CALL_BODY, out)
        self.assertIn("_Pre-baked lean: Model pending — lean Alpha._", out)
        # Odds slot byte-identical, Stakes untouched
        self.assertIn("**Odds & Best Bet:** *[Phase 3 — watch the draw.]*", out)
        self.assertIn("**Stakes:** *[Edition day placeholder.]*", out)

    def test_md3_combined_slot_splits_call_from_odds(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md3=MD3)
            card, _ = be.extract_card("A5", "Team Alpha", "Team Beta", cards)
        out, ok = be.inject_call(card, CALL_BODY)
        self.assertTrue(ok)
        self.assertIn(CALL_BODY, out)
        self.assertIn("_Pre-baked lean: Model pending._", out)
        self.assertIn("**Odds & Best Bet:** *[Phase 5 — market snapshot pending.]*", out)
        self.assertNotIn("**The Call / Odds & Best Bet:**", out)

    def test_template_section_layout(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), template=TEMPLATE)
            card, _ = be.extract_card("B1", "Canada", "Bosnia and Herzegovina", cards)
        out, ok = be.inject_call(card, CALL_BODY)
        self.assertTrue(ok)
        self.assertIn("## The Call", out)
        self.assertIn(CALL_BODY, out)
        self.assertIn("_Pre-baked lean: Pending aggregate model._", out)
        self.assertIn("*[Phase 3 — market snapshot.]*", out)   # odds untouched

    def test_no_call_slot_reports_not_replaced(self):
        out, ok = be.inject_call("## Card\n\nNothing here.", CALL_BODY)
        self.assertFalse(ok)


class OddsInjectionTests(unittest.TestCase):
    ODDS_BODY = "| Market | ... |\n\n**No bet** — nothing clears 3%."

    def test_inline_layout_fills_odds_keeps_hint(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md1=MD1)
            card, _ = be.extract_card("A1", "Team Alpha", "Team Beta", cards)
        out, ok = be.inject_odds(card, self.ODDS_BODY)
        self.assertTrue(ok)
        self.assertIn("**No bet**", out)
        self.assertIn("_Pre-baked note: Phase 3 — watch the draw._", out)

    def test_md3_combined_slot_works_after_call_injection(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), md3=MD3)
            card, _ = be.extract_card("A5", "Team Alpha", "Team Beta", cards)
        card, _ = be.inject_call(card, "call body")          # splits the slot
        out, ok = be.inject_odds(card, self.ODDS_BODY)
        self.assertTrue(ok)
        self.assertIn("**No bet**", out)
        self.assertIn("call body", out)                       # both injected

    def test_template_section_layout(self):
        with tempfile.TemporaryDirectory() as d:
            cards = write_cards(Path(d), template=TEMPLATE)
            card, _ = be.extract_card("B1", "Canada", "Bosnia and Herzegovina", cards)
        out, ok = be.inject_odds(card, self.ODDS_BODY)
        self.assertTrue(ok)
        self.assertIn("## Odds & Best Bet", out)
        self.assertIn("**No bet**", out)


class OvernightGradingTests(unittest.TestCase):
    def test_graded_match_shows_check_brier_and_cumulative(self):
        fixtures = "\n".join([
            FIX_HEADER,
            "A1,A,1,2026-06-11,15:00,3:00 PM,X,Y,S,C,USA,Fox,2,0,played,",
        ]) + "\n"
        import tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write(fixtures)
            path = f.name
        try:
            rows = be.read_rows(path)
        finally:
            Path(path).unlink()
        graded = {"A1": {"p": (0.7, 0.2, 0.1), "outcome": 0, "brier": 0.14,
                         "correct": True, "predicted_score": "2-0"}}
        out = "\n".join(be._overnight_section(rows, date(2026, 6, 11), graded=graded,
                                              cumulative="Ledger to date: 1 graded..."))
        self.assertIn("✓ correct", out)
        self.assertIn("Brier 0.140", out)
        self.assertIn("predicted 2-0", out)
        self.assertIn("Ledger to date: 1 graded...", out)

    def test_no_grading_keeps_placeholder_line(self):
        out = "\n".join(be._overnight_section([], date(2026, 6, 11)))
        self.assertIn("No matches on the previous editorial date", out)


if __name__ == "__main__":
    unittest.main()
