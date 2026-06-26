#!/usr/bin/env python3
"""Third-model bake-off: structural vs our hybrid vs the reference XGBoost model
that motivated this whole exercise (/Users/bnowak/dev/world_cup_predictions).

The reference model is the "first ML project" repo Brian forked the hybrid
idea from. It's XGBoost too — and richer than our v1 hybrid: rolling Elo,
5/10-match form, rest days, head-to-head, neutral/tournament weight. Worth
benchmarking against because (a) it was the comparison point all along and (b)
seeing how all three score against the actual played wc26 matches is the kind
of A/B/C the project's Brier ledger is designed for.

Fairness notes:
  * Reference model TRAINED ONCE with cutoff = 2026-06-11 (tournament start),
    same way our structural ratings were frozen on June 11. No look-ahead.
  * At prediction time their model uses match_date for the as-of features
    (Elo, form, rest days) — so a Round-1 prediction sees Elo as of pre-tournament,
    a Round-2 prediction sees Elo updated with Round-1 results, etc. Same as our
    structural's behaviour (it has fixed ratings, so for Round 2 it'd be slightly
    stale, but we're only on Round 1 so no divergence yet).
  * Reference model treats every wc26 game as neutral (a stated v1 simplification).
    Our structural model correctly applies HFA for the 3 host nations playing
    at home. This is a real difference in modeling, not a bug.

Usage:
    python scripts/compare_with_reference.py [--experiment experiments/cal_minimal_tau5_iso]
                                              [--output experiments/THREE_WAY.md]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/Users/bnowak/dev/world_cup_predictions")

import hybrid as hy            # noqa: E402
import predict as pr           # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_EXP = REPO / "experiments" / "cal_minimal_tau5_iso"
TOURNAMENT_START = "2026-06-11"

# Our canon (CLAUDE.md) -> reference repo's internal name (post-NAME_MAP).
# Everything not in the map passes through unchanged.
CANON_TO_REF = {
    "Türkiye": "Turkey",
    "Côte d'Ivoire": "Ivory Coast",
    "Czechia": "Czech Republic",
    "Curaçao": "Curacao",
    "Cape Verde": "Cabo Verde",
    # All other canon names (United States, South Korea, DR Congo, Iran, etc.)
    # match the reference repo's internal canon as-is.
}


def to_ref(name: str) -> str:
    return CANON_TO_REF.get(name.strip(), name.strip())


def rps(p: tuple, outcome: int) -> float:
    f1, f2 = p[0], p[0] + p[1]
    o1 = 1.0 if outcome == 0 else 0.0
    o2 = 1.0 if outcome <= 1 else 0.0
    return 0.5 * ((f1 - o1) ** 2 + (f2 - o2) ** 2)


def blend(struct_triple: tuple, ref_triple: tuple,
          w_struct: float = 0.5) -> tuple:
    """Linear blend of two W/D/L triples, renormalised to sum to 1.

    Default 0.5 is the *a priori* ensemble weight — picked before seeing the
    scored matches, NOT tuned against the played slice (which would overfit at
    n=18). Standard "equal-weight ensemble" baseline from the literature, and
    the right default when both models have similar marginal quality but
    different error shapes.
    """
    w_ref = 1.0 - w_struct
    blended = tuple(w_struct * s + w_ref * r
                    for s, r in zip(struct_triple, ref_triple))
    z = sum(blended)
    return tuple(b / z for b in blended)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Structural vs hybrid vs reference comparison.")
    ap.add_argument("--experiment", type=Path, default=DEFAULT_EXP)
    ap.add_argument("--output", type=Path, default=REPO / "experiments" / "THREE_WAY.md")
    ap.add_argument("--upcoming-dates", default="2026-06-16,2026-06-17",
                    help="comma-separated ET dates whose scheduled matches "
                         "get a forward-look preview section (default: today + tomorrow)")
    ap.add_argument("--ensemble-w-struct", type=float, default=0.5,
                    help="weight on structural in the ensemble (rest goes to "
                         "reference). 0.5 = equal-weight a priori default; "
                         "0.3 = reference-heavy; etc.")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    print("Loading reference model (one-time train) …", file=sys.stderr)
    # Reference model: train with cutoff at tournament start (no look-ahead).
    # This is the SAME amount of data the structural model's ratings were frozen
    # with on June 11 — the fair benchmark.
    #
    # IMPORTANT — leakage fix (2026-06-16): the reference repo's cached results.csv
    # contains played wc26 matches with their REAL scores. `final_elo` and `long`
    # are built from the FULL corpus and used directly as features (home_elo,
    # away_elo, elo_diff) at predict time, with NO as-of filter. That means a
    # 6/13 Brazil-Morocco prediction would see Brazil's Elo already adjusted for
    # the 1-1 draw — leakage through the rating, even though the booster never
    # saw the match as a training row. Truncate the corpus to pre-tournament so
    # `final_elo` is the Elo as of 2026-06-10 — apples-to-apples with structural
    # and hybrid, which use frozen 6/11 ratings.
    import pandas as pd
    import predict_today as ref
    results = ref.load_results()
    n_before = len(results)
    results = results[results["date"] < pd.Timestamp(TOURNAMENT_START)].copy()
    print(f"  filtered corpus to pre-tournament (date < {TOURNAMENT_START}): "
          f"{n_before} -> {len(results)} rows", file=sys.stderr)
    dataset, final_elo = ref.build_dataset(results)
    long = ref.per_team_long(results)
    train, val = ref.split_by_date(dataset, ref.TRAIN_START, ref.VAL_START, TOURNAMENT_START)
    ref_model, _, _ = ref.train_model(train, val)
    print(f"  reference model trained on {len(train)} matches "
          f"({ref.TRAIN_START}..{TOURNAMENT_START})", file=sys.stderr)

    # Load our hybrid
    art = args.experiment / "hybrid.ubj"
    meta = args.experiment / "hybrid.meta.json"
    booster = hy._load_booster(artifact_path=art, meta_path=meta)
    if booster is None:
        print(f"error: could not load hybrid from {args.experiment}", file=sys.stderr)
        return 1

    model = pr.load_ratings()

    # The upcoming-fixtures window for this run. By default: today + tomorrow
    # in ET — the matches whose three-way preview is genuinely useful right now
    # (tomorrow's slate is what tonight's edition would publish).
    upcoming_dates = {d.strip() for d in args.upcoming_dates.split(",") if d.strip()}

    # Walk fixtures once; partition into "played" (gets RPS + winner) and
    # "upcoming" (gets predictions only, no RPS / actual / winner since the
    # outcome doesn't exist yet).
    played_fix, upcoming_fix = [], []
    with (REPO / "data" / "fixtures.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            status = row["status"].strip()
            date = row["date_et"].strip()
            if status == "played":
                played_fix.append(row)
            elif date in upcoming_dates:
                upcoming_fix.append(row)

    print(f"  scoring against {len(played_fix)} played wc26 matches", file=sys.stderr)
    print(f"  previewing {len(upcoming_fix)} upcoming matches "
          f"({', '.join(sorted(upcoming_dates))})", file=sys.stderr)

    def predict_three_way(r):
        """All three models for one fixture row. Returns (s_triple, h_triple,
        ref_triple, a, b, hfa, match_date) — the bits both tables need."""
        a, b = r["team_a"].strip(), r["team_b"].strip()
        host = pr.HOST_BY_COUNTRY.get((r.get("country") or "").strip())
        hfa = host if host in (a, b) else None
        match_date = r["date_et"]

        s = pr.predict_match(model, a, b, hfa_team=hfa)
        s_triple = (s.p_a, s.p_draw, s.p_b)

        feature_names = tuple(booster.meta["feature_names"])
        feats = hy.extract_features_live(model, s, a, b, hfa_team=hfa,
                                          feature_names=feature_names)
        ha, hd, hb = booster.predict_proba(feats)
        z = ha + hd + hb
        h_triple = (ha / z, hd / z, hb / z)

        ra, rb_ = to_ref(a), to_ref(b)
        rp = ref.predict_symmetric(ref_model, long, final_elo,
                                    ra, rb_, match_date,
                                    ref.MATCH_NEUTRAL, ref.MATCH_WEIGHT)
        ref_triple = (float(rp[0]), float(rp[1]), float(rp[2]))
        return s_triple, h_triple, ref_triple, a, b, hfa, match_date

    rows = []
    for r in played_fix:
        s_triple, h_triple, ref_triple, a, b, hfa, match_date = predict_three_way(r)
        ens_triple = blend(s_triple, ref_triple, w_struct=args.ensemble_w_struct)
        sa, sb = int(r["score_a"]), int(r["score_b"])
        oc = 0 if sa > sb else (1 if sa == sb else 2)
        rows.append({
            "match_id": r["match_id"], "date": match_date,
            "team_a": a, "team_b": b, "hfa": hfa,
            "actual": f"{sa}-{sb}", "outcome_idx": oc,
            "s": s_triple, "h": h_triple, "r": ref_triple, "e": ens_triple,
            "rps_s": rps(s_triple, oc), "rps_h": rps(h_triple, oc),
            "rps_r": rps(ref_triple, oc), "rps_e": rps(ens_triple, oc),
        })

    upcoming_rows = []
    for r in upcoming_fix:
        s_triple, h_triple, ref_triple, a, b, hfa, match_date = predict_three_way(r)
        ens_triple = blend(s_triple, ref_triple, w_struct=args.ensemble_w_struct)
        upcoming_rows.append({
            "match_id": r["match_id"], "date": match_date,
            "kickoff": r["kickoff_et"].strip(),
            "team_a": a, "team_b": b, "hfa": hfa,
            "s": s_triple, "h": h_triple, "r": ref_triple, "e": ens_triple,
        })

    # ------------------------------------------------------------ aggregates
    n = len(rows)
    mean_s = sum(r["rps_s"] for r in rows) / n
    mean_h = sum(r["rps_h"] for r in rows) / n
    mean_r = sum(r["rps_r"] for r in rows) / n
    mean_e = sum(r["rps_e"] for r in rows) / n

    def per_match_winner(row):
        triples = [("S", row["rps_s"]), ("H", row["rps_h"]),
                   ("R", row["rps_r"]), ("E", row["rps_e"])]
        triples.sort(key=lambda x: x[1])
        if triples[1][1] - triples[0][1] < 1e-6:
            return "tie"
        return triples[0][0]
    wins = {"S": 0, "H": 0, "R": 0, "E": 0, "tie": 0}
    for r in rows:
        wins[per_match_winner(r)] += 1

    def fmt_pct(p): return f"{round(p*100):2d}"
    def fmt_triple(t): return f"{fmt_pct(t[0])}/{fmt_pct(t[1])}/{fmt_pct(t[2])}"

    lines = [
        "# Three-way bake-off — structural vs our hybrid vs reference XGBoost",
        "",
        f"_Hybrid experiment: `{args.experiment.name}`. "
        f"Reference: `~/dev/world_cup_predictions/predict_today.py` "
        f"(XGBoost + Elo + form + rest + H2H, trained through {TOURNAMENT_START} "
        f"on {len(train)} matches)._  ",
        f"_Scored against {n} played wc26 matches._",
        "",
        "## Aggregate RPS (lower is better)",
        "",
        f"| model | mean RPS | vs structural | head-to-head wins |",
        f"|---|---|---|---|",
        f"| **structural** (incumbent) | **{mean_s:.4f}** | — | {wins['S']} |",
        f"| our hybrid (cal_minimal_tau5_iso) | {mean_h:.4f} | "
        f"{100*(mean_h-mean_s)/mean_s:+.2f}% | {wins['H']} |",
        f"| reference XGBoost (Elo frozen 6/10) | {mean_r:.4f} | "
        f"{100*(mean_r-mean_s)/mean_s:+.2f}% | {wins['R']} |",
        f"| **ensemble** ({int(args.ensemble_w_struct*100)}/{int((1-args.ensemble_w_struct)*100)} structural + reference) | **{mean_e:.4f}** | "
        f"**{100*(mean_e-mean_s)/mean_s:+.2f}%** | **{wins['E']}** |",
        f"| _ties_ | — | — | {wins['tie']} |",
        "",
        "_Ensemble weights chosen a priori (50/50), NOT tuned against this slice — "
        "with n=18 played matches, fitting weights to maximize aggregate RPS would "
        "overfit. The classical equal-weight ensemble is the right baseline for two "
        "models with similar marginal quality but different error shapes._",
        "",
        "## Per-match comparison",
        "",
        "| date | match | actual | structural | our hybrid | reference XGB | ensemble | RPS S / H / R / E | winner |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: (r["date"], r["match_id"])):
        a_tag = f"🏠 {r['team_a']}" if r["hfa"] == r["team_a"] else r["team_a"]
        b_tag = f"🏠 {r['team_b']}" if r["hfa"] == r["team_b"] else r["team_b"]
        winner = per_match_winner(r)
        winner_str = {"S": "structural", "H": "**hybrid**",
                      "R": "**reference**", "E": "**ensemble**",
                      "tie": "tie"}[winner]
        lines.append(
            f"| {r['date']} | {a_tag} vs {b_tag} | {r['actual']} | "
            f"{fmt_triple(r['s'])} | {fmt_triple(r['h'])} | {fmt_triple(r['r'])} | "
            f"{fmt_triple(r['e'])} | "
            f"{r['rps_s']:.3f} / {r['rps_h']:.3f} / {r['rps_r']:.3f} / {r['rps_e']:.3f} | "
            f"{winner_str} |"
        )

    # ---------------------------------------------------- upcoming-matches section
    # Predictions only — no actual / RPS / winner columns since these games
    # haven't happened yet. Useful for previewing today's edition + tomorrow's
    # ahead of kickoff, and as a record we can grade against later.
    if upcoming_rows:
        lines += [
            "",
            f"## Upcoming matches — three-way preview ({', '.join(sorted(upcoming_dates))})",
            "",
            "_Predictions only — these games haven't been played. Same model "
            "instances used in the Aggregate table above; this is a forward "
            "look, not a backtest._",
            "",
            "| date | kickoff | match | structural | our hybrid | reference XGB | ensemble | biggest disagreement |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in sorted(upcoming_rows, key=lambda r: (r["date"], r["match_id"])):
            a_tag = f"🏠 {r['team_a']}" if r["hfa"] == r["team_a"] else r["team_a"]
            b_tag = f"🏠 {r['team_b']}" if r["hfa"] == r["team_b"] else r["team_b"]
            # Per-class max |delta| across all three component models — surfaces
            # the match where the models disagree most, regardless of which
            # pair. (Ensemble is a function of the others, not a 4th opinion.)
            max_spread_pp = 0
            spread_class = ""
            for cls_idx, label in [(0, "A"), (1, "D"), (2, "B")]:
                triples = [r["s"][cls_idx], r["h"][cls_idx], r["r"][cls_idx]]
                spread = round((max(triples) - min(triples)) * 100)
                if spread > max_spread_pp:
                    max_spread_pp, spread_class = spread, label
            spread_str = f"{max_spread_pp}pp on {spread_class}" if max_spread_pp else "—"
            lines.append(
                f"| {r['date']} | {r['kickoff']} | {a_tag} vs {b_tag} | "
                f"{fmt_triple(r['s'])} | {fmt_triple(r['h'])} | {fmt_triple(r['r'])} | "
                f"{fmt_triple(r['e'])} | {spread_str} |"
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {args.output.relative_to(REPO)}")
    print(f"  structural {mean_s:.4f}  |  hybrid {mean_h:.4f} "
          f"({100*(mean_h-mean_s)/mean_s:+.2f}%)  |  reference {mean_r:.4f} "
          f"({100*(mean_r-mean_s)/mean_s:+.2f}%)  |  ensemble {mean_e:.4f} "
          f"({100*(mean_e-mean_s)/mean_s:+.2f}%)")
    print(f"  head-to-head: structural {wins['S']}  hybrid {wins['H']}  "
          f"reference {wins['R']}  ensemble {wins['E']}  ties {wins['tie']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
