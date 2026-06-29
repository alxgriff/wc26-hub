"""edge_drd.py — Deserved-Result Divergence: a tagged edge-hypothesis tracker.

THESIS (from the 2026-06-18 investigation). The market is the strongest single
predictor, so a strength model is not an edge. The one input plausibly ORTHOGONAL
to the market is "deserved result": futi.live is an Expected-Possession-Value model
(it scores how a team PLAYED), whereas the market and Elo are anchored on RESULTS /
reputation. So a team's process-vs-reputation gap is

    gap(team) = z(Futi) - z(Elo)            # +ve: plays better than its record says

and the market, being reputation-anchored, sits near the Elo view. Where our PROCESS
view (the live Futi-tilted model) upgrades a side that the market has NOT priced up
(market ≈ Elo on that side), the market is leaving a deserved-result edge on the table.

Per match (team_a vs team_b) we compute three W/D/L views — Elo-only (reputation),
the live Futi-tilted model (process), and the de-vigged market consensus — pick the
side our process view most upgrades over Elo, and tag it when:
  drd_edge      = process_p - market_p   >= --edge   (bettable gap vs the price)
  process_lean  = process_p - elo_p      >= --lean   (the gap is genuinely process-driven)
  market_capture= market_p - elo_p       <  process_lean   (market hasn't priced it)
This filters generic model-vs-market noise down to UNPRICED process divergence.

Leakage discipline (same as the model): the backward validation is vintage-aware —
MD1 games use the PRE-tournament 6/12 Futi (the 6/18 file absorbed MD1 results, corr
+0.36 with MD1 points), MD2+ use 6/18. Forward picks log the snapshot market implied
so CLV (closing - snapshot) is the verdict, not units. Paper only, stake 0.

Usage:
    python scripts/edge_drd.py board                 # today's unpriced-divergence opportunities
    python scripts/edge_drd.py validate              # backward read on played games (leak-free)
    python scripts/edge_drd.py log [--edge 0.04]     # append qualifying upcoming picks (paper)
    python scripts/edge_drd.py report                # CLV-by-tag on logged picks
"""
from __future__ import annotations
import argparse
import csv
import statistics as stats
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as P
import odds as O
import ledger as L

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "data" / "fixtures.csv"
DRD_LOG = REPO / "data" / "edge_drd_log.csv"
TAG = "deserved-result-divergence"
FUTI_PRE = "World_Cup_2026_Futi_Final_Fixed_Futi_Detailed_Profiles_Final.csv"   # 6/12, pre-tournament
FUTI_NOW = P.FUTI_FILE   # the production vintage — follows predict.py (whatever FUTI_FILE points
#                          at) so the live board/log never go stale when the Futi file is bumped.
#                          FUTI_PRE stays fixed: the pre-tournament file for leak-free MD1 validation.

LOG_COLS = ["match_id", "side", "team", "as_of", "model_p", "market_p", "drd_edge",
            "process_lean", "market_capture", "price_decimal", "price_american", "book",
            "snapshot_implied", "status", "result", "won", "closing_implied", "clv_pp"]

SIDE_NAME = {0: "home", 2: "away"}        # 1 = draw is not a DRD side (no "deserved win" lean)


def _cfg(w_elo: float, w_futi: float) -> P.Config:
    """Calibrated config (mirrors production) with an explicit Elo/Futi blend."""
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


def build_models(futi_file: str) -> tuple:
    """(process_model, reputation_model) for a given Futi vintage. Process = the live
    1:1.5 Futi tilt; reputation = Elo-only (w_futi=0). Restores P.FUTI_FILE after."""
    saved = P.FUTI_FILE
    try:
        P.FUTI_FILE = futi_file
        proc = P.load_ratings(config=_cfg(1.0, 1.5))
        rep = P.load_ratings(config=_cfg(1.0, 0.0))
    finally:
        P.FUTI_FILE = saved
    return proc, rep


