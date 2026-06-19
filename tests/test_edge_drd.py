"""Tests for the Deserved-Result Divergence tracker (scripts/edge_drd.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import edge_drd as E


def _d(edge, lean, cap):
    return {"drd_edge": edge, "process_lean": lean, "market_capture": cap}


class QualifyTests(unittest.TestCase):
    def test_qualifies_process_driven_and_unpriced(self):
        # Morocco-like: real process lean, market sits below reputation
        self.assertTrue(E._qualifies(_d(0.096, 0.082, -0.014), 0.04, 0.03))

    def test_rejects_low_process_lean(self):
        # D3 Australia: big drd_edge but ~0 process lean => generic noise, not deserved-result
        self.assertFalse(E._qualifies(_d(0.171, 0.002, -0.169), 0.04, 0.03))

    def test_rejects_when_market_already_priced(self):
        # market_capture >= process_lean => the divergence is already in the line
        self.assertFalse(E._qualifies(_d(0.06, 0.08, 0.085), 0.04, 0.03))

    def test_rejects_small_edge(self):
        self.assertFalse(E._qualifies(_d(0.02, 0.08, -0.06), 0.04, 0.03))


class MarketWdlTests(unittest.TestCase):
    def _rows(self, h, d, a):
        return [{"match_id": "X1", "market": "h2h", "selection": s, "line": "",
                 "odds": str(o), "source": "median/9books", "phase": "snapshot",
                 "timestamp": "2026-06-18T10:00:00-04:00"}
                for s, o in (("home", h), ("draw", d), ("away", a))]

    def test_devig_sums_to_one_and_orders(self):
        wdl = E.market_wdl(self._rows(2.0, 3.4, 4.0), "X1")
        self.assertAlmostEqual(sum(wdl), 1.0, places=9)
        self.assertGreater(wdl[0], wdl[2])          # shorter home price => more likely

    def test_none_when_missing(self):
        self.assertIsNone(E.market_wdl([], "X1"))


class DecompositionTests(unittest.TestCase):
    """Integration against the live verified ratings (like predict RealDataTests)."""
    @classmethod
    def setUpClass(cls):
        cls.proc, cls.rep = E.build_models(E.FUTI_NOW)

    def test_drd_edge_is_lean_minus_capture(self):
        d = E.drd_for_match(self.proc, self.rep, "Brazil", "Haiti", None, (0.6, 0.25, 0.15))
        self.assertAlmostEqual(d["drd_edge"], d["process_lean"] - d["market_capture"], places=9)

    def test_process_gap_signs(self):
        gaps = E.team_gaps(self.proc)
        self.assertGreater(gaps["Morocco"], 0)     # Futi (process) rates Morocco above Elo
        self.assertLess(gaps["Türkiye"], 0)        # and Türkiye below Elo

    def test_build_models_restores_futi_file(self):
        before = E.P.FUTI_FILE
        E.build_models(E.FUTI_PRE)
        self.assertEqual(E.P.FUTI_FILE, before)     # vintage swap must not leak globally


if __name__ == "__main__":
    unittest.main()
