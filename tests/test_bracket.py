"""Tests for scripts/bracket.py — the as-it-stands 2026 knockout-bracket projector.

Covers the fixed template/tree invariants, the Annex C table integrity, the
projection of a fully-played synthetic tournament (R32 resolves, third assignment
matches Annex C), and the gating that omits unstarted groups.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bracket as bk      # noqa: E402
import standings as st    # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SNAP = REPO / "tests" / "fixtures" / "site_snapshot" / "fixtures.csv"


def _full_groups() -> list:
    """All 12 groups fully played: g1>g2>g3>g4, with g3's GD = -(k) (k=1..12 by
    group letter) so the best-8 thirds rank A..H deterministically (combo ABCDEFGH)."""
    matches = []
    for k, g in enumerate("ABCDEFGHIJKL", start=1):
        t = [f"{g}1", f"{g}2", f"{g}3", f"{g}4"]

        def M(no, a, b, sa, sb):
            return st.Match(f"{g}{no}", g, (no + 1) // 2, a, b, sa, sb, "played")
        matches += [
            M(1, t[0], t[1], 1, 0), M(2, t[0], t[2], 1, 0), M(3, t[0], t[3], 1, 0),
            M(4, t[1], t[2], k, 0),                 # g2 beats g3 by k -> g3 GD = -k
            M(5, t[1], t[3], 1, 0), M(6, t[2], t[3], 1, 0),
        ]
    return matches


class TemplateTests(unittest.TestCase):
    def test_r32_template_is_16_matches_73_to_88(self):
        self.assertEqual(set(bk.R32_TEMPLATE), set(range(73, 89)))

    def test_eight_third_hosting_matches(self):
        self.assertEqual(bk.THIRD_HOSTING, (74, 77, 79, 80, 81, 82, 85, 87))

    def test_third_pool_excludes_own_winner_group(self):
        for m in bk.THIRD_HOSTING:
            winner_group = bk.R32_TEMPLATE[m][0][1]
            self.assertNotIn(winner_group, bk.R32_TEMPLATE[m][1][1])   # no own-group rematch

    def test_bracket_tree_feeds_from_earlier_matches(self):
        for m, (x, y) in bk.BRACKET_TREE.items():
            self.assertLess(x, m)
            self.assertLess(y, m)
            for f in (x, y):
                self.assertTrue(f in bk.R32_TEMPLATE or f in bk.BRACKET_TREE)

    def test_two_halves_partition_the_r32(self):
        self.assertEqual(len(bk._TOP_HALF_R32), 8)
        self.assertEqual(len(set(bk.R32_TEMPLATE) - bk._TOP_HALF_R32), 8)


class AnnexCTests(unittest.TestCase):
    def test_loads_495_with_integrity(self):
        annex = bk.load_annex_c()
        self.assertEqual(len(annex), 495)
        # row 1 (combo EFGHIJKL): match 79 (winner 1A) hosts group E's third
        self.assertEqual(annex[tuple("EFGHIJKL")][79], "E")
        self.assertEqual(annex[tuple("EFGHIJKL")][74], "F")   # 1E column


class ProjectionTests(unittest.TestCase):
    def test_full_tournament_resolves_every_r32_match(self):
        annex = bk.load_annex_c()
        s = st.compute_standings(_full_groups())
        proj = bk.project(s, annex)
        self.assertTrue(proj["thirds_resolved"])
        self.assertTrue(proj["fully_projectable"])
        # best-8 thirds are groups A..H (by the engineered GD)
        self.assertEqual(tuple(sorted(r.group for r in s.third_place[:8])), tuple("ABCDEFGH"))
        for m, e in proj["r32"].items():
            self.assertIsNotNone(e["home"], m)
            self.assertIsNotNone(e["away"], m)
        # match 79 = Winner A vs the Annex-C-assigned third for combo ABCDEFGH
        third_group = annex[tuple("ABCDEFGH")][79]
        self.assertEqual(proj["r32"][79]["home"], "A1")            # group A winner
        self.assertEqual(proj["r32"][79]["away"], f"{third_group}3")

    def test_gating_omits_unstarted_groups(self):
        s = st.compute_standings(st.load_fixtures(SNAP))   # frozen: only group A has played
        proj = bk.project(s)
        self.assertFalse(proj["thirds_resolved"])          # not all 12 groups started
        self.assertTrue(any(e["home"] is None for e in proj["r32"].values()))  # abstract slots
        md = bk.render_markdown(proj)
        self.assertIn("as it stands", md.lower())
        self.assertIn("Best 3rd of", md)                   # third slots unresolved


def _alpha_resolver(a, b):
    """Deterministic stand-in for the model: the alphabetically-first team always
    advances, at a fixed probability. Lets feed() be tested without predict.py."""
    winner, loser = (a, b) if a <= b else (b, a)
    return {"winner": winner, "loser": loser, "p": 0.6}


class FeedTests(unittest.TestCase):
    def test_feed_propagates_to_a_champion(self):
        s = st.compute_standings(_full_groups())
        proj = bk.project(s, bk.load_annex_c())
        fed = bk.feed(proj, _alpha_resolver)
        # all 32 ties (R32..Final plus the third-place play-off) resolve on a
        # fully-played tournament — i.e. every match number 73..104
        self.assertEqual({int(k) for k in fed["winners"]}, set(range(73, 105)))
        # the champion is the alphabetically-first team to enter the bracket
        entrants = [e["home"] for e in proj["r32"].values()] + \
                   [e["away"] for e in proj["r32"].values()]
        self.assertEqual(fed["champion"], min(entrants))
        # every winner is one of that tie's two participants, p carried through
        for m, w in fed["winners"].items():
            self.assertIn(w["team"], fed["participants"][m])
            if w["source"] == "model":
                self.assertEqual(w["p"], 0.6)
        # third-place play-off is the two semi-final losers
        tp = proj["third_place_match"]
        self.assertEqual(set(fed["participants"][tp]),
                         {fed["winners"][101]["loser"], fed["winners"][102]["loser"]})

    def test_results_override_the_model(self):
        s = st.compute_standings(_full_groups())
        proj = bk.project(s, bk.load_annex_c())
        # force the underdog (alphabetically-last R32 entrant's match) via results
        any_r32 = next(iter(proj["r32"]))
        e = proj["r32"][any_r32]
        forced = max(e["home"], e["away"])             # the one _alpha_resolver would NOT pick
        fed = bk.feed(proj, _alpha_resolver, results={any_r32: forced})
        self.assertEqual(fed["winners"][any_r32]["team"], forced)
        self.assertEqual(fed["winners"][any_r32]["source"], "result")
        self.assertIsNone(fed["winners"][any_r32]["p"])

    def test_gating_halts_propagation(self):
        # only group A has played -> no full R32 tie has both sides known except via
        # started groups; nothing should propagate past the gating frontier.
        s = st.compute_standings(st.load_fixtures(SNAP))
        proj = bk.project(s)
        fed = bk.feed(proj, _alpha_resolver)
        self.assertIsNone(fed["champion"])             # cannot reach the Final
        self.assertEqual(fed["winners"], {})           # no R32 tie has both sides known
        # any resolved tie would have two concrete (non-None) participants
        for m, pr in fed["participants"].items():
            self.assertTrue(all(pr))


LIVE = REPO / "data" / "fixtures.csv"


class ProjectedFinishTests(unittest.TestCase):
    """The 'projected finish' view: project remaining games -> the full bracket resolves."""

    def test_project_final_standings_completes_every_group(self):
        s = bk.project_final_standings(st.load_fixtures(LIVE), lambda m: (1, 0))
        self.assertEqual(len(s.groups), 12)
        for gt in s.groups.values():                       # four teams, three games each
            self.assertEqual(len(gt.rows), 4)
            for r in gt.rows:
                self.assertEqual(r.played, 3)

    def test_projected_bracket_fully_resolves_to_a_champion(self):
        s = bk.project_final_standings(st.load_fixtures(LIVE), lambda m: (2, 1))
        proj = bk.feed(bk.project(s, resolve_provisional=True), _alpha_resolver)
        self.assertTrue(proj["fully_projectable"])
        self.assertTrue(proj["thirds_resolved"])
        self.assertIsNotNone(proj.get("champion"))

    def test_resolve_provisional_breaks_the_cutline(self):
        # all draws => every team level => the third-place cutline is provisional everywhere
        s = bk.project_final_standings(st.load_fixtures(LIVE), lambda m: (1, 1))
        self.assertFalse(bk.project(s)["thirds_resolved"])                 # gated by default
        self.assertTrue(bk.project(s, resolve_provisional=True)["thirds_resolved"])

    def test_per_side_provisional_flags(self):
        matches = st.load_fixtures(LIVE)
        now = bk.project(st.compute_standings(matches), resolve_provisional=True)
        for e in now["r32"].values():
            self.assertIn("home_provisional", e)
            self.assertIn("away_provisional", e)
        # mid-group-stage no position is sealed, so concrete slots are flagged provisional
        self.assertTrue(any(e["home_provisional"] or e["away_provisional"]
                            for e in now["r32"].values()))
        # projected-final standings (every team played 3) => nothing provisional
        full = bk.project(bk.project_final_standings(matches, lambda m: (2, 1)),
                          resolve_provisional=True)
        self.assertFalse(any(e["home_provisional"] or e["away_provisional"]
                             for e in full["r32"].values()))


if __name__ == "__main__":
    unittest.main()