def team_gaps(proc) -> dict:
    """gap = z(Futi) - z(Elo) per team (the process-minus-reputation signal)."""
    elo = {t: proc.teams[t].elo for t in proc.teams}
    fut = {t: proc.teams[t].futi for t in proc.teams}

    def z(d):
        mu, sd = stats.mean(d.values()), stats.pstdev(d.values()) or 1.0
        return {k: (v - mu) / sd for k, v in d.items()}
    ze, zf = z(elo), z(fut)
    return {t: zf[t] - ze[t] for t in proc.teams}


def market_wdl(odds_rows: list, mid: str, phase: str = "snapshot") -> tuple | None:
    """De-vigged consensus (home, draw, away) from the latest median h2h rows for
    the given phase (snapshot for the price we took, closing for CLV)."""
    m = O.latest_market(odds_rows, mid, "h2h", phase=phase, source_prefix="median")
    try:
        h, d, a = m[("home", "")][0], m[("draw", "")][0], m[("away", "")][0]
    except KeyError:
        return None
    return tuple(O.devig([h, d, a]))


def best_price(odds_rows: list, mid: str, side: str) -> tuple:
    """(decimal, book) best available price for a side, or (None, '')."""
    m = O.latest_market(odds_rows, mid, "h2h", phase="snapshot", source_prefix="best")
    # best rows carry source 'best:<book>' but latest_market drops the book; re-read raw
    rows = [r for r in odds_rows if r["match_id"] == mid and r["market"] == "h2h"
            and r["phase"] == "snapshot" and r["selection"] == side
            and r["source"].startswith("best")]
    if not rows:
        return (None, "")
    last = max(rows, key=lambda r: r["timestamp"])
    book = last["source"].split(":", 1)[1] if ":" in last["source"] else ""
    return (float(last["odds"]), book)


def drd_for_match(proc, rep, a: str, b: str, hfa, mkt: tuple) -> dict | None:
    """Compute the deserved-result-divergence read for one match. mkt=(home,draw,away)."""
    pe = P.predict_match(rep, a, b, hfa_team=hfa)
    pp = P.predict_match(proc, a, b, hfa_team=hfa)
    elo_t = (pe.p_a, pe.p_draw, pe.p_b)
    proc_t = (pp.p_a, pp.p_draw, pp.p_b)
    # the side our process view most upgrades vs reputation (win sides only)
    lean = {0: proc_t[0] - elo_t[0], 2: proc_t[2] - elo_t[2]}
    S = max(lean, key=lean.get)
    return {
        "side": S, "team": a if S == 0 else b,
        "model_p": proc_t[S], "elo_p": elo_t[S], "market_p": mkt[S],
        "process_lean": proc_t[S] - elo_t[S],
        "market_capture": mkt[S] - elo_t[S],
        "drd_edge": proc_t[S] - mkt[S],
    }


def _qualifies(d: dict, edge: float, lean: float) -> bool:
    return (d["drd_edge"] >= edge and d["process_lean"] >= lean
            and d["market_capture"] < d["process_lean"])


# ---------------------------------------------------------------- data
def fixtures() -> list:
    with FIXTURES.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _hfa(proc, row, a, b):
    host = P.HOST_BY_COUNTRY.get((row.get("country") or "").strip())
    return host if host in (a, b) else None


def _models_for(matchday: str, cache: dict) -> tuple:
    """Vintage-aware: MD1 -> 6/12 (pre-tournament), MD2+ -> 6/18. Leak-free for validation."""
    key = "pre" if str(matchday).strip() == "1" else "now"
    if key not in cache:
        cache[key] = build_models(FUTI_PRE if key == "pre" else FUTI_NOW)
    return cache[key]


