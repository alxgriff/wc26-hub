#!/usr/bin/env python3
"""Per-game side-by-side: structural W/D/L vs hybrid overlay W/D/L for every
fixture in data/fixtures.csv. Loads a chosen experiment artifact directly
(does NOT touch data/calibration/) so production stays inert.

Emits a markdown table with both predictions + signed deltas. Sorts by biggest
disagreement first by default — that surfaces "the games where the hybrid is
actually making a different call" rather than burying them.

Usage:
    python scripts/compare_overlay.py [--experiment experiments/cal_minimal_tau5_iso]
                                       [--output experiments/COMPARISON.md]
                                       [--sort {disagreement,date,match_id}]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid as hy        # noqa: E402
import predict as pr       # noqa: E402
import standings as st     # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_EXP = REPO / "experiments" / "cal_minimal_tau5_iso"


def _hfa_for(team_a: str, team_b: str, country: str) -> str | None:
    """Same rule as predict._fixture_lookup: the host nation gets HFA when it
    plays at home. Kept in one place so the comparison and the live CLI agree."""
    host = pr.HOST_BY_COUNTRY.get((country or "").strip())
    return host if host in (team_a, team_b) else None


def _fixture_rows(path: Path) -> list:
    """Return the raw fixtures CSV rows in source order (we want both unplayed
    and played, since played games come with a real outcome to score against)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _pct_triple(p_a: float, p_d: float, p_b: float) -> str:
    return f"{round(p_a*100):2d}/{round(p_d*100):2d}/{round(p_b*100):2d}"


def _signed_delta_triple(s, h) -> tuple:
    """Hybrid minus structural, per class. + means hybrid is MORE bullish on that class."""
    return (h[0] - s[0], h[1] - s[1], h[2] - s[2])


def _max_abs_delta(deltas: tuple) -> float:
    return max(abs(d) for d in deltas)


def _outcome_label(score_a, score_b) -> tuple:
    """Convert (score_a, score_b) strings to (outcome_idx, "A-B"). Returns
    (None, "") for unplayed."""
    try:
        a, b = int(score_a), int(score_b)
    except (TypeError, ValueError):
        return None, ""
    if a > b:
        return 0, f"{a}-{b}"
    if a == b:
        return 1, f"{a}-{b}"
    return 2, f"{a}-{b}"


def _score_call(struct: tuple, hyb: tuple, outcome_idx: int | None) -> str:
    """Which model got closer on RPS? Returns "→S" or "→H" or "=" — empty when unplayed."""
    if outcome_idx is None:
        return ""
    def rps(p, oc):
        f1, f2 = p[0], p[0] + p[1]
        o1 = 1.0 if oc == 0 else 0.0
        o2 = 1.0 if oc <= 1 else 0.0
        return 0.5 * ((f1 - o1) ** 2 + (f2 - o2) ** 2)
    rs, rh = rps(struct, outcome_idx), rps(hyb, outcome_idx)
    if abs(rs - rh) < 1e-6:
        return "tie"
    return "**H**" if rh < rs else "S"


