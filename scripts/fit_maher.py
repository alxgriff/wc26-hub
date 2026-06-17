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


# ---------------------------------------------------------------- OOS W/D/L gate
# fit_rho-style out-of-sample validation: split the corpus train (2010..holdout) /
# holdout (>=holdout), fit each form's total params on TRAIN, then compare per-match
# multiclass W/D/L Brier + log-loss on the HOLDOUT. Leak-free: Elo is pre-match by
# construction; att/def ratings are TRAIN-only. The total comes from att/def (the
# Maher terms), the SPLIT from a single Elo-based share scale fit once and SHARED by
# both forms, so the Brier difference isolates the totals' effect on W/D/L — exactly
# what the gate must measure. Scale note: corpus att/def z-scores differ from the
# production Futi scale, so each form gets its own corpus-fit alpha; the OOS conclusion
# (does the Maher MECHANISM degrade W/D/L / improve totals OOS) is what transfers, not
# the specific constants (the production curve fit sets those).
SHARE_GRID = [200, 250, 300, 350, 400, 450, 500, 600, 800]
# OOS total-form fit caps w at full-Maher (1.0). Per-match SSE descends monotonically
# in w past 1.0 (it chases the 7-9 goal blowout tail), so an uncapped grid just pegs
# at its edge; the exact w is a corpus STAND-IN anyway (different z-scale from Futi)
# and immaterial to the only OOS claim we make — W/D/L non-degradation.
OOS_W_GRID = [round(0.1 * i, 2) for i in range(11)]          # 0.0 .. 1.0
DR_BINS = [(0, 60), (60, 140), (140, 250), (250, 9999)]      # |Elo gap| reliability bins


def _elo_pass(matches: list) -> list:
    """Forward Elo (reuses backtest_totals' params); annotate each match with the
    PRE-match Elo of both sides (reflects only prior games -> no leakage)."""
    elo: dict = {}
    out = []
    for m in sorted(matches, key=lambda r: r["date"]):
        eh, ea = elo.get(m["home"], 1500.0), elo.get(m["away"], 1500.0)
        out.append({**m, "elo_h": eh, "elo_a": ea})
        dr = (eh + (0.0 if m["neutral"] else bt.HFA_ELO)) - ea
        e_home = 1.0 / (1.0 + 10.0 ** (-dr / 400.0))
        s = 1.0 if m["hs"] > m["as"] else 0.5 if m["hs"] == m["as"] else 0.0
        d = bt.K_ELO * bt._gd_multiplier(m["hs"] - m["as"]) * (s - e_home)
        elo[m["home"]], elo[m["away"]] = eh + d, ea - d
    return out


def _attdef_z(matches: list) -> tuple:
    """Per-team z_att (std goals-for) and z_def (std SUPPRESSION = -std goals-against,
    so a stingy team scores HIGH) from the given (train) matches. predict_match's
    convention: h = z_att(attacker) - z_def(defender)."""
    import statistics
    gf, ga, n = {}, {}, {}
    for m in matches:
        for t, sc, co in ((m["home"], m["hs"], m["as"]), (m["away"], m["as"], m["hs"])):
            gf[t] = gf.get(t, 0) + sc
            ga[t] = ga.get(t, 0) + co
            n[t] = n.get(t, 0) + 1
    teams = [t for t in n if n[t] >= 10]
    af = {t: gf[t] / n[t] for t in teams}
    df = {t: ga[t] / n[t] for t in teams}
    ma, sa = statistics.mean(af.values()), statistics.pstdev(af.values()) or 1.0
    md, sd = statistics.mean(df.values()), statistics.pstdev(df.values()) or 1.0
    return ({t: (af[t] - ma) / sa for t in teams},
            {t: -(df[t] - md) / sd for t in teams})


def _features(rows: list, z_att: dict, z_def: dict) -> list:
    out = []
    for m in rows:
        out.append({
            "ha": z_att.get(m["home"], 0.0) - z_def.get(m["away"], 0.0),
            "hb": z_att.get(m["away"], 0.0) - z_def.get(m["home"], 0.0),
            "tot": m["hs"] + m["as"],
            "dr": (m["elo_h"] + (0.0 if m["neutral"] else bt.HFA_ELO)) - m["elo_a"],
            "oc": 0 if m["hs"] > m["as"] else 1 if m["hs"] == m["as"] else 2,
        })
    return out


def _fit_total_params(feats: list, with_maher: bool) -> tuple:
    ws = OOS_W_GRID if with_maher else [0.0]
    best = None
    for mu0 in MU0_GRID:
        for al in ALPHA_GRID:
            for w in ws:
                sse = sum((_total(mu0, al, w, f["ha"], f["hb"]) - f["tot"]) ** 2 for f in feats)
                # tie-break on the flat ridge: prefer less change (smaller w, then alpha)
                key = (round(sse, 4), w, al, mu0)
                if best is None or key < best:
                    best = key
    return best[3], best[2], best[1]            # (mu0, alpha, w)