# ---------------------------------------------------------------- commands
def cmd_board(args) -> None:
    proc, rep = build_models(FUTI_NOW)
    gaps = team_gaps(proc)
    odds_rows = O.load_odds()
    rows = []
    for r in fixtures():
        if (r.get("status") or "").strip().lower() == "played":
            continue
        a, b = P._canon(r["team_a"]), P._canon(r["team_b"])
        if a not in proc.teams or b not in proc.teams:
            continue
        mkt = market_wdl(odds_rows, r["match_id"])
        if not mkt:
            continue
        d = drd_for_match(proc, rep, a, b, _hfa(proc, r, a, b), mkt)
        d["match_id"], d["matchup"] = r["match_id"], f"{a} v {b}"
        rows.append(d)
    rows.sort(key=lambda d: -d["drd_edge"])
    print(f"\nDeserved-Result Divergence board  (process=1:1.5 Futi tilt / 6-18 ; reputation=Elo-only)")
    print(f"{'match':>5} {'side':>4} {'team':<16} {'model':>6} {'mkt':>6} {'drd_edge':>9} "
          f"{'proc_lean':>9} {'mkt_cap':>8}  flag")
    for d in rows:
        flag = "  <= UNPRICED DIVERGENCE" if _qualifies(d, args.edge, args.lean) else ""
        print(f"{d['match_id']:>5} {SIDE_NAME[d['side']]:>4} {d['team'][:16]:<16} "
              f"{d['model_p']*100:>5.0f}% {d['market_p']*100:>5.0f}% {d['drd_edge']*100:>+8.1f}pp "
              f"{d['process_lean']*100:>+8.1f}pp {d['market_capture']*100:>+7.1f}pp{flag}")
    n = sum(1 for d in rows if _qualifies(d, args.edge, args.lean))
    print(f"\n{n} qualifying unpriced-divergence pick(s) "
          f"(edge>={args.edge:.0%}, lean>={args.lean:.0%}, market_capture<lean).")


def cmd_validate(args) -> None:
    """Leak-free backward read: on each played game, take the DRD-favoured side at the
    pre-kickoff market price and ask whether it WON more than the market implied."""
    cache = {}
    odds_rows = O.load_odds()
    picks = []
    for r in fixtures():
        if (r.get("status") or "").strip().lower() != "played":
            continue
        a, b = P._canon(r["team_a"]), P._canon(r["team_b"])
        proc, rep = _models_for(r.get("matchday", ""), cache)
        if a not in proc.teams or b not in proc.teams:
            continue
        mkt = market_wdl(odds_rows, r["match_id"])
        if not mkt:
            continue
        d = drd_for_match(proc, rep, a, b, _hfa(proc, r, a, b), mkt)
        try:
            sa, sb = int(r["score_a"]), int(r["score_b"])
        except (ValueError, TypeError):
            continue
        won = 1 if ((d["side"] == 0 and sa > sb) or (d["side"] == 2 and sb > sa)) else 0
        d.update(match_id=r["match_id"], matchday=r.get("matchday", ""), won=won)
        picks.append(d)

    if not picks:
        print("no played games with a market snapshot to validate on.")
        return
    qual = [d for d in picks if _qualifies(d, args.edge, args.lean)]
    for label, ps in (("ALL DRD-favoured sides", picks), (f"QUALIFYING (edge>={args.edge:.0%})", qual)):
        if not ps:
            print(f"\n{label}: none"); continue
        wins = sum(d["won"] for d in ps)
        implied = sum(d["market_p"] for d in ps)          # market's expected wins
        proc_exp = sum(d["model_p"] for d in ps)          # our process model's expected wins
        roi = sum((d["won"] / d["market_p"] - 1) for d in ps) / len(ps)   # flat-stake ROI at fair price
        print(f"\n{label}  (n={len(ps)})")
        print(f"  actual wins {wins}  vs market-implied {implied:.1f}  vs our-model {proc_exp:.1f}")
        print(f"  -> DRD side beat the market by {wins - implied:+.1f} wins; flat-stake ROI {roi:+.1%} "
              f"(at the de-vigged fair price; small n — directional only)")
    print("\nNB ~24 of the played games are MD1 (6/12 ratings, leak-free). This is a first read, "
          "not significance — the forward CLV log is the real test.")


