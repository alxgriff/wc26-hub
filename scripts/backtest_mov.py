"""backtest_mov.py — does a 538-style margin-of-victory multiplier predict better than our
current eloratings step function?

Our roller (update_elo.py) scales each result by goal difference ALONE (step: 1 / 1.5 / 1.75
/ +1/8), winner-agnostic. FiveThirtyEight's MoV multiplier adds an autocorrelation correction:

    mult_538 = ln(GD+1) * 2.2 / ((winner_elo - loser_elo)*0.001 + 2.2)

The second factor DAMPENS a favourite winning big (expected, low information) and AMPLIFIES an
underdog winning big (a surprise — strong evidence). So a minnow's 3-0 over a giant moves Elo
MORE than a giant's 3-0 over a minnow. (NB the (actual-expected) term already rewards upsets in
both schemes; this is an extra adjustment to the MARGIN weight on top of that.)

We test which RATING SYSTEM forecasts better, online & leak-free, on the international corpus
(data/History/results.csv): roll each scheme chronologically, predict each match from its
PRE-match Elo (Elo gap -> Poisson W/D/L, params fit per scheme on a train split so each is
optimally calibrated and the K/scale difference is absorbed), then update. Score the held-out
tail by Brier, RPS, log-loss with a paired bootstrap CI on the difference.

Schemes:
  step       — our current eloratings goal-difference step (winner-agnostic)
  damp       — step x the 538 winner-correction (isolates the favourite/underdog asymmetry)
  ln538      — the full FiveThirtyEight form ln(GD+1) x correction (draws -> base 1.0)

Usage:  python scripts/backtest_mov.py [--since 2010-01-01] [--split 0.6]
"""
from __future__ import annotations
import argparse
import csv
import math
import statistics as stats
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_market as BM   # reuse model_wdl / brier / rps / logloss / fit_model_params / paired_bootstrap / outcome_idx

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "data" / "History" / "results.csv"

K = 40.0          # fixed roll K for ALL schemes (held constant; theta-per-scheme absorbs scale)
ROLL_HFA = 65.0   # home edge baked into the roll's expectation (0 when neutral)


def step_mult(gd, dw):
    g = abs(gd)
    if g <= 1:
        return 1.0
    if g == 2:
        return 1.5
    if g == 3:
        return 1.75
    return 1.75 + (g - 3) / 8.0


def damp_factor(dw):
    """538 winner-correction: dw = winner_elo - loser_elo (pre-match). <1 favourite, >1 underdog."""
    return 2.2 / (dw * 0.001 + 2.2)


def damp_mult(gd, dw):
    if abs(gd) == 0:
        return 1.0                       # draw: no margin to dampen
    return step_mult(gd, dw) * damp_factor(dw)


def ln538_mult(gd, dw):
    if abs(gd) == 0:
        return 1.0                       # draw: standard update (ln(1)=0 would zero it out)
    return math.log(abs(gd) + 1) * damp_factor(dw)


SCHEMES = {"step": step_mult, "damp": damp_mult, "ln538": ln538_mult}


def load_corpus(since: str):
    rows = []
    with RESULTS.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = r["date"]
            if d < since:
                continue
            try:
                hs, as_ = int(r["home_score"]), int(r["away_score"])
            except (ValueError, TypeError):
                continue
            rows.append({"date": d, "home": r["home_team"].strip(), "away": r["away_team"].strip(),
                         "hs": hs, "as": as_, "ftr": "H" if hs > as_ else ("D" if hs == as_ else "A"),
                         "neutral": (r.get("neutral", "").strip().lower() == "true")})
    rows.sort(key=lambda x: x["date"])
    return rows


def load_corpus_club(since: str):
    """Independent replication set: football-data.co.uk club leagues (data/MarketHistory)."""
    rows = []
    for f in sorted(BM.CACHE.glob("*.csv")):
        with f.open(encoding="latin-1", newline="") as fh:
            for r in csv.DictReader(fh):
                d = BM._parse_date(r.get("Date", ""))
                if not d:
                    continue
                iso = d.isoformat()
                if iso < since:
                    continue
                try:
                    hs, as_ = int(r["FTHG"]), int(r["FTAG"])
                except (KeyError, ValueError):
                    continue
                ft = (r.get("FTR") or "").strip()
                if ft not in ("H", "D", "A"):
                    continue
                rows.append({"date": iso, "home": r["HomeTeam"].strip(), "away": r["AwayTeam"].strip(),
                             "hs": hs, "as": as_, "ftr": ft, "neutral": False})   # club games are home/away
    rows.sort(key=lambda x: x["date"])
    return rows


