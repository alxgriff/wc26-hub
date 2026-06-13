"""Tests for fit_rho.py (the Dixon-Coles rho fitter) and the calibration loader.

The fitter must DETECT low-score draw-excess (return rho<0) when it's there — so
that the real-data finding (rho ~= 0 on international football, because that corpus
does NOT show draw-excess) is a true signal, not a broken fitter. The loader must
activate a persisted rho and stay inert when none exists.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fit_rho as fr      # noqa: E402
import predict as pr      # noqa: E402


def _m(hs, as_):
    return {"home": "A", "away": "B", "hs": hs, "as": as_, "neutral": False,
            "date": "2020-01-01", "tournament": "X"}


def _corpus(n00, n11, n10, n01):
    ms = [_m(0, 0)] * n00 + [_m(1, 1)] * n11 + [_m(1, 0)] * n10 + [_m(0, 1)] * n01
    ms += [_m(2, 1) for _ in range(10)] + [_m(1, 2) for _ in range(10)]  # set non-degenerate lambdas
    return [dict(m) for m in ms]


class FitRhoTests(unittest.TestCase):
    def test_fitter_detects_low_score_draw_excess(self):
        heavy = _corpus(30, 30, 10, 10)   # excess 0-0 / 1-1
        light = _corpus(10, 10, 30, 30)   # fewer draws than independence
        r_heavy = fr.fit(heavy, fr.attack_defense(heavy))
        r_light = fr.fit(light, fr.attack_defense(light))
        self.assertLess(r_heavy, 0.0)        # draw-excess -> negative rho
        self.assertLess(r_heavy, r_light)    # more low-score draws -> more negative

    def test_partial_ll_is_finite_and_curated_drops_unplayed(self):
        ad = fr.attack_defense(_corpus(5, 5, 5, 5))
        self.assertTrue(fr.partial_ll(_corpus(5, 5, 5, 5), ad, -0.05) < 0.0)  # sum of log(<1)


class CalibrationLoaderTests(unittest.TestCase):
    def test_calibration_applied_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            cal = Path(d) / "calibration.json"
            cal.write_text(json.dumps({"rho": -0.06}), encoding="utf-8")
            orig = pr.CALIBRATION
            pr.CALIBRATION = cal
            try:
                self.assertAlmostEqual(pr.load_ratings().config.rho, -0.06)   # config=None reads it
            finally:
                pr.CALIBRATION = orig

    def test_explicit_config_is_not_overridden(self):
        # an explicit Config is respected verbatim — calibration only fills the default
        self.assertEqual(pr.load_ratings(config=pr.Config(rho=0.0)).config.rho, 0.0)

    def test_inert_when_no_calibration_file(self):
        with tempfile.TemporaryDirectory() as d:
            orig = pr.CALIBRATION
            pr.CALIBRATION = Path(d) / "nope.json"
            try:
                self.assertEqual(pr.load_ratings().config.rho, 0.0)
            finally:
                pr.CALIBRATION = orig


if __name__ == "__main__":
    unittest.main()
