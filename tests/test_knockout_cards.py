"""Tests for scripts/knockout_cards.py — grounded, fail-soft knockout card generation."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import knockout as ko            # noqa: E402
import knockout_cards as kc      # noqa: E402
import site_content as sc        # noqa: E402


# --- injectable fake Anthropic client (mirrors the stakes_blurb test pattern) ---
class _Block:
    def __init__(self, t):
        self.type, self.text = "text", t


class _Resp:
    def __init__(self, t):
        self.content, self.stop_reason = [_Block(t)], "end_turn"


class FakeClient:
    def __init__(self, text="", raise_exc=None):
        self.text, self.raise_exc, self.last_user = text, raise_exc, None
        self.messages = self

    def create(self, **kw):
        self.last_user = kw["messages"][0]["content"]
        if self.raise_exc:
            raise self.raise_exc
        return _Resp(self.text)


TAC = """## The Matchup
France press high; Brazil build through Casemiro.

## Key Duel
Mbappe vs Marquinhos.

## Watch For
- Mbappe runs in behind.

## Margin Notes
- These teams have met before.
"""


def _profile(team, group="A"):
    return sc.TeamProfile(team=team, group=group, facts={"Manager": "X"},
                          squad=[("GK", "Keeper")], tactical=["Plays 4-3-3."],
                          key_player="Star", rising_star="Kid", fun_fact="Trivia.")


def _km(no, team_a="France", team_b="Brazil", **kw):
    base = dict(match_no=no, round=ko.round_of(no), date_et="2026-06-28", kickoff_et_24h="15:00",
                kickoff_et="3:00 PM", stadium="SoFi Stadium", city="Inglewood", country="USA",
                tv_us="Fox", team_a=team_a, team_b=team_b, score_a=None, score_b=None,
                decided_by="", winner="", status="scheduled", notes="")
    base.update(kw)
    return ko.KnockoutMatch(**base)


def _profiles():
    return {"France": _profile("France"), "Brazil": _profile("Brazil")}


class GenerateCardTests(unittest.TestCase):
    def test_full_card_has_all_sections(self):
        card = kc.generate_card(_km(73), _profiles(), kc.build_facts(_km(73)),
                                client=FakeClient(TAC))
        self.assertIsNotNone(card)
        for h in ("# Round of 32: France vs Brazil", "## The Matchup", "## Key Duel",
                  "## Watch For", "## Projected Shapes & Selection Questions", "## Stakes",
                  "## The Call", "## Odds & Best Bet", "## Margin Notes"):
            self.assertIn(h, card)
        self.assertIn("Mbappe", card)                    # tactical content from the model
        self.assertIn("M73", card)
        self.assertIn("(verify before use)", card)       # projected-shapes honesty tag

    def test_card_parses_with_site_content(self):
        card = kc.generate_card(_km(73), _profiles(), kc.build_facts(_km(73)),
                                client=FakeClient(TAC))
        header, sections = sc.parse_card(card)            # must be readable by the site parser
        labels = [lbl for lbl, _ in sections]
        self.assertIn("The Matchup", labels)
        self.assertIn("The Call", labels)

    def test_grounding_pack_carries_only_supplied_profiles(self):
        client = FakeClient(TAC)
        kc.generate_card(_km(73), _profiles(), kc.build_facts(_km(73)), client=client)
        self.assertIn("France profile", client.last_user)
        self.assertIn("Brazil profile", client.last_user)
        self.assertIn("Plays 4-3-3.", client.last_user)   # tactical text grounded in
        self.assertIn("grounded ONLY", client.last_user)

    def test_failsoft_no_client(self):
        self.assertIsNone(kc.generate_card(_km(73), _profiles(), client=None))

    def test_failsoft_missing_profile(self):
        self.assertIsNone(kc.generate_card(_km(73), {"France": _profile("France")},
                                           client=FakeClient(TAC)))

    def test_failsoft_unresolved_matchup(self):
        self.assertIsNone(kc.generate_card(_km(74, team_a="", team_b=""), _profiles(),
                                           client=FakeClient(TAC)))

    def test_failsoft_client_raises(self):
        self.assertIsNone(kc.generate_card(_km(73), _profiles(),
                                           client=FakeClient(raise_exc=RuntimeError("boom"))))

    def test_failsoft_empty_response(self):
        self.assertIsNone(kc.generate_card(_km(73), _profiles(), client=FakeClient("")))


class FactsTests(unittest.TestCase):
    def test_build_facts_without_model_has_no_call(self):
        f = kc.build_facts(_km(73))
        self.assertNotIn("call", f)
        self.assertIn("Round of 16", f["stakes"])          # R32 winner earns an R16 place
        self.assertIn("straight from the group stage", f["path"])
        self.assertEqual(f["round_name"], "Round of 32")

    def test_parse_sections(self):
        d = kc._parse_sections(TAC)
        self.assertEqual(set(d), {"The Matchup", "Key Duel", "Watch For", "Margin Notes"})
        self.assertIn("Casemiro", d["The Matchup"])


class PersistenceTests(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            kc.save_card(73, "# card body\n", cards_dir=Path(d))
            self.assertEqual(kc.load_ko_card(73, cards_dir=Path(d)), "# card body")
            self.assertIsNone(kc.load_ko_card(99, cards_dir=Path(d)))


if __name__ == "__main__":
    unittest.main()