def roll_scheme(rows, mult_fn):
    """Roll Elo with mult_fn; stamp each row with PRE-match elo_diff (home-away). Leak-free
    (snapshot before update). Returns rows with 'elo_diff' added (a fresh per-scheme copy)."""
    elo = {}
    out = []
    for m in rows:
        ra = elo.get(m["home"], 1500.0)
        rb = elo.get(m["away"], 1500.0)
        warm = (m["home"] in elo and m["away"] in elo)
        rec = dict(m, elo_diff=ra - rb, warm=warm)
        out.append(rec)
        h = 0.0 if m["neutral"] else ROLL_HFA
        wa = 1.0 if m["ftr"] == "H" else (0.5 if m["ftr"] == "D" else 0.0)
        ea = 1.0 / (1.0 + 10 ** (-((ra + h) - rb) / 400.0))
        gd = m["hs"] - m["as"]
        winner_minus_loser = (ra - rb) if gd > 0 else ((rb - ra) if gd < 0 else 0.0)
        delta = K * mult_fn(gd, winner_minus_loser) * (wa - ea)
        elo[m["home"]] = ra + delta
        elo[m["away"]] = rb - delta
    return out


def evaluate(rows, split):
    cut = int(len(rows) * split)
    # demo of the asymmetry the user asked about
    print("\nMoV multiplier — a 3-0 win, giant (winner +300 Elo) vs minnow (winner -300 Elo):")
    for name, fn in SCHEMES.items():
        print(f"  {name:>6}: favourite 3-0  x{fn(3, 300):.3f}   |   underdog 3-0  x{fn(3, -300):.3f}"
              + ("   <- symmetric (winner-agnostic)" if name == "step" else "   <- underdog rewarded more"))

    base_metrics = {}
    print(f"\nOnline OOS forecast accuracy (corpus {rows[0]['date']}..{rows[-1]['date']}, "
          f"{len(rows)} matches, holdout tail {len(rows)-cut}):")
    print(f"  {'scheme':>6} {'Brier':>8} {'RPS':>8} {'LogLoss':>8}")
    per, per_gap = {}, {}
    for name, fn in SCHEMES.items():
        rolled = roll_scheme(rows, fn)
        train = [r for r in rolled[:cut] if r["warm"]]
        test = [r for r in rolled[cut:] if r["warm"]]   # warm flag depends only on match order => aligned across schemes
        theta, mu0, hfa = BM.fit_model_params(train)
        bs, rp, ll = [], [], []
        for m in test:
            p = BM.model_wdl(m["elo_diff"], theta, mu0, hfa)
            y = BM.outcome_idx(m["ftr"])
            bs.append(BM.brier(p, y)); rp.append(BM.rps(p, y)); ll.append(BM.logloss(p, y))
        per[name] = bs
        per_gap[name] = [abs(m["elo_diff"]) for m in test]
        print(f"  {name:>6} {stats.mean(bs):>8.4f} {stats.mean(rp):>8.4f} {stats.mean(ll):>8.4f}"
              f"   (theta={theta} mu0={mu0} hfa={hfa}, n_test={len(test)})")
    # paired diffs vs the current step scheme (full holdout)
    base = per["step"]
    print("\nPaired vs current 'step' (negative = the variant predicts BETTER):")
    for name in ("damp", "ln538"):
        n = min(len(base), len(per[name]))
        diff = [per[name][i] - base[i] for i in range(n)]
        lo, hi = BM.paired_bootstrap(diff)
        print(f"  {name:>6}: dBrier {stats.mean(diff):+.4f}  95%CI [{lo:+.4f},{hi:+.4f}]"
              f"   {'separable' if (lo>0 or hi<0) else 'within noise'}")

    # LOPSIDED subset — where MoV-dampening should bite most (the WC giant-vs-minnow regime)
    gaps = per_gap["step"]   # |elo_diff| per test match, aligned to the metric lists
    for thr in (150, 250):
        idx = [i for i, g in enumerate(gaps) if g > thr]
        if len(idx) < 50:
            continue
        print(f"\nLopsided subset |elo gap| > {thr}  (n={len(idx)} of {len(gaps)} test games):")
        sb = stats.mean([base[i] for i in idx])
        print(f"  {'step':>6} Brier {sb:.4f}")
        for name in ("damp", "ln538"):
            diff = [per[name][i] - base[i] for i in idx]
            lo, hi = BM.paired_bootstrap(diff)
            print(f"  {name:>6} Brier {stats.mean([per[name][i] for i in idx]):.4f}  "
                  f"dBrier {stats.mean(diff):+.4f}  95%CI [{lo:+.4f},{hi:+.4f}]"
                  f"   {'separable' if (lo>0 or hi<0) else 'within noise'}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2010-01-01")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--source", choices=("intl", "club"), default="intl")
    args = ap.parse_args()
    rows = load_corpus(args.since) if args.source == "intl" else load_corpus_club(args.since)
    if len(rows) < 500:
        print(f"only {len(rows)} matches — need the corpus (intl: data/History/results.csv; "
              f"club: backtest_market.py --download first)")
        return
    print(f"loaded {len(rows)} {args.source} matches since {args.since}")
    evaluate(rows, args.split)


if __name__ == "__main__":
    main()
