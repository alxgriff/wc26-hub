#!/usr/bin/env python3
"""Measure international home-field advantage and whether it SCALES with host strength.

Tier 2.4 of MODEL_IMPROVEMENTS.md, evidence-first (like fit_rho). The spec's
"stronger host -> larger edge" (Kalwij 2025) is a TOURNAMENT-VICTORY effect
(compounded over a run) and the per-MATCH relationship is contested (Allen & Jones
find weaker teams show a larger per-match home edge). So before scaling HFA by host
strength we test it on real per-match data:

  home_goal_diff  ~  a + b*(strength_gap)            [flat HFA]
  home_goal_diff  ~  a + b*(strength_gap) + c*s_home [strength-scaled HFA]

c>0 => stronger hosts get a bigger per-match home edge (the spec's direction);
c<0 => reversed; c~0 => flat is right. We OOS-validate (does the scaled model beat
flat on held-out home-GD RMSE) and translate the flat home edge into our model's
Elo-point HFA. Strength proxy = each team's mean goal difference per match over the
window (crude but the gap term controls the matchup; OOS validation is the real test).

NB the WC2026 hosts play at NFL venues with split crowds, so the corpus's full-home
edge should be DISCOUNTED for deployment (the current Config.hfa=60 already does this);
this tool sizes the full edge + the scaling sign, not the final deployed constant.

stdlib only. Corpus: data/History/results.csv (see data/History/DATA_QUALITY.md).
CLI: python scripts/fit_hfa.py [--start 2010-01-01] [--holdout 2023-01-01]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_rho as fr   # noqa: E402  (reuse load_curated)
import predict as pr   # noqa: E402  (Config.theta for the Elo translation)

REPO = Path(__file__).resolve().parents[1]
MIN_TEAM_MATCHES = 15
GD_CLIP = 5            # clip blowouts so OLS isn't dominated by 7-0 friend-of-format games


def strength_proxy(matches: list[dict]) -> dict:
    """team -> mean goal difference per match (centred so an average team ~ 0)."""
    gd, n = {}, {}
    for m in matches:
        for t, diff in ((m["home"], m["hs"] - m["as"]), (m["away"], m["as"] - m["hs"])):
            gd[t] = gd.get(t, 0) + diff
            n[t] = n.get(t, 0) + 1
    raw = {t: gd[t] / n[t] for t in n if n[t] >= MIN_TEAM_MATCHES}
    mean = sum(raw.values()) / len(raw)
    return {t: v - mean for t, v in raw.items()}


def _design(matches: list[dict], s: dict, scaled: bool) -> tuple:
    """Rows (x, y): y=clipped home GD; x=[1, gap] or [1, gap, s_home]."""
    X, Y = [], []
    for m in matches:
        if m["neutral"] or m["home"] not in s or m["away"] not in s:
            continue
        gap = s[m["home"]] - s[m["away"]]
        y = max(-GD_CLIP, min(GD_CLIP, m["hs"] - m["as"]))
        X.append([1.0, gap, s[m["home"]]] if scaled else [1.0, gap])
        Y.append(y)
    return X, Y


def _ols(X: list, Y: list) -> list:
    """Solve (X'X) b = X'Y by Gaussian elimination (stdlib)."""
    p = len(X[0])
    A = [[sum(X[r][i] * X[r][j] for r in range(len(X))) for j in range(p)] for i in range(p)]
    g = [sum(X[r][i] * Y[r] for r in range(len(X))) for i in range(p)]
    for c in range(p):
        piv = max(range(c, p), key=lambda r: abs(A[r][c]))
        A[c], A[piv] = A[piv], A[c]
        g[c], g[piv] = g[piv], g[c]
        d = A[c][c]
        A[c] = [v / d for v in A[c]]
        g[c] /= d
        for r in range(p):
            if r != c and A[r][c]:
                f = A[r][c]
                A[r] = [A[r][k] - f * A[c][k] for k in range(p)]
                g[r] -= f * g[c]
    return g


def _rmse(X: list, Y: list, b: list) -> float:
    return math.sqrt(sum((sum(b[i] * x[i] for i in range(len(b))) - y) ** 2
                         for x, y in zip(X, Y)) / len(Y))


def hfa_to_elo(home_gd: float, theta: float, mu0: float = 2.6) -> float:
    """Elo points h such that our model's home goal diff at even strength = home_gd:
    mu0*(2*logistic(h/theta) - 1) = home_gd."""
    share = (home_gd / mu0 + 1) / 2
    share = min(max(share, 1e-6), 1 - 1e-6)
    return theta * math.log(share / (1 - share))


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure home advantage + host-strength scaling.")
    ap.add_argument("--corpus", type=Path, default=fr.CORPUS)
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--holdout", default="2023-01-01")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if not args.corpus.exists():
        print(f"error: corpus not found at {args.corpus} (see data/History/DATA_QUALITY.md)",
              file=sys.stderr)
        return 1

    full = fr.load_curated(args.corpus, args.start)
    s_full = strength_proxy(full)
    train = [m for m in full if m["date"] < args.holdout]
    test = [m for m in full if m["date"] >= args.holdout]
    s_tr = strength_proxy(train)

    # Full-window estimates (report)
    Xf, Yf = _design(full, s_full, scaled=False)
    Xs, Ys = _design(full, s_full, scaled=True)
    bf, bs = _ols(Xf, Yf), _ols(Xs, Ys)
    home_gd = bf[0]                          # home edge in goals at even strength
    elo = hfa_to_elo(home_gd, pr.Config().theta)
    n_home = len(Yf)

    # OOS: fit on train, validate home-GD RMSE on holdout
    bf_tr = _ols(*_design(train, s_tr, scaled=False))
    bs_tr = _ols(*_design(train, s_tr, scaled=True))
    Xte_f, Yte = _design(test, s_tr, scaled=False)
    Xte_s, _ = _design(test, s_tr, scaled=True)
    rmse_flat = _rmse(Xte_f, Yte, bf_tr)
    rmse_scaled = _rmse(Xte_s, Yte, bs_tr)

    print(f"non-neutral home matches: {n_home} ({args.start}..{max(m['date'] for m in full)})")
    print(f"home edge at even strength: {home_gd:+.3f} goals  ->  ~{elo:.0f} Elo pts "
          f"(full crowd; current Config.hfa = {pr.Config().hfa:.0f}, already crowd-discounted)")
    print(f"strength-gap coef b:        {bf[1]:+.3f} goals per unit GD/match gap")
    print(f"host-strength scaling c:    {bs[2]:+.4f}  "
          f"({'stronger host -> LARGER edge' if bs[2] > 0 else 'stronger host -> SMALLER edge' if bs[2] < 0 else 'flat'})")
    print(f"out-of-sample home-GD RMSE: flat {rmse_flat:.4f}  vs  scaled {rmse_scaled:.4f}  "
          f"(delta {rmse_scaled - rmse_flat:+.4f})")
    # Adopt only on a MEANINGFUL OOS gain, not any improvement. The prior `- 1e-4` bar fired
    # on a ~0.055% RMSE artifact (2026-06-20 audit); require the scaled RMSE to beat flat by at
    # least MIN_REL_GAIN of the flat RMSE so a sub-noise wiggle can't trigger an "adopt".
    MIN_REL_GAIN = 0.005   # 0.5% of the flat home-GD RMSE
    gain = rmse_flat - rmse_scaled
    helps = gain > MIN_REL_GAIN * rmse_flat
    print(f"\nverdict: per-match host-strength scaling "
          f"{'IMPROVES' if helps else 'does NOT improve'} out-of-sample home-GD by a meaningful "
          f"margin (gain {gain:+.4f} = {gain / rmse_flat:+.2%}, bar {MIN_REL_GAIN:.1%}); "
          + ("adopt a strength-scaled HFA." if helps
             else "keep a FLAT host HFA (scaling unsupported / within noise per-match)."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
