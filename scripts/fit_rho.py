#!/usr/bin/env python3
"""Fit the Dixon-Coles low-score dependence parameter rho on historical results.

Tier 2 of MODEL_IMPROVEMENTS.md. rho only touches the (0,0)/(0,1)/(1,0)/(1,1)
score cells, so it is estimated by maximising the Dixon-Coles PARTIAL
log-likelihood (sum of log tau over low-score matches) — the Poisson marginal
terms don't depend on rho and drop out. We FIT broad (all competitive matches in
a recent window) and VALIDATE narrow (held-out out-of-sample W/D/L log-loss /
Brier) before writing rho to data/calibration.json, which predict.load_ratings()
reads into Config.rho. Fit, don't hand-pick; activate only on evidence.

Per-match expected goals use a light one-pass attack/defense ratio model fit on
the data (rho is robust to lambda misspecification — it only sees the low-score
corner — so this is sufficient; a full Maher MLE is overkill and needs scipy).

stdlib only. Reuses predict._dc_tau / predict._wdl so the DC math has one source.

CLI:
    python scripts/fit_rho.py [--start 2010-01-01] [--holdout 2023-01-01] [--write]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as pr  # noqa: E402  (reuse _dc_tau / _wdl / Config)

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "data" / "History" / "results.csv"
CALIBRATION = REPO / "data" / "calibration.json"
WINDOW_START = "2010-01-01"      # last ~4 cycles; recent enough for the modern game
HOLDOUT_FROM = "2023-01-01"      # out-of-sample validation slice
RHO_GRID = [round(-0.20 + 0.0025 * i, 4) for i in range(81)]   # -0.20 .. 0.0
MIN_TEAM_MATCHES = 10            # below this, a team gets league-average att/def


def load_curated(path: Path, start: str) -> list[dict]:
    """Played, competitive (non-friendly), integer-score matches on/after ``start``.
    Drops the unplayed NA rows (incl. future WC2026 fixtures) so there is no leakage.
    Folds played WC2026 results in from fixtures.csv (corpus_sync) so the snapshot's lag
    doesn't silently exclude the tournament — added after the 2026-06-20 audit."""
    import corpus_sync
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    rows, _n_wc = corpus_sync.merge_wc(rows)
    out = []
    for r in rows:
        if (r.get("tournament") or "") == "Friendly":
            continue
        hs, as_ = (r.get("home_score") or "").strip(), (r.get("away_score") or "").strip()
        if hs in ("", "NA") or as_ in ("", "NA"):
            continue
        if (r.get("date") or "") < start:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except ValueError:
            continue
        out.append({"home": r["home_team"], "away": r["away_team"], "hs": hs, "as": as_,
                    "neutral": (r.get("neutral") or "").strip().upper() == "TRUE",
                    "date": r["date"], "tournament": r["tournament"]})
    return out


def attack_defense(matches: list[dict]) -> dict:
    """One-pass ratio attack/defense per team + home/away/neutral goal baselines."""
    nonneu = [m for m in matches if not m["neutral"]]
    mu_home = sum(m["hs"] for m in nonneu) / len(nonneu)
    mu_away = sum(m["as"] for m in nonneu) / len(nonneu)
    neu = [m for m in matches if m["neutral"]]
    mu_neu = (sum(m["hs"] + m["as"] for m in neu) / (2 * len(neu))) if neu else (mu_home + mu_away) / 2
    overall = sum(m["hs"] + m["as"] for m in matches) / (2 * len(matches))
    gf, ga, n = {}, {}, {}
    for m in matches:
        for t, scored, conceded in ((m["home"], m["hs"], m["as"]), (m["away"], m["as"], m["hs"])):
            gf[t] = gf.get(t, 0) + scored
            ga[t] = ga.get(t, 0) + conceded
            n[t] = n.get(t, 0) + 1
    att, dfn = {}, {}
    for t in n:
        if n[t] >= MIN_TEAM_MATCHES:
            att[t] = (gf[t] / n[t]) / overall
            dfn[t] = (ga[t] / n[t]) / overall
        else:
            att[t] = dfn[t] = 1.0
    return {"att": att, "dfn": dfn, "mu_home": mu_home, "mu_away": mu_away, "mu_neu": mu_neu}


def match_lambdas(m: dict, ad: dict) -> tuple:
    a_h, d_h = ad["att"].get(m["home"], 1.0), ad["dfn"].get(m["home"], 1.0)
    a_a, d_a = ad["att"].get(m["away"], 1.0), ad["dfn"].get(m["away"], 1.0)
    if m["neutral"]:
        return ad["mu_neu"] * a_h * d_a, ad["mu_neu"] * a_a * d_h
    return ad["mu_home"] * a_h * d_a, ad["mu_away"] * a_a * d_h