def predict_pair(model: pr.RatingModel, booster: hy._Booster,
                 team_a: str, team_b: str, hfa_team: str | None) -> tuple:
    """Both predictions for one match. Hybrid uses the artifact's own
    feature_names so the booster sees exactly what it was trained on."""
    struct = pr.predict_match(model, team_a, team_b, hfa_team=hfa_team)
    feature_names = tuple(booster.meta["feature_names"])
    feats = hy.extract_features_live(model, struct, team_a, team_b,
                                     hfa_team=hfa_team, feature_names=feature_names)
    p_a, p_d, p_b = booster.predict_proba(feats)
    z = p_a + p_d + p_b
    return ((struct.p_a, struct.p_draw, struct.p_b),
            (p_a / z, p_d / z, p_b / z))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Side-by-side per-game comparison.")
    ap.add_argument("--experiment", type=Path, default=DEFAULT_EXP,
                    help=f"experiment dir to compare (default: {DEFAULT_EXP.name})")
    ap.add_argument("--output", type=Path,
                    default=REPO / "experiments" / "COMPARISON.md")
    ap.add_argument("--sort", choices=("disagreement", "date", "match_id"),
                    default="disagreement",
                    help="row sort key (default: disagreement — biggest disagreement first)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    art = args.experiment / "hybrid.ubj"
    meta = args.experiment / "hybrid.meta.json"
    if not art.exists() or not meta.exists():
        print(f"error: missing artifact or meta under {args.experiment}", file=sys.stderr)
        return 1
    booster = hy._load_booster(artifact_path=art, meta_path=meta)
    if booster is None:
        print(f"error: could not load booster from {args.experiment} "
              "(xgboost not installed? feature_names mismatch?)", file=sys.stderr)
        return 1
    cal_method = (booster.calibrator.method if booster.calibrator else "none")

    model = pr.load_ratings()
    fixtures = _fixture_rows(REPO / "data" / "fixtures.csv")

    rows = []
    for row in fixtures:
        mid = row["match_id"].strip()
        a, b = row["team_a"].strip(), row["team_b"].strip()
        hfa = _hfa_for(a, b, row.get("country", ""))
        s_triple, h_triple = predict_pair(model, booster, a, b, hfa)
        delta = _signed_delta_triple(s_triple, h_triple)
        oc_idx, score_str = _outcome_label(row.get("score_a"), row.get("score_b"))
        call = _score_call(s_triple, h_triple, oc_idx)
        rows.append({
            "match_id": mid, "date_et": row["date_et"],
            "team_a": a, "team_b": b, "hfa": hfa,
            "s": s_triple, "h": h_triple, "delta": delta,
            "max_abs_delta": _max_abs_delta(delta),
            "score": score_str, "call": call,
            "status": row["status"].strip(),
        })

    # Sort
    if args.sort == "disagreement":
        rows.sort(key=lambda r: -r["max_abs_delta"])
    elif args.sort == "date":
        rows.sort(key=lambda r: (r["date_et"], r["match_id"]))
    else:
        rows.sort(key=lambda r: r["match_id"])

    # Summary stats
    avg_d = sum(r["max_abs_delta"] for r in rows) / len(rows)
    big_disagreements = sum(1 for r in rows if r["max_abs_delta"] >= 0.05)
    played = [r for r in rows if r["status"] == "played"]
    h_won = sum(1 for r in played if r["call"] == "**H**")
    s_won = sum(1 for r in played if r["call"] == "S")
    ties = sum(1 for r in played if r["call"] == "tie")

    lines = [
        "# Structural vs hybrid — per-game W/D/L side by side",
        "",
        f"_Experiment: `{args.experiment.name}` (calibration: {cal_method})._  ",
        f"_Generated against `data/fixtures.csv` ({len(rows)} matches, "
        f"{len(played)} played). Sort: `{args.sort}`._",
        "",
        "## At a glance",
        "",
        f"- **Mean disagreement** (max |hybrid − structural| across W/D/L): "
        f"**{avg_d*100:.1f}pp** — the average game sees the hybrid shift one of "
        f"the three probabilities by about {avg_d*100:.1f} points.",
        f"- **Big disagreements** (≥ 5pp on any class): **{big_disagreements} of {len(rows)} games**.",
    ]
    if played:
        lines.append(f"- **Head-to-head on played matches** (which model's RPS was lower): "
                     f"hybrid won **{h_won}**, structural won **{s_won}**, ties **{ties}** "
                     f"(of {len(played)}).")
    lines += [
        "",
        "## Reading the table",
        "",
        "- **`struct W/D/L`** and **`hybrid W/D/L`** are rounded percentages "
        "(`team_a / draw / team_b`). Sums may be 99 or 101 due to rounding.",
        "- **`Δ W/D/L`** is signed `hybrid − structural` in percentage points. "
        "`+` means hybrid is more bullish on that class than structural; `−` means less.",
        "- **`RPS winner`** (played matches only): **H** = hybrid had lower RPS on "
        "this outcome (closer to truth); **S** = structural was closer; `tie` "
        "= within 1e-6.",
        "- 🏠 next to a team name means that team got the host home-field bonus.",
        "",
        "## Per-game comparison",
        "",
        "| # | date | match | struct W/D/L | hybrid W/D/L | Δ (pp) | result | RPS winner |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        a_tag = f"🏠 {r['team_a']}" if r["hfa"] == r["team_a"] else r["team_a"]
        b_tag = f"🏠 {r['team_b']}" if r["hfa"] == r["team_b"] else r["team_b"]
        teams = f"{a_tag} vs {b_tag}"
        delta_str = " / ".join(f"{d*100:+.0f}" for d in r["delta"])
        result = r["score"] or "_unplayed_"
        lines.append(f"| {r['match_id']} | {r['date_et']} | {teams} | "
                     f"{_pct_triple(*r['s'])} | {_pct_triple(*r['h'])} | "
                     f"{delta_str} | {result} | {r['call']} |")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.output.relative_to(REPO)} ({len(rows)} matches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
