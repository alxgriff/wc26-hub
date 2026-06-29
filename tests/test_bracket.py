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
import scenarios as scen  # noqa: E402

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


def _all_draws_groups() -> list:
    """Every group game a 1-1 draw: all 48 teams finish level on points, GD and goals, so the
    third-place cutline is genuinely ambiguous everywhere (the 8th/9th boundary is a tie).
    Hermetic stand-in for 'a fully provisional cutline' — independent of the live results."""
    matches = []
    pairs = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
    for g in "ABCDEFGHIJKL":
        t = [f"{g}1", f"{g}2", f"{g}3", f"{g}4"]
        for no, (a, b) in enumerate(pairs, 1):
            matches.append(st.Match(f"{g}{no}", g, (no + 1) // 2, t[a], t[b], 1, 1, "played"))
    return matches


def _partial_groups() -> list:
    """All 12 groups MID-stage: matchdays 1-2 played, matchday 3 still scheduled — so every
    team has played 2 of 3 and NO group position is sealed. Hermetic stand-in for 'the live
    fixtures, mid-group-stage' (which stops being true once the real groups finish), so the
    provisional-flag assertion is date-independent."""
    out = []
    for m in _full_groups():
        if m.matchday == 3:
            out.append(st.Match(m.match_id, m.group, m.matchday, m.team_a, m.team_b,
                                None, None, "scheduled"))
        else:
            out.append(m)
    return out


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
        # started groups; no WINNER should be resolved past the gating frontier.
        s = st.compute_standings(st.load_fixtures(SNAP))
        proj = bk.project(s)
        fed = bk.feed(proj, _alpha_resolver)
        self.assertIsNone(fed["champion"])             # cannot reach the Final
        self.assertEqual(fed["winners"], {})           # no R32 tie has both sides known
        # a winner is only ever resolved when BOTH sides are known (partial slots may exist
        # for the live view, but they never carry a fabricated winner)
        for m in fed["winners"]:
            self.assertTrue(all(fed["participants"][m]))

    def test_winner_advances_onto_next_line_before_sibling_decided(self):
        # a team that has WON its tie shows on the next round's line immediately, with the
        # other slot still blank — like a real bracket (the M73 winner -> the M90 line).
        s = st.compute_standings(_full_groups())
        proj = bk.project(s, resolve_provisional=True)
        fed = bk.feed(proj, lambda a, b: None, results={73: proj["r32"][73]["home"]})
        self.assertEqual(fed["participants"][90], [proj["r32"][73]["home"], None])
        self.assertNotIn(90, fed["winners"])           # M90 itself isn't decided yet


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
        # a GENUINELY ambiguous cutline (all draws => every third level, the 8th/9th boundary
        # is a tie) is gated by default and broken deterministically by resolve_provisional
        s = st.compute_standings(_all_draws_groups())
        self.assertFalse(bk.project(s)["thirds_resolved"])                 # gated by default
        self.assertTrue(bk.project(s, resolve_provisional=True)["thirds_resolved"])

    def test_order_only_tie_inside_top8_does_not_gate(self):
        # the real-world case: a tie WITHIN the top 8 (both qualify) must NOT gate the bracket —
        # only a tie straddling the 8th/9th boundary changes the qualifying SET Annex C reads.
        def tr(g, gd, gf):                       # a third-place TeamRow with a given (gd, gf), 3 pts
            return st.TeamRow(f"{g}3", g, played=3, won=1, drawn=0, lost=2, gf=gf, ga=gf - gd)
        # ranks 1..7 distinct; 3rd & 4th tied (order-only, both in); 8th strictly ahead of 9th
        thirds = [tr("K", 4, 6), tr("F", 3, 6), tr("E", 2, 4), tr("L", 2, 4),  # E/L tie, both top-8
                  tr("B", 1, 5), tr("J", 0, 5), tr("D", 0, 4),                  # 7th
                  tr("I", 2, 8),                                                # 8th — clearly in
                  tr("G", 0, 3), tr("A", -1, 2)]                               # 9th — clearly out
        self.assertFalse(bk._cutline_ambiguous(thirds))                        # boundary determined
        # and a boundary straddle (8th & 9th level on pts/gd/gf) IS ambiguous
        straddle = thirds[:7] + [tr("I", 1, 4), tr("G", 1, 4), tr("A", -2, 1)]
        self.assertTrue(bk._cutline_ambiguous(straddle))

    def test_per_side_provisional_flags(self):
        # hermetic: a synthetic MID-group-stage set (MD1-2 played, MD3 open) — not the live
        # fixtures, which stop being mid-stage once the real groups finish.
        matches = _partial_groups()
        now = bk.project(st.compute_standings(matches), resolve_provisional=True)
        for e in now["r32"].values():
            self.assertIn("home_provisional", e)
            self.assertIn("away_provisional", e)
        # no position is sealed (each team has a game left), so concrete slots are provisional
        self.assertTrue(any(e["home_provisional"] or e["away_provisional"]
                            for e in now["r32"].values()))
        # projected-final standings (every team played 3) => nothing provisional
        full = bk.project(bk.project_final_standings(matches, lambda m: (2, 1)),
                          resolve_provisional=True)
        self.assertFalse(any(e["home_provisional"] or e["away_provisional"]
                             for e in full["r32"].values()))


class ConfirmedSlotTests(unittest.TestCase):
    """The per-side confirmed (✓) flag: a slot is confirmed only when its occupant has
    mathematically secured that exact seed, and only when a clinch map is supplied."""

    def test_no_clinch_map_means_nothing_confirmed(self):
        # even a fully-played tournament shows no ✓ unless the clinch map is passed —
        # the hypothetical 'projected finish' view relies on exactly this.
        proj = bk.project(st.compute_standings(_full_groups()), bk.load_annex_c())
        self.assertFalse(any(e["home_confirmed"] or e["away_confirmed"]
                             for e in proj["r32"].values()))

    def test_fully_played_with_clinch_map_confirms_every_seed(self):
        matches = _full_groups()
        s = st.compute_standings(matches)
        clinched = {g: scen.clinched_ranks(g, matches) for g in s.groups}
        proj = bk.project(s, bk.load_annex_c(), clinched=clinched)
        for m, e in proj["r32"].items():                    # all 72 games played -> every
            self.assertTrue(e["home_confirmed"], m)         # seed is mathematically secured
            self.assertTrue(e["away_confirmed"], m)
            self.assertFalse(e["home_provisional"])         # confirmed and provisional are
            self.assertFalse(e["away_provisional"])         # mutually exclusive at source

    def test_confirmed_winner_or_runnerup_holds_that_exact_rank(self):
        # live, mid-stage: any confirmed W/RU slot's occupant must hold that exact clinched
        # rank (1 or 2). Conditional, so it's robust to whatever the live table looks like.
        matches = st.load_fixtures(LIVE)
        s = st.compute_standings(matches, fair_play=st.load_discipline())
        clinched = {g: scen.clinched_ranks(g, matches) for g in s.groups}
        proj = bk.project(s, resolve_provisional=True, clinched=clinched)
        for m, e in proj["r32"].items():
            for side, slot in (("home", bk.R32_TEMPLATE[m][0]), ("away", bk.R32_TEMPLATE[m][1])):
                if e[f"{side}_confirmed"] and slot[0] in ("W", "RU"):
                    rank = 1 if slot[0] == "W" else 2
                    self.assertEqual(clinched[slot[1]].get(e[side]), rank, (m, side))


def _mk(mid, a, b, sa=None, sb=None):
    return st.Match(mid, mid[0], (int(mid[1]) + 1) // 2, a, b, sa, sb,
                    "played" if sa is not None else "scheduled")


# Mirrors the Group-G case: leader L is on 4 pts and only needs a draw in its MD3 game vs
# M; rival B is the strongest side but on 2 pts and plays the weak W. The decisive-chaining
# projector forces L's near-even game to a loss and crowns B/M; the modal projector keeps L
# (who controls its own fate) on top.
GROUP_LEADER = [
    _mk("A1", "L", "B", 1, 1), _mk("A2", "M", "W", 2, 2),
    _mk("A3", "L", "W", 2, 0), _mk("A4", "M", "B", 0, 0),
    _mk("A5", "L", "M"), _mk("A6", "B", "W"),
]


class ModalProjectionTests(unittest.TestCase):
    """project_modal_standings: simulate remaining games, take each group's modal winner."""

    def test_completes_every_group_and_is_deterministic(self):
        matches = st.load_fixtures(LIVE)
        a = bk.project_modal_standings(matches, lambda m: (1.5, 1.2), n_sims=500)
        b = bk.project_modal_standings(matches, lambda m: (1.5, 1.2), n_sims=500)
        self.assertEqual(len(a.groups), 12)
        for gt in a.groups.values():                       # every remaining game projected
            for r in gt.rows:
                self.assertEqual(r.played, 3)
        self.assertEqual([a.groups[g].rows[0].team for g in a.groups],
                         [b.groups[g].rows[0].team for g in b.groups])   # seeded -> identical

    def test_leader_who_only_needs_a_draw_keeps_top_spot(self):
        # M is slightly favoured in the L-M decider, B dominant over W. The modal method keeps
        # L on top (L only needs a point); the decisive-chaining method does NOT.
        rates = lambda m: {"A5": (1.0, 1.3), "A6": (2.6, 0.3)}.get(m.match_id, (1.2, 1.2))
        s = bk.project_modal_standings(GROUP_LEADER, rates, n_sims=3000)
        self.assertEqual(s.groups["A"].rows[0].team, "L")

        def decisive(m):
            la, lb = rates(m)
            return (1, 0) if la > lb else (0, 1) if lb > la else (1, 1)
        dec = bk.project_final_standings(GROUP_LEADER, decisive)
        self.assertNotEqual(dec.groups["A"].rows[0].team, "L")        # regression guard

    def test_modal_projection_drives_a_resolvable_bracket(self):
        s = bk.project_modal_standings(st.load_fixtures(LIVE), lambda m: (1.4, 1.1), n_sims=500)
        proj = bk.feed(bk.project(s, resolve_provisional=True), _alpha_resolver)
        self.assertTrue(proj["fully_projectable"])
        self.assertTrue(proj["thirds_resolved"])
        self.assertIsNotNone(proj.get("champion"))


if __name__ == "__main__":
    unittest.main()