def partial_ll(matches: list[dict], ad: dict, rho: float) -> float:
    """Dixon-Coles partial log-likelihood: sum of log tau over the low-score cells
    (tau == 1 elsewhere => contributes 0). A rho that drives any tau <= 0 is invalid."""
    s = 0.0
    for m in matches:
        if m["hs"] > 1 or m["as"] > 1:
            continue
        lam_h, lam_a = match_lambdas(m, ad)
        tau = pr._dc_tau(m["hs"], m["as"], lam_h, lam_a, rho)
        if tau <= 1e-12:
            return float("-inf")
        s += math.log(tau)
    return s


def fit(matches: list[dict], ad: dict) -> float:
    return max(RHO_GRID, key=lambda r: partial_ll(matches, ad, r))


def wdl_scores(matches: list[dict], ad: dict, rho: float) -> tuple:
    """(mean log-loss, mean multiclass Brier) of W/D/L predictions under the given
    rho, using the DC-corrected score matrix (predict._wdl)."""
    cfg = pr.Config(rho=rho)
    ll = brier = 0.0
    for m in matches:
        lam_h, lam_a = match_lambdas(m, ad)
        p = pr._wdl(lam_h, lam_a, cfg)
        oc = 0 if m["hs"] > m["as"] else (1 if m["hs"] == m["as"] else 2)
        ll += -math.log(max(p[oc], 1e-15))
        brier += sum((p[k] - (1.0 if k == oc else 0.0)) ** 2 for k in range(3))
    n = len(matches)
    return ll / n, brier / n


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit Dixon-Coles rho on historical results.")
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--start", default=WINDOW_START)
    ap.add_argument("--holdout", default=HOLDOUT_FROM)
    ap.add_argument("--write", action="store_true", help="write data/calibration.json")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.corpus.exists():
        print(f"error: corpus not found at {args.corpus}\n"
              "  Get it (public CC0): curl -sSL -o data/History/results.csv \\\n"
              "    https://raw.githubusercontent.com/martj42/international_results/master/results.csv",
              file=sys.stderr)
        return 1

    full = load_curated(args.corpus, args.start)
    if len(full) < 1000:
        print(f"error: only {len(full)} curated matches since {args.start} — too few to fit",
              file=sys.stderr)
        return 1
    train = [m for m in full if m["date"] < args.holdout]
    test = [m for m in full if m["date"] >= args.holdout]

    # FIT broad (train), VALIDATE narrow (held-out out-of-sample)
    ad_train = attack_defense(train)
    rho_train = fit(train, ad_train)
    ll0, br0 = wdl_scores(test, ad_train, 0.0)
    llr, brr = wdl_scores(test, ad_train, rho_train)

    # production value: refit on the FULL window (more data) once direction is confirmed
    ad_full = attack_defense(full)
    rho_full = fit(full, ad_full)

    print(f"corpus: {len(full)} competitive matches {args.start}..{max(m['date'] for m in full)} "
          f"(train {len(train)} / holdout {len(test)} from {args.holdout})")
    print(f"rho (train fit):     {rho_train:+.4f}")
    print(f"rho (full-window):   {rho_full:+.4f}   <- production value")
    print(f"out-of-sample W/D/L  log-loss: {ll0:.5f} (rho=0) -> {llr:.5f} (rho={rho_train:+.3f})  "
          f"delta {llr - ll0:+.5f}")
    print(f"out-of-sample W/D/L  Brier:    {br0:.5f} (rho=0) -> {brr:.5f} (rho={rho_train:+.3f})  "
          f"delta {brr - br0:+.5f}")

    # stability across tournament regimes (diagnostic; expect fairly stable)
    print("rho by tournament slice (full window, n>=300):")
    from collections import Counter
    counts = Counter(m["tournament"] for m in full)
    for tname, c in counts.most_common():
        if c < 300:
            continue
        sub = [m for m in full if m["tournament"] == tname]
        print(f"   {fit(sub, attack_defense(sub)):+.4f}  {tname} (n={c})")

    improves = (llr <= ll0) and (brr <= br0)
    print(f"\nverdict: rho is {'NEGATIVE and ' if rho_full < 0 else ''}"
          f"{'IMPROVES' if improves else 'does NOT improve'} out-of-sample log-loss & Brier.")

    if args.write:
        payload = {"rho": rho_full, "fit_date": datetime.now(timezone.utc).date().isoformat(),
                   "window_start": args.start, "n_matches": len(full),
                   "method": "Dixon-Coles partial-LL MLE (grid), light one-pass att/def lambdas",
                   "oos_logloss_delta": round(llr - ll0, 6),
                   "oos_brier_delta": round(brr - br0, 6),
                   "corpus": "martj42/international_results (CC0)"}
        CALIBRATION.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {CALIBRATION.relative_to(REPO)} (rho={rho_full:+.4f})")
    else:
        print("\n(dry run — pass --write to persist data/calibration.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
