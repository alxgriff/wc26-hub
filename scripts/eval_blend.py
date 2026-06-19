"""eval_blend.py — independent backtest of the Elo/Futi supremacy blend.

Question (raised 2026-06-18): is a *Futi-only* supremacy model more predictive
than our equal-weight Elo+Futi hybrid? This sweeps the blend weight and scores
W/D/L out of sample, two ways:

  1. RECENT-HISTORY reverse-fit (data/History/results.csv): every match between
     two WC-2026 teams in a date window, scored with CURRENT rating snapshots.
     CAVEAT — we only hold current snapshots, so absolute Brier is look-ahead
     optimistic. But Elo and Futi are BOTH current snapshots, so the *relative*
     Futi-vs-Elo-vs-hybrid comparison is fair (the bias hits all configs alike).

  2. TOURNAMENT out-of-sample (data/fixtures.csv, status=played): the only games
     whose ratings genuinely predate kickoff. Small (MD1 so far) — reported with
     the honest caveat that ~16 games cannot separate models statistically.

Only the supremacy blend changes (Config.w_elo / w_futi); goals stay Futi-driven,
matching the production model exactly. Futi share f = w_futi/(w_elo+w_futi):
f=0.5 is the live model, f=1.0 "Futi-only", f=0.0 "Elo-only". Calibration.json
knobs (mu0/alpha/maher_w/rho/hfa) are loaded so every config mirrors production.
"""
from __future__ import annotations
import argparse
import csv
import math
import random
import statistics as stats
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as P

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "data" / "History" / "results.csv"
FIXTURES = REPO / "data" / "fixtures.csv"


def build_config(w_elo: float, w_futi: float) -> P.Config:
    """A Config that mirrors production (calibration.json knobs) but with an
    explicit supremacy blend. Replicates load_ratings' calibration branch since
    that branch only fires when config is None."""
    cfg = P.Config()
    cal = P._load_calibration() or {}
    if cal.get("rho") is not None:
        cfg.rho = float(cal["rho"])
    for k in ("maher_w", "alpha", "mu0"):
        if cal.get(k) is not None:
            setattr(cfg, k, float(cal[k]))
    if cal.get("hfa") is not None:
        cfg.hfa = float(cal["hfa"])
    if cal.get("hfa_by_host"):
        cfg.hfa_by_host = {k: float(v) for k, v in cal["hfa_by_host"].items()}
    cfg.w_elo, cfg.w_futi = w_elo, w_futi
    return cfg


def outcome_vec(hs: int, as_: int) -> tuple[int, int, int]:
    if hs > as_:
        return (1, 0, 0)
    if hs == as_:
        return (0, 1, 0)
    return (0, 0, 1)


def brier(p: tuple[float, float, float], y: tuple[int, int, int]) -> float:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y))


def logloss(p: tuple[float, float, float], y: tuple[int, int, int]) -> float:
    pi = max(1e-12, p[y.index(1)])
    return -math.log(pi)


def load_history_matches(canon: set[str], since: str, until: str) -> list[dict]:
    out = []
    with RESULTS.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            d = r["date"]
            if not (since <= d <= until):
                continue
            h, a = P._canon(r["home_team"]), P._canon(r["away_team"])
            if h not in canon or a not in canon:
                continue
            try:
                hs, as_ = int(r["home_score"]), int(r["away_score"])
            except (ValueError, TypeError):
                continue
            out.append({"date": d, "home": h, "away": a, "hs": hs, "as": as_,
                        "neutral": (r.get("neutral", "").strip().lower() == "true"),
                        "tournament": r.get("tournament", "")})
    return out


def load_tournament_matches(canon: set[str]) -> list[dict]:
    out = []
    with FIXTURES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("status") or "").strip().lower() != "played":
                continue
            h, a = P._canon(r["team_a"]), P._canon(r["team_b"])
            if h not in canon or a not in canon:
                continue
            try:
                hs, as_ = int(r["score_a"]), int(r["score_b"])
            except (ValueError, TypeError):
                continue
            host = P.HOST_BY_COUNTRY.get((r.get("country") or "").strip())
            hfa_team = host if host in (h, a) else None
            out.append({"id": r["match_id"], "home": h, "away": a, "hs": hs, "as": as_,
                        "hfa_team": hfa_team})
    return out


