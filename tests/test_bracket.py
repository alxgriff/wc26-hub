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


if __name__ == "__main__":
    unittest.main()