def _wdl_at(f: dict, mu0: float, al: float, w: float, S: float, cfg) -> tuple:
    tot = _total(mu0, al, w, f["ha"], f["hb"])
    share = 1.0 / (1.0 + 10.0 ** (-f["dr"] / S))
    return pr._wdl(tot * share, tot * (1 - share), cfg)   # (p_home, p_draw, p_away)


def _fit_share_scale(feats: list, mu0: float, al: float, w: float, cfg) -> float:
    best = None
    for S in SHARE_GRID:
        ll = -sum(math.log(max(_wdl_at(f, mu0, al, w, S, cfg)[f["oc"]], 1e-12)) for f in feats)
        if best is None or ll < best[0]:
            best = (ll, S)
    return best[1]


def _brier_logloss(feats: list, mu0: float, al: float, w: float, S: float, cfg) -> tuple:
    briers, ll = [], 0.0
    for f in feats:
        p = _wdl_at(f, mu0, al, w, S, cfg)
        briers.append(sum((p[i] - (1 if i == f["oc"] else 0)) ** 2 for i in range(3)))
        ll -= math.log(max(p[f["oc"]], 1e-12))
    n = len(feats)
    return sum(briers) / n, ll / n, briers          # per-match briers for a paired SE


def _reliability(feats: list, params: tuple, S: float, cfg) -> dict:
    """Holdout draw-rate and favourite-win-rate calibration GAP (|model−actual|) by
    |Elo gap| bin — the dominance-resolved W/D/L signal the aggregate Brier hides.
    The favourite is the dr-implied side; this is where a totals change actually
    shows up in W/D/L (mismatch draw rates), so it is the informative OOS statistic."""
    agg = {b: {"n": 0, "a_dr": 0, "a_fw": 0, "m_dr": 0.0, "m_fw": 0.0} for b in DR_BINS}
    for f in feats:
        b = next((b for b in DR_BINS if b[0] <= abs(f["dr"]) < b[1]), None)
        if b is None:
            continue
        p = _wdl_at(f, *params, S, cfg)
        fav_actual = 1 if (f["dr"] > 0 and f["oc"] == 0) or (f["dr"] <= 0 and f["oc"] == 2) else 0
        p_fav = p[0] if f["dr"] > 0 else p[2]
        d = agg[b]
        d["n"] += 1
        d["a_dr"] += int(f["oc"] == 1)
        d["a_fw"] += fav_actual
        d["m_dr"] += p[1]
        d["m_fw"] += p_fav
    out = {}
    for b, d in agg.items():
        if d["n"]:
            out[b] = {"n": d["n"],
                      "draw_gap": abs(d["m_dr"] / d["n"] - d["a_dr"] / d["n"]),
                      "fav_gap": abs(d["m_fw"] / d["n"] - d["a_fw"] / d["n"])}
    return out


