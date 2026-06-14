#!/usr/bin/env python3
"""Fit the Tier 3.1 total-goals knobs (maher_w, alpha, mu0) so the model's
total-vs-dominance curve matches reality, GATED on W/D/L calibration not degrading.
Evidence-first, like fit_rho / fit_hfa; writes to data/calibration.json only with
--write AND only if both gates pass. Fit, don't hand-pick.

Method (transfer-safe — uses the model's OWN Futi ratings, so a fitted alpha is in
the production z-scale):
  * TARGET: the empirical total-vs-dominance curve. A forward Elo over the
    competitive corpus (backtest_totals.forward_elo) gives each historical match a
    favourite expected-points value E_fav; we bin by E_fav and read the actual mean
    total and actual W/D/L rates.
  * MODEL: all 1,128 pairwise matchups of the 48 real WC2026 teams, predicted with a
    candidate (mu0, alpha, maher_w). Binned by the model's own E_fav (= max(p_a,p_b)
    + 0.5*p_draw, the same expected-points quantity), we get the model's total curve.
  * TOTALS FIT: grid-search (mu0, alpha, maher_w) minimising the empirical-count-
    weighted squared error between the model's and the actual mean-total curves.
  * W/D/L GATE: at the fitted params, recompute the model's draw-rate and favourite-
    win-rate curves and compare to the empirical ones. The fit is ACCEPTED only if it
    does not worsen that W/D/L calibration error (the gate that rejected rho). Totals
    must improve AND W/D/L must not degrade.

The E_fav binning is done at each candidate's params for the model (E_fav shifts
slightly as totals move), so the comparison is honest. mu0 sets the even-game level,
alpha the att/def sensitivity, maher_w the convex mismatch lift.

stdlib only. Corpus: data/History/results.csv. CLI:
    python scripts/fit_maher.py [--write] [--elo-start ...] [--analyze-from ...]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_rho as fr          # noqa: E402  (load_curated, CORPUS, CALIBRATION)
import predict as pr          # noqa: E402  (model + Config)
import backtest_totals as bt  # noqa: E402  (forward_elo: dominance + totals + outcomes)

REPO = Path(__file__).resolve().parents[1]
BIN_W = 0.05                  # E_fav bin width for the curves
MIN_BIN = 40                  # ignore empirical bins thinner than this
MU0_GRID = [round(2.30 + 0.05 * i, 2) for i in range(9)]      # 2.30 .. 2.70
ALPHA_GRID = [round(0.15 + 0.025 * i, 4) for i in range(13)]  # 0.15 .. 0.45
W_GRID = [round(0.1 * i, 2) for i in range(16)]               # 0.0 .. 1.5
EPS = 1e-9                    # W/D/L gate tolerance (best must be <= current + EPS)


def _bin_lo(e_fav: float) -> float:
    e = min(max(e_fav, 0.5), 0.9999)
    return round(0.5 + math.floor((e - 0.5) / BIN_W) * BIN_W, 4)


def empirical_curve(rows: list) -> dict:
    """E_fav bin -> {n, total, fav_win, draw} from real matches (>= MIN_BIN games)."""
    agg: dict = {}
    for r in rows:
        d = agg.setdefault(_bin_lo(r["e_fav"]), {"n": 0, "tot": 0.0, "fw": 0, "dr": 0})
        d["n"] += 1
        d["tot"] += r["total"]
        if r["fav_goals"] > r["dog_goals"]:
            d["fw"] += 1
        elif r["fav_goals"] == r["dog_goals"]:
            d["dr"] += 1
    return {lo: {"n": d["n"], "total": d["tot"] / d["n"],
                 "fav_win": d["fw"] / d["n"], "draw": d["dr"] / d["n"]}
            for lo, d in agg.items() if d["n"] >= MIN_BIN}


def _total(mu0: float, alpha: float, w: float, h_a: float, h_b: float) -> float:
    """Model total at candidate params (mirrors predict_match's blend exactly)."""
    texture = (h_a + h_b) / 2
    t = mu0 * math.exp(alpha * texture)
    if w:
        t = (1 - w) * t + w * 0.5 * mu0 * (math.exp(alpha * h_a) + math.exp(alpha * h_b))
    return t


def matchup_features(model: "pr.RatingModel") -> list:
    """All 1,128 pairwise matchups: (E_fav at current params, h_a, h_b). E_fav is the
    dominance axis; total is refit from (h_a, h_b). E_fav is recomputed per-params in
    the W/D/L stage; for the totals grid the current-params E_fav is a fine bin proxy
    (dominance is strength-driven and near-invariant to the total knobs)."""
    out = []
    for a, b in combinations(model.teams.values(), 2):
        p = pr.predict_match(model, a.team, b.team)
        out.append({"e_fav": max(p.p_a, p.p_b) + 0.5 * p.p_draw,
                    "h_a": a.z_att - b.z_def, "h_b": b.z_att - a.z_def})
    return out


def model_total_curve(feats: list, mu0: float, alpha: float, w: float) -> dict:
    agg: dict = {}
    for f in feats:
        d = agg.setdefault(_bin_lo(f["e_fav"]), {"n": 0, "tot": 0.0})
        d["n"] += 1
        d["tot"] += _total(mu0, alpha, w, f["h_a"], f["h_b"])
    return {lo: d["tot"] / d["n"] for lo, d in agg.items()}


def totals_sse(feats: list, emp: dict, mu0: float, alpha: float, w: float) -> float:
    mc = model_total_curve(feats, mu0, alpha, w)
    return sum(e["n"] * (mc[lo] - e["total"]) ** 2 for lo, e in emp.items() if lo in mc)


def wdl_curve(teams: dict, asof: str, mu0: float, alpha: float, w: float) -> dict:
    """Model draw-rate / favourite-win-rate by E_fav bin at the given params (full
    matrix, E_fav recomputed at these params)."""
    cfg = pr.Config(mu0=mu0, alpha=alpha, maher_w=w)
    model = pr.RatingModel(teams, cfg, asof)
    agg: dict = {}
    for a, b in combinations(teams.values(), 2):
        p = pr.predict_match(model, a.team, b.team)
        lo = _bin_lo(max(p.p_a, p.p_b) + 0.5 * p.p_draw)
        d = agg.setdefault(lo, {"n": 0, "draw": 0.0, "fw": 0.0})
        d["n"] += 1
        d["draw"] += p.p_draw
        d["fw"] += max(p.p_a, p.p_b)
    return {lo: {"n": d["n"], "draw": d["draw"] / d["n"], "fav_win": d["fw"] / d["n"]}
            for lo, d in agg.items()}


def wdl_err(mc: dict, emp: dict) -> float:
    """Empirical-count-weighted mean |Δdraw| + |Δfav_win| over shared bins."""
    num = den = 0.0
    for lo, e in emp.items():
        if lo in mc:
            num += e["n"] * (abs(mc[lo]["draw"] - e["draw"]) + abs(mc[lo]["fav_win"] - e["fav_win"]))
            den += e["n"]
    return num / den if den else 0.0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit Maher-form total knobs, gated on W/D/L.")
    ap.add_argument("--corpus", type=Path, default=fr.CORPUS)
    ap.add_argument("--elo-start", default="1994-01-01")
    ap.add_argument("--analyze-from", default="2010-01-01")
    ap.add_argument("--fixtures", type=Path, default=REPO / "data" / "fixtures.csv")
    ap.add_argument("--write", action="store_true",
                    help="persist the fit to calibration.json (only if both gates pass)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if not args.corpus.exists():
        print(f"error: corpus not found at {args.corpus} (see data/History/DATA_QUALITY.md)",
              file=sys.stderr)
        return 1

    emp = empirical_curve(bt.forward_elo(fr.load_curated(args.corpus, args.elo_start),
                                         args.analyze_from))
    model = pr.load_ratings(fixtures=args.fixtures)
    feats = matchup_features(model)

    cur = pr.Config()                       # current production params
    base_sse = totals_sse(feats, emp, cur.mu0, cur.alpha, cur.maher_w)
    best = (base_sse, cur.mu0, cur.alpha, cur.maher_w)
    for mu0 in MU0_GRID:
        for alpha in ALPHA_GRID:
            for w in W_GRID:
                s = totals_sse(feats, emp, mu0, alpha, w)
                if s < best[0]:
                    best = (s, mu0, alpha, w)
    _, mu0, alpha, w = best

    # W/D/L gate
    wcur = wdl_err(wdl_curve(model.teams, model.asof, cur.mu0, cur.alpha, cur.maher_w), emp)
    wbest = wdl_err(wdl_curve(model.teams, model.asof, mu0, alpha, w), emp)
    totals_ok = best[0] < base_sse - EPS
    wdl_ok = wbest <= wcur + EPS
    accept = totals_ok and wdl_ok

    # report
    print(f"empirical curve: {sum(b['n'] for b in emp.values()):,} matches across "
          f"{len(emp)} bins (>= {MIN_BIN} each); model: {len(feats)} pairwise matchups.\n")
    cur_curve = model_total_curve(feats, cur.mu0, cur.alpha, cur.maher_w)
    new_curve = model_total_curve(feats, mu0, alpha, w)
    print(f"{'E_fav':>6} | {'n_emp':>6} {'ACT tot':>7} | {'cur MOD':>7} {'fit MOD':>7} "
          f"| {'cur gap':>7} {'fit gap':>7}")
    print("-" * 64)
    for lo in sorted(emp):
        e = emp[lo]
        cg = f"{cur_curve[lo]-e['total']:+.2f}" if lo in cur_curve else "   —"
        ng = f"{new_curve[lo]-e['total']:+.2f}" if lo in new_curve else "   —"
        cm = f"{cur_curve[lo]:.2f}" if lo in cur_curve else "  —"
        nm = f"{new_curve[lo]:.2f}" if lo in new_curve else "  —"
        print(f"{lo:>6.2f} | {e['n']:>6} {e['total']:>7.2f} | {cm:>7} {nm:>7} | {cg:>7} {ng:>7}")

    print(f"\nfitted:  mu0 {cur.mu0:.2f}->{mu0:.2f}   alpha {cur.alpha:.3f}->{alpha:.3f}   "
          f"maher_w {cur.maher_w:.2f}->{w:.2f}")
    print(f"totals SSE: {base_sse:.2f} -> {best[0]:.2f}  "
          f"({'IMPROVED' if totals_ok else 'no improvement'})")
    print(f"W/D/L calib err (weighted |Δdraw|+|Δfavwin|): {wcur:.4f} -> {wbest:.4f}  "
          f"({'OK — not degraded' if wdl_ok else 'DEGRADED — gate fails'})")
    print(f"\nVERDICT: {'ACCEPT' if accept else 'REJECT'} — "
          + ("totals improved and W/D/L held; "
             if accept else "gate failed; ")
          + ("writing calibration.json." if (accept and args.write)
             else "calibration.json NOT written." if accept
             else "calibration.json NOT written (model stays inert)."))

    if accept and args.write:
        cal = {}
        if fr.CALIBRATION.exists():
            try:
                cal = json.loads(fr.CALIBRATION.read_text(encoding="utf-8"))
            except ValueError:
                cal = {}
        cal.update({"mu0": mu0, "alpha": alpha, "maher_w": w,
                    "maher_fit": {"corpus_matches": sum(b["n"] for b in emp.values()),
                                  "totals_sse": [round(base_sse, 3), round(best[0], 3)],
                                  "wdl_err": [round(wcur, 5), round(wbest, 5)]}})
        fr.CALIBRATION.write_text(json.dumps(cal, indent=2), encoding="utf-8")
        print(f"wrote {fr.CALIBRATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