def score_config(matches: list[dict], w_elo: float, w_futi: float,
                 use_hfa: bool) -> list[tuple[float, float]]:
    """Return per-match (brier, logloss) for one blend config."""
    cfg = build_config(w_elo, w_futi)
    model = P.load_ratings(config=cfg)
    rows = []
    for m in matches:
        hfa_team = m.get("hfa_team") if use_hfa else None
        pred = P.predict_match(model, m["home"], m["away"], hfa_team=hfa_team)
        p = (pred.p_a, pred.p_draw, pred.p_b)
        y = outcome_vec(m["hs"], m["as"])
        rows.append((brier(p, y), logloss(p, y)))
    return rows


def paired_bootstrap(diff: list[float], B: int = 5000, seed: int = 12) -> tuple[float, float]:
    rnd = random.Random(seed)
    n = len(diff)
    means = []
    for _ in range(B):
        s = sum(diff[rnd.randrange(n)] for _ in range(n)) / n
        means.append(s)
    means.sort()
    return means[int(0.025 * B)], means[int(0.975 * B)]


def report(title: str, matches: list[dict], grid: list[float], use_hfa: bool) -> None:
    print(f"\n{'='*72}\n{title}  (n={len(matches)}, hfa={'on' if use_hfa else 'neutral'})\n{'='*72}")
    if not matches:
        print("  (no matches)")
        return
    per = {f: score_config(matches, 1 - f, f, use_hfa) for f in grid}
    base_f = 0.5  # live equal-weight hybrid
    if base_f not in per:
        per[base_f] = score_config(matches, 0.5, 0.5, use_hfa)
    base_brier = [b for b, _ in per[base_f]]
    print(f"\n  {'f=futi':>7} {'Brier':>8} {'LogLoss':>8}   {'ΔBrier vs hybrid':>18}  {'95% CI':>20}  win%")
    best_f, best_b = None, 9e9
    for f in grid:
        bl = [b for b, _ in per[f]]
        ll = [l for _, l in per[f]]
        mb, ml = stats.mean(bl), stats.mean(ll)
        if mb < best_b:
            best_b, best_f = mb, f
        diff = [bf - bb for bf, bb in zip(bl, base_brier)]  # config minus hybrid; <0 = better
        md = stats.mean(diff)
        lo, hi = paired_bootstrap(diff) if any(diff) else (0.0, 0.0)
        winpct = 100 * sum(1 for d in diff if d < 0) / len(diff)
        tag = "  <-- live" if abs(f - 0.5) < 1e-9 else ("  <-- Futi-only" if f >= 0.999 else
              ("  <-- Elo-only" if f <= 1e-9 else ""))
        print(f"  {f:>7.2f} {mb:>8.4f} {ml:>8.4f}   {md:>+18.4f}  [{lo:>+7.4f},{hi:>+7.4f}] {winpct:>4.0f}{tag}")
    print(f"\n  best Brier at f={best_f:.2f} (Brier {best_b:.4f}); "
          f"lower Brier = better. ΔBrier CI excluding 0 => statistically separable.")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2022-01-01")
    ap.add_argument("--until", default="2026-06-10", help="exclude WC26 itself from history")
    ap.add_argument("--grid", default="0,0.25,0.5,0.75,1.0",
                    help="Futi-share grid (f). f=0.5 is live, 1.0 Futi-only, 0.0 Elo-only")
    ap.add_argument("--home-edge", type=float, default=None,
                    help="if set, apply this Elo-pt generic home edge to non-neutral history (robustness)")
    args = ap.parse_args()

    model = P.load_ratings()
    canon = set(model.teams)
    grid = [float(x) for x in args.grid.split(",")]

    hist = load_history_matches(canon, args.since, args.until)
    report(f"RECENT HISTORY reverse-fit  {args.since}..{args.until}", hist, grid, use_hfa=False)

    tour = load_tournament_matches(canon)
    report("TOURNAMENT out-of-sample (fixtures.csv played)", tour, grid, use_hfa=True)

    # quick provenance
    print(f"\n[calibration] {P._load_calibration()}")
    print(f"[history] {len(hist)} WC-team-vs-WC-team matches in window; "
          f"corpus tail date {model.asof=}")


if __name__ == "__main__":
    main()