def oos_validation(corpus: list, holdout_from: str) -> dict:
    import statistics
    rows = _elo_pass(corpus)
    train = [m for m in rows if "2010-01-01" <= m["date"] < holdout_from]
    hold = [m for m in rows if m["date"] >= holdout_from]
    z_att, z_def = _attdef_z(train)                 # TRAIN-only ratings (no holdout leak)
    ftr, fho = _features(train, z_att, z_def), _features(hold, z_att, z_def)
    cfg = pr.Config()                               # rho = 0
    cur = _fit_total_params(ftr, with_maher=False)
    mah = _fit_total_params(ftr, with_maher=True)
    S = _fit_share_scale(ftr, *cur, cfg)            # share calibrated once, SHARED
    cur_bs, cur_ll, cur_b = _brier_logloss(fho, *cur, S, cfg)
    mah_bs, mah_ll, mah_b = _brier_logloss(fho, *mah, S, cfg)
    diffs = [m - c for m, c in zip(mah_b, cur_b)]    # paired per-match Brier difference
    md = statistics.mean(diffs)
    se = (statistics.pstdev(diffs) / math.sqrt(len(diffs))) if len(diffs) > 1 else 0.0
    mae = lambda p: sum(abs(_total(*p, f["ha"], f["hb"]) - f["tot"]) for f in fho) / len(fho)
    return {"n_train": len(ftr), "n_hold": len(fho), "S": S, "cur": cur, "mah": mah,
            "cur_brier": cur_bs, "mah_brier": mah_bs, "cur_ll": cur_ll, "mah_ll": mah_ll,
            "cur_mae": mae(cur), "mah_mae": mae(mah), "brier_diff": md, "brier_se": se,
            "rel_cur": _reliability(fho, cur, S, cfg), "rel_mah": _reliability(fho, mah, S, cfg)}


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit Maher-form total knobs, gated on W/D/L.")
    ap.add_argument("--corpus", type=Path, default=fr.CORPUS)
    ap.add_argument("--elo-start", default="1994-01-01")
    ap.add_argument("--analyze-from", default="2010-01-01")
    ap.add_argument("--fixtures", type=Path, default=REPO / "data" / "fixtures.csv")
    ap.add_argument("--write", action="store_true",
                    help="persist the fit to calibration.json (only if all gates pass)")
    ap.add_argument("--holdout", default="2023-01-01",
                    help="OOS holdout cutoff (matches on/after are held out)")
    ap.add_argument("--oos", action="store_true",
                    help="also run the out-of-sample W/D/L Brier gate (corpus train/holdout)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if not args.corpus.exists():
        print(f"error: corpus not found at {args.corpus} (see data/History/DATA_QUALITY.md)",
              file=sys.stderr)
        return 1

    corpus = fr.load_curated(args.corpus, args.elo_start)
    emp = empirical_curve(bt.forward_elo(corpus, args.analyze_from))
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
    oos = oos_validation(corpus, args.holdout) if args.oos else None
    # noise-aware: PASS unless the Maher form is SIGNIFICANTLY worse OOS (paired Brier
    # Δ more than 2 SE above zero). A statistically-zero change is the "no harm" result.
    oos_ok = (oos["brier_diff"] <= 2 * oos["brier_se"]) if oos else True
    accept = totals_ok and wdl_ok and oos_ok

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
    if oos:
        md, se = oos["brier_diff"], oos["brier_se"]
        t = md / se if se else 0.0
        mae_d = oos["mah_mae"] - oos["cur_mae"]
        verdict = ("statistically indistinguishable" if abs(t) < 2
                   else "significantly WORSE" if md > 0 else "significantly better")
        print("\nOOS W/D/L NON-DEGRADATION gate (SAFETY only: tests the total-form change "
              "doesn't hurt W/D/L;\n  share/supremacy harm is NOT tested by design, and "
              "this uses a corpus STAND-IN, not the shipped config):")
        print(f"  train {oos['n_train']:,} (2010..{args.holdout}) / holdout {oos['n_hold']:,} "
              f"(>= {args.holdout}); shared share-scale S={oos['S']}.")
        print(f"  stand-in total params: current {tuple(round(x, 3) for x in oos['cur'])} "
              f"vs Maher {tuple(round(x, 3) for x in oos['mah'])} (NOT the production 2.45/0.30/1.0).")
        print(f"  holdout Brier {oos['cur_brier']:.4f} -> {oos['mah_brier']:.4f}  "
              f"(paired Δ {md:+.5f} ± {se:.5f}, t={t:+.2f} — {verdict})")
        print(f"  holdout log-loss {oos['cur_ll']:.4f} -> {oos['mah_ll']:.4f};  totals MAE "
              f"{oos['cur_mae']:.3f} -> {oos['mah_mae']:.3f} "
              f"({'flat (Δ<0.005)' if abs(mae_d) < 0.005 else f'{mae_d:+.3f}'}).")
        print("  dominance-resolved reliability — |model−actual| W/D/L gap by |Elo gap| bin "
              "(where a totals change actually shows up):")
        print(f"    {'|dr|':>10} {'n':>5} | {'draw cur':>8} {'draw mah':>8} | {'fav cur':>8} {'fav mah':>8}")
        for b in DR_BINS:
            rc, rm = oos["rel_cur"].get(b), oos["rel_mah"].get(b)
            if rc and rm:
                lbl = f"{b[0]}-{b[1] if b[1] < 9999 else '∞'}"
                print(f"    {lbl:>10} {rc['n']:>5} | {rc['draw_gap']:>8.3f} {rm['draw_gap']:>8.3f} "
                      f"| {rc['fav_gap']:>8.3f} {rm['fav_gap']:>8.3f}")
        print(f"  OOS gate: {'PASS (not significantly degraded)' if oos_ok else 'FAIL (significantly worse)'}")
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
        meta = {"corpus_matches": sum(b["n"] for b in emp.values()),
                "totals_sse": [round(base_sse, 3), round(best[0], 3)],
                "wdl_err": [round(wcur, 5), round(wbest, 5)]}
        if oos:
            meta["oos_brier"] = [round(oos["cur_brier"], 5), round(oos["mah_brier"], 5)]
            meta["oos_totals_mae"] = [round(oos["cur_mae"], 4), round(oos["mah_mae"], 4)]
        cal.update({"mu0": mu0, "alpha": alpha, "maher_w": w, "maher_fit": meta})
        fr.CALIBRATION.write_text(json.dumps(cal, indent=2), encoding="utf-8")
        print(f"wrote {fr.CALIBRATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
