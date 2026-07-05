"""Tests for bracket.championship_odds — the EXACT title-race propagation.
HERMETIC: synthetic KnockoutMatch rows + deterministic p_fn, never the live data
or the real model (point-in-time assertions break at phase boundaries)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bracket as bk           # noqa: E402
import knockout as ko          # noqa: E402

# 16 distinct team names, one per R32 slot 73..88
TEAMS = {m: f"T{m}" for m in range(73, 89)}


def _r32_played(winner_of=None):
    """All 16 R32 ties played; winner_of maps match_no -> 'A'|'B' (default A)."""
    rows = []
    for m in range(73, 89):
        a, b = TEAMS[m], f"L{m}"                     # loser names never reappear
        side = (winner_of or {}).get(m, "A")
        rows.append(ko.KnockoutMatch(m, "R32", "2026-06-29", "16:00", "4:00 PM",
                                     "Gillette Stadium", "Foxborough", "USA", "",
                                     a, b, 1 if side == "A" else 0,
                                     0 if side == "A" else 1,
                                     "regulation", side, "played", ""))
    return rows


def _flat_p(a, b, m):
    return 0.5


class ChampionshipOddsTests(unittest.TestCase):
    def test_uniform_model_gives_uniform_title_odds(self):
        odds = bk.championship_odds(ko.by_no(_r32_played()), _flat_p)
        champ = odds["champion"]
        self.assertEqual(len(champ), 16)
        for t, p in champ.items():
            self.assertAlmostEqual(p, 1 / 16, places=12)
        self.assertAlmostEqual(sum(champ.values()), 1.0, places=12)

    def test_reach_probabilities_halve_per_round_under_uniform(self):
        odds = bk.championship_odds(ko.by_no(_r32_played()), _flat_p)
        r = odds["reach"][TEAMS[74]]                  # an R32 winner
        self.assertAlmostEqual(r["R16"], 1.0, places=12)   # appears in R16 for sure
        self.assertAlmostEqual(r["QF"], 0.5, places=12)
        self.assertAlmostEqual(r["SF"], 0.25, places=12)
        self.assertAlmostEqual(r["Final"], 0.125, places=12)

    def test_certain_favourite_walks_the_bracket(self):
        fav = TEAMS[74]
        odds = bk.championship_odds(ko.by_no(_r32_played()),
                                    lambda a, b, m: 1.0 if a == fav
                                    else (0.0 if b == fav else 0.5))
        self.assertAlmostEqual(odds["champion"][fav], 1.0, places=12)
        self.assertAlmostEqual(odds["reach"][fav]["Final"], 1.0, places=12)

    def test_played_tree_match_is_history_not_model(self):
        rows = _r32_played()
        # M89 (feeders 74, 77) already played: T77 beat T74 — the model must not re-run it
        rows.append(ko.KnockoutMatch(89, "R16", "2026-07-04", "17:00", "5:00 PM",
                                     "Lincoln Financial Field", "Philadelphia", "USA", "",
                                     TEAMS[74], TEAMS[77], 0, 2, "regulation", "B",
                                     "played", ""))
        fav = TEAMS[74]                               # "certain" per the model, but lost
        odds = bk.championship_odds(ko.by_no(rows),
                                    lambda a, b, m: 1.0 if a == fav
                                    else (0.0 if b == fav else 0.5))
        self.assertEqual(odds["champion"].get(fav, 0.0), 0.0)
        self.assertAlmostEqual(odds["reach"][TEAMS[77]]["QF"], 1.0, places=12)

    def test_eliminated_teams_are_absent(self):
        odds = bk.championship_odds(ko.by_no(_r32_played()), _flat_p)
        self.assertNotIn("L74", odds["champion"])     # R32 losers never reappear
        self.assertNotIn("L74", odds["reach"])

    def test_third_place_match_is_excluded(self):
        # M103 must not shift champion mass: total stays 1.0 (it's outside the tree)
        odds = bk.championship_odds(ko.by_no(_r32_played()), _flat_p)
        self.assertAlmostEqual(sum(odds["champion"].values()), 1.0, places=12)

    def test_render_md_orders_by_title_chance(self):
        fav = TEAMS[73]
        odds = bk.championship_odds(ko.by_no(_r32_played()),
                                    lambda a, b, m: 0.9 if a == fav
                                    else (0.1 if b == fav else 0.5))
        md = bk.render_title_race_md(odds, limit=3)
        lines = [ln for ln in md.splitlines()
                 if ln.startswith("| T") and not ln.startswith("| Team")]
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith(f"| {fav} "))


if __name__ == "__main__":
    unittest.main()