def cmd_log(args) -> None:
    proc, rep = build_models(FUTI_NOW)
    odds_rows = O.load_odds()
    existing = L_load(DRD_LOG)
    have = {(r["match_id"], r["side"]) for r in existing}
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    added = 0
    for r in fixtures():
        if (r.get("status") or "").strip().lower() == "played":
            continue
        a, b = P._canon(r["team_a"]), P._canon(r["team_b"])
        if a not in proc.teams or b not in proc.teams:
            continue
        mkt = market_wdl(odds_rows, r["match_id"])
        if not mkt:
            continue
        d = drd_for_match(proc, rep, a, b, _hfa(proc, r, a, b), mkt)
        if not _qualifies(d, args.edge, args.lean):
            continue
        side = SIDE_NAME[d["side"]]
        if (r["match_id"], side) in have:
            continue
        price, book = best_price(odds_rows, r["match_id"], side)
        existing.append({
            "match_id": r["match_id"], "side": side, "team": d["team"], "as_of": now,
            "model_p": f"{d['model_p']:.4f}", "market_p": f"{d['market_p']:.4f}",
            "drd_edge": f"{d['drd_edge']*100:.1f}", "process_lean": f"{d['process_lean']*100:.1f}",
            "market_capture": f"{d['market_capture']*100:.1f}",
            "price_decimal": f"{price:.3f}" if price else "", "book": book,
            "price_american": O.american_odds(price) if price else "",
            "snapshot_implied": f"{d['market_p']:.4f}", "status": "open",
            "result": "", "won": "", "closing_implied": "", "clv_pp": ""})
        added += 1
    L_save(existing, DRD_LOG)
    print(f"logged {added} new deserved-result-divergence pick(s) (paper, tag={TAG}); "
          f"{len(existing)} total in {DRD_LOG.name}")


def cmd_report(args) -> None:
    rows = L_load(DRD_LOG)
    if not rows:
        print("no logged DRD picks yet — run `edge_drd.py log` after a fetch.")
        return
    odds_rows = O.load_odds()
    fx = {r["match_id"]: r for r in fixtures()}
    clvs, wins, n_settled = [], 0, 0
    for r in rows:
        # CLV only against a TRUE closing line (within CLOSING_WINDOW before kickoff) —
        # reuse odds.py's guard so far-out 'closing'-tagged rows don't fake a ~0 CLV
        f = fx.get(r["match_id"])
        try:
            ko = L.kickoff_dt(f) if f else None
        except Exception:
            ko = None
        if ko is not None and O._closing_is_timely(odds_rows, {"match_id": r["match_id"], "market": "h2h"}, ko):
            mkt_close = market_wdl(odds_rows, r["match_id"], phase="closing")
            if mkt_close:
                idx = {"home": 0, "away": 2}[r["side"]]
                clv = (mkt_close[idx] - float(r["snapshot_implied"])) * 100
                r["closing_implied"], r["clv_pp"] = f"{mkt_close[idx]:.4f}", f"{clv:+.1f}"
                clvs.append(clv)
        # settle result if played
        f = fx.get(r["match_id"])
        if f and (f.get("status") or "").strip().lower() == "played":
            try:
                sa, sb = int(f["score_a"]), int(f["score_b"])
                won = 1 if ((r["side"] == "home" and sa > sb) or (r["side"] == "away" and sb > sa)) else 0
                r["status"], r["result"], r["won"] = "settled", f"{sa}-{sb}", str(won)
                wins += won; n_settled += 1
            except (ValueError, TypeError):
                pass
    L_save(rows, DRD_LOG)
    print(f"\nDeserved-Result Divergence ledger ({TAG})  —  {len(rows)} picks")
    if clvs:
        beat = sum(1 for c in clvs if c > 0)
        print(f"  CLV: mean {stats.mean(clvs):+.2f}pp across {len(clvs)} priced picks; "
              f"{beat}/{len(clvs)} beat the close  <-- the real edge signal")
    else:
        print("  CLV: no closing snapshots yet — CLV is the verdict; check back after closing fetches.")
    if n_settled:
        print(f"  Results: {wins}/{n_settled} settled picks won.")


# tiny CSV helpers (the DRD log has its own schema; keep it off odds.py's loaders)
def L_load(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def L_save(rows: list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in LOG_COLS} for r in rows)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("board", "validate", "log", "report"):
        s = sub.add_parser(name)
        s.add_argument("--edge", type=float, default=0.04, help="min drd_edge (process_p - market_p)")
        s.add_argument("--lean", type=float, default=0.03, help="min process_lean (process_p - elo_p)")
    args = ap.parse_args()
    {"board": cmd_board, "validate": cmd_validate, "log": cmd_log,
     "report": cmd_report}.get(args.cmd, cmd_board)(args)


if __name__ == "__main__":
    main()
