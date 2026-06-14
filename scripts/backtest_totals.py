#!/usr/bin/env python3
"""Backtest: is the model's TOTAL-goals miscalibrated for mismatches — and if so,
is it a MEAN error (favorite under-projected -> Maher-form fix) or a VARIANCE/tail
error (-> negative-binomial overdispersion)? Read-only; touches nothing in
predict_match. This is the evidence step MODEL_IMPROVEMENTS.md §3.1 asks for before
any total/texture change ("change nothing ... without backtest evidence").

Why not "replay history through the model": predict_match reads CURRENT (2026)
Elo/Futi ratings, so it can't price a 2014 match. Instead we put history and the
model on a common, scale-free axis — the FAVORITE'S PRE-MATCH EXPECTED POINTS — and
compare the goal totals each implies:

  * History: a forward Elo (built chronologically over the competitive corpus,
    home edge ~65 Elo per fit_hfa) gives each match a pre-match favorite and an
    expected-points value E_fav = max(E_home, 1-E_home), where Elo's expected
    score IS expected points. We bin matches by E_fav and read off the ACTUAL
    mean total, the variance/mean ratio (overdispersion index; Poisson => 1.0),
    the favorite's actual goals, and P(total >= 4).
  * Model: for the 72 WC2026 fixtures, E_fav = max(p_a,p_b) + 0.5*p_draw (also
    expected points) and the model's own total / P(total>3.5) / favorite lambda.

If actual totals RISE with dominance while the model's stay flat, the gap is a
MEAN error (the market's higher mismatch totals are right; canonical fix is the
Maher own-attack x opp-defense lambda). If actual MEAN tracks the model but the
variance/mean ratio climbs in mismatch bins, it is OVERDISPERSION (fix = NB goals,
not a higher mean). The table reports both so the decision is evidence-led.

stdlib only. Corpus: data/History/results.csv (see data/History/DATA_QUALITY.md).
CLI: python scripts/backtest_totals.py [--elo-start 1994-01-01] [--analyze-from 2010-01-01]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_rho as fr     # noqa: E402  (load_curated + CORPUS)
import predict as pr     # noqa: E402  (the model under test)
import build_edition as be  # noqa: E402  (fixtures reader)

REPO = Path(__file__).resolve().parents[1]
HFA_ELO = 65.0           # forward-Elo home edge, ~ the +0.455 goals fit_hfa measured
K_ELO = 40.0             # update step for competitive internationals
MIN_PRIOR = 10           # both teams need this many prior matches before a row is analysed
BINS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]


def _gd_multiplier(gd: int) -> float:
    """World Football Elo goal-difference weight: 1 for <=1, 1.5 at 2, then tapering."""
    gd = abs(gd)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return 1.75 + (gd - 3) / 8.0


def forward_elo(matches: list, analyze_from: str) -> list:
    """Walk matches in date order, maintaining a forward Elo. Return the analysable
    rows (date >= analyze_from, both teams warmed up) as dicts with the PRE-match
    favorite expected-points and the actual scoreline."""
    elo: dict = {}
    seen: dict = {}
    out = []
    for m in sorted(matches, key=lambda r: r["date"]):
        h, a = m["home"], m["away"]
        eh, ea = elo.get(h, 1500.0), elo.get(a, 1500.0)
        dr = (eh + (0.0 if m["neutral"] else HFA_ELO)) - ea
        e_home = 1.0 / (1.0 + 10.0 ** (-dr / 400.0))     # expected points for home
        if (m["date"] >= analyze_from and seen.get(h, 0) >= MIN_PRIOR
                and seen.get(a, 0) >= MIN_PRIOR):
            home_is_fav = e_home >= 0.5
            total = m["hs"] + m["as"]
            out.append({
                "e_fav": max(e_home, 1.0 - e_home),
                "total": total,
                "fav_goals": m["hs"] if home_is_fav else m["as"],
                "dog_goals": m["as"] if home_is_fav else m["hs"],
            })
        # update
        s_home = 1.0 if m["hs"] > m["as"] else 0.5 if m["hs"] == m["as"] else 0.0
        k = K_ELO * _gd_multiplier(m["hs"] - m["as"])
        delta = k * (s_home - e_home)
        elo[h], elo[a] = eh + delta, ea - delta
        seen[h] = seen.get(h, 0) + 1
        seen[a] = seen.get(a, 0) + 1
    return out


def _bin(e_fav: float) -> tuple | None:
    for lo, hi in BINS:
        if lo <= e_fav < hi:
            return (lo, hi)
    return None


def _stats(vals: list) -> tuple:
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n if n > 1 else 0.0
    return mean, var


def empirical_table(rows: list) -> dict:
    buckets: dict = {b: [] for b in BINS}
    for r in rows:
        b = _bin(r["e_fav"])
        if b:
            buckets[b].append(r)
    table = {}
    for b, rs in buckets.items():
        if not rs:
            continue
        totals = [r["total"] for r in rs]
        mean, var = _stats(totals)
        table[b] = {
            "n": len(rs),
            "total": mean,
            "vmr": (var / mean) if mean else 0.0,         # variance/mean (Poisson => 1)
            "fav_g": sum(r["fav_goals"] for r in rs) / len(rs),
            "dog_g": sum(r["dog_goals"] for r in rs) / len(rs),
            "p4": sum(1 for t in totals if t >= 4) / len(rs),
        }
    return table


def model_table(fixtures_path: Path) -> dict:
    model = pr.load_ratings(fixtures=fixtures_path)
    buckets: dict = {b: [] for b in BINS}
    for fr_row in be.read_rows(fixtures_path):
        try:
            p = pr.predict_match(model, fr_row["team_a"], fr_row["team_b"])
        except ValueError:
            continue
        e_fav = max(p.p_a, p.p_b) + 0.5 * p.p_draw         # favorite expected points
        b = _bin(e_fav)
        if b:
            buckets[b].append((p, max(p.lambda_a, p.lambda_b)))
    table = {}
    for b, ps in buckets.items():
        if not ps:
            continue
        table[b] = {
            "n": len(ps),
            "total": sum(p.total for p, _ in ps) / len(ps),
            "fav_g": sum(fg for _, fg in ps) / len(ps),
            "p4": sum(p.over[3.5] for p, _ in ps) / len(ps),   # P(total > 3.5) = P(>=4)
        }
    return table


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Totals calibration backtest (mean vs overdispersion).")
    ap.add_argument("--corpus", type=Path, default=fr.CORPUS)
    ap.add_argument("--elo-start", default="1994-01-01", help="build Elo from (warmup)")
    ap.add_argument("--analyze-from", default="2010-01-01", help="analyse matches on/after")
    ap.add_argument("--fixtures", type=Path, default=REPO / "data" / "fixtures.csv")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if not args.corpus.exists():
        print(f"error: corpus not found at {args.corpus} (see data/History/DATA_QUALITY.md)",
              file=sys.stderr)
        return 1

    corpus = fr.load_curated(args.corpus, args.elo_start)
    rows = forward_elo(corpus, args.analyze_from)
    emp = empirical_table(rows)
    mod = model_table(args.fixtures)

    print(f"Corpus: {len(corpus):,} competitive matches (Elo from {args.elo_start}); "
          f"{len(rows):,} analysed (>= {args.analyze_from}, "
          f"both teams >= {MIN_PRIOR} priors).\n")
    print("Axis = favourite's pre-match EXPECTED POINTS (Elo for history, "
          "p_win+0.5*p_draw for the model). Both measure the same thing.\n")
    hdr = (f"{'E_fav bin':>11} | {'n':>5} {'ACT tot':>7} {'var/mean':>8} {'favG':>5} "
           f"{'dogG':>5} {'P>=4':>5} | {'n':>3} {'MOD tot':>7} {'MOD favG':>8} {'P>=4':>5}  GAP")
    print(hdr)
    print("-" * len(hdr))
    for b in BINS:
        e, m = emp.get(b), mod.get(b)
        lbl = f"{b[0]:.2f}-{min(b[1],1.0):.2f}"
        if not e:
            continue
        gap = f"{(e['total'] - m['total']):+.2f}" if m else "   —"
        mblock = (f"{m['n']:>3} {m['total']:>7.2f} {m['fav_g']:>8.2f} {m['p4']:>5.0%}"
                  if m else f"{'—':>3} {'—':>7} {'—':>8} {'—':>5}")
        print(f"{lbl:>11} | {e['n']:>5} {e['total']:>7.2f} {e['vmr']:>8.2f} {e['fav_g']:>5.2f} "
              f"{e['dog_g']:>5.2f} {e['p4']:>5.0%} | {mblock}  {gap}")

    # headline read
    top = max((b for b in BINS if b in emp and b in mod), key=lambda b: b[0], default=None)
    lo = next((b for b in BINS if b in emp and b in mod), None)
    print()
    if top and lo and top != lo:
        de = emp[top]["total"] - emp[lo]["total"]
        dm = mod[top]["total"] - mod[lo]["total"]
        print(f"How much does the TOTAL rise from even ({lo[0]:.2f}) to most-lopsided "
              f"({top[0]:.2f}) bin:")
        print(f"  actual:  {emp[lo]['total']:.2f} -> {emp[top]['total']:.2f}  ({de:+.2f} goals)")
        print(f"  model:   {mod[lo]['total']:.2f} -> {mod[top]['total']:.2f}  ({dm:+.2f} goals)")
        print(f"  favourite's goals, top bin:  actual {emp[top]['fav_g']:.2f}  vs  "
              f"model {mod[top]['fav_g']:.2f}")
        print(f"  variance/mean ratio, top bin: {emp[top]['vmr']:.2f}  "
              f"(Poisson assumes 1.0; >1 => overdispersed tails)")
        print("\nRead: a big actual-vs-model rise with dominance => MEAN error (Maher-form "
              "lambda). A flat mean but var/mean >> 1 in mismatch bins => OVERDISPERSION (NB).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
