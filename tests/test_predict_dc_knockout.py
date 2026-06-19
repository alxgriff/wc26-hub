"""Regression tests for the Dixon-Coles correction (Tier 1.1) and the knockout
resolution layer (Tier 1.2). Mirrors the style of the existing theta-calibration
test: lock the invariants that must not silently change.

Run:  python -m unittest discover -s tests
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import predict as P  # noqa: E402

MODEL = P.load_ratings()                       # default Config (rho=0.0)
N = MODEL.config.max_goals


# --------------------------------------------------------------- helpers

def _indep_wdl(lam_a: float, lam_b: float) -> tuple:
    """Plain independent-Poisson W/D/L, with NO Dixon-Coles term — the pre-change
    reference behaviour."""
    pa = [P._poisson(i, lam_a) for i in range(N + 1)]
    pb = [P._poisson(j, lam_b) for j in range(N + 1)]
    m = {(i, j): pa[i] * pb[j] for i in range(N + 1) for j in range(N + 1)}
    z = sum(m.values())
    a = sum(p for (i, j), p in m.items() if i > j) / z
    d = sum(p for (i, j), p in m.items() if i == j) / z
    b = sum(p for (i, j), p in m.items() if i < j) / z
    return a, d, b


def _toy_model(strength_a: float, strength_b: float, cfg: "P.Config | None" = None):
    """Two synthetic teams with controllable strengths and neutral att/def
    (z_att=z_def=0 => texture 0 => total = mu0). Data-free, for exact symmetry and
    monotonicity checks."""
    cfg = cfg or P.Config()

    def tr(name: str, s: float) -> P.TeamRating:
        return P.TeamRating(
            team=name, elo=s, futi=s, attack=0.0, defense=0.0, strength=s,
            z_att=0.0, z_def=0.0, elo_rank=1, futi_rank=1, consensus_rank=1,
            opta_advance=None, opta_wincup=None, opta_rank=None,
            market_odds=None, market_implied=None, market_rank=None)

    return P.RatingModel({"A": tr("A", strength_a), "B": tr("B", strength_b)},
                         cfg, "test")


class DixonColesTests(unittest.TestCase):
    # --------------------------------------------------------- 1.1 Dixon-Coles
    def test_rho_zero_is_inert(self):
        """rho=0 => predict_match equals plain independent Poisson, to machine precision."""
        m = P.load_ratings(config=P.Config(rho=0.0))
        pr = P.predict_match(m, "Brazil", "Morocco")
        a, d, b = _indep_wdl(pr.lambda_a, pr.lambda_b)
        self.assertLess(abs(pr.p_a - a), 1e-12)
        self.assertLess(abs(pr.p_draw - d), 1e-12)
        self.assertLess(abs(pr.p_b - b), 1e-12)

    def test_rho_zero_reproduces_known_baseline(self):
        """Locked documented output (C1 triangulation): Brazil 51 / draw 29 / Morocco 20.
        Integration lock against the live verified ratings (like RealDataTests). History:
        55/26/19 -> 53/28/19 when the Tier 3.1 Maher-form total params were activated
        (2026-06-14). Now 51/29/20 after the 2026-06-18 model update (Futi tilt w_futi
        1.0->1.5 AND the post-MD1 6/18 futi.live ratings): Futi rates Morocco far higher
        than Elo (rank 8 vs 22), so the heavier Futi weight lifts Morocco ~1pp and trims
        Brazil ~2pp. rho is still 0."""
        pr = P.predict_match(MODEL, "Brazil", "Morocco")
        got = (round(pr.p_a * 100), round(pr.p_draw * 100), round(pr.p_b * 100))
        self.assertEqual(got, (51, 29, 20), got)

    def test_dc_tau_per_cell_lambda_mapping(self):
        """Pin the EXACT per-cell Dixon-Coles form, incl. the deliberate cross-mapping
        ((0,1)->lam_a, (1,0)->lam_b). With asymmetric lambdas a swap would change the
        values, so this catches the common swapped-index bug."""
        la, lb, rho = 2.0, 0.5, -0.1
        self.assertAlmostEqual(P._dc_tau(0, 0, la, lb, rho), 1 - la * lb * rho, places=12)
        self.assertAlmostEqual(P._dc_tau(0, 1, la, lb, rho), 1 + la * rho, places=12)   # uses lam_a
        self.assertAlmostEqual(P._dc_tau(1, 0, la, lb, rho), 1 + lb * rho, places=12)   # uses lam_b
        self.assertAlmostEqual(P._dc_tau(1, 1, la, lb, rho), 1 - rho, places=12)
        self.assertEqual(P._dc_tau(2, 3, la, lb, rho), 1.0)                              # untouched cell
        self.assertNotEqual(P._dc_tau(0, 1, la, lb, rho), P._dc_tau(1, 0, la, lb, rho))  # not symmetric

    def test_dc_draw_monotonic_in_rho(self):
        """More negative rho => strictly more draw mass (low-total, near-even fixture)."""
        draws = []
        for rho in (0.0, -0.03, -0.06, -0.10, -0.13):
            m = P.load_ratings(config=P.Config(rho=rho))
            draws.append(P.predict_match(m, "Brazil", "Morocco").p_draw)
        self.assertTrue(all(d2 > d1 for d1, d2 in zip(draws, draws[1:])), draws)

    def test_dc_probabilities_still_sum_to_one(self):
        m = P.load_ratings(config=P.Config(rho=-0.06))
        pr = P.predict_match(m, "France", "Norway")
        self.assertLess(abs((pr.p_a + pr.p_draw + pr.p_b) - 1.0), 1e-12)

    def test_dc_magnitude_and_self_targeting(self):
        """At rho=-0.06 the draw bump is single-digit pp, and larger for a low-total
        even tie than for a lopsided one."""
        base = P.load_ratings(config=P.Config(rho=0.0))
        dc = P.load_ratings(config=P.Config(rho=-0.06))
        even = (P.predict_match(dc, "Brazil", "Morocco").p_draw
                - P.predict_match(base, "Brazil", "Morocco").p_draw)
        lopsided = (P.predict_match(dc, "Qatar", "Switzerland").p_draw
                    - P.predict_match(base, "Qatar", "Switzerland").p_draw)
        self.assertTrue(0.0 < even < 0.10, even)             # positive, single-digit pp
        self.assertGreater(even, lopsided)                   # self-targeting to close ties

    def test_dc_tau_stays_positive_over_range(self):
        """tau > 0 for every corrected cell across rho in [-0.15, 0] and a lambda range
        with headroom past the model's realistic max (~3.5) toward the binding
        -1/lambda constraint — guards the known DC validity limitation."""
        for rho in (0.0, -0.03, -0.06, -0.10, -0.15):
            for la in (0.3, 1.0, 2.0, 3.5, 5.0):
                for lb in (0.3, 1.0, 2.0, 3.5, 5.0):
                    for (i, j) in ((0, 0), (0, 1), (1, 0), (1, 1)):
                        self.assertGreater(P._dc_tau(i, j, la, lb, rho), 0.0)


class KnockoutTests(unittest.TestCase):
    # --------------------------------------------------------- 1.2 knockout
    def test_knockout_advance_normalised(self):
        ko = P.resolve_knockout(MODEL, "Brazil", "Morocco")
        self.assertLess(abs(ko.p_advance_a + ko.p_advance_b - 1.0), 1e-12)

    def test_knockout_equal_strength_is_coinflip(self):
        """Identical strengths => symmetric => exactly 0.5 advance (flat shootout)."""
        ko = P.resolve_knockout(_toy_model(1800.0, 1800.0), "A", "B")
        self.assertLess(abs(ko.p_advance_a - 0.5), 1e-9)

    def test_knockout_stronger_team_advances_more(self):
        a_big = P.resolve_knockout(_toy_model(2000.0, 1700.0), "A", "B").p_advance_a
        a_small = P.resolve_knockout(_toy_model(1800.0, 1700.0), "A", "B").p_advance_a
        self.assertTrue(a_big > a_small > 0.5)

    def test_knockout_advance_exceeds_90min_win_for_favourite(self):
        """A favourite's advance prob should exceed its bare 90-minute win prob, since
        part of the draw mass converts to advancement."""
        ko = P.resolve_knockout(_toy_model(1950.0, 1750.0), "A", "B")
        self.assertGreater(ko.p_advance_a, ko.reg.p_a)

    def test_knockout_reach_frequency_sanity(self):
        """Averaged over a slate of plausible knockout matchups, the implied reach-ET
        and reach-shootout rates should sit in a plausible neighbourhood of WC history
        (~32% reach ET; ~20-25% reach penalties per FiveThirtyEight). A sanity band,
        NOT a calibration target — tightening it is a Tier 2.5 outcome once c is fit.
        The band is wide because reach-shootout depends on the (still-unfit) c."""
        slate = [("Brazil", "Morocco"), ("France", "Norway"), ("Spain", "Uruguay"),
                 ("Argentina", "Croatia"), ("England", "Netherlands"),
                 ("Portugal", "Mexico"), ("Germany", "Japan"), ("Belgium", "Senegal")]
        ets = [P.resolve_knockout(MODEL, a, b).p_reach_et for a, b in slate]
        sos = [P.resolve_knockout(MODEL, a, b).p_reach_shootout for a, b in slate]
        mean_et = sum(ets) / len(ets)
        mean_so = sum(sos) / len(sos)
        self.assertTrue(0.15 < mean_et < 0.40, mean_et)     # historical ~0.32
        self.assertTrue(0.04 < mean_so < 0.28, mean_so)     # historical ~0.20-0.25


if __name__ == "__main__":
    unittest.main()
