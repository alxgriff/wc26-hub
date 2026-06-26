#!/usr/bin/env python3
"""WC26 prediction ledger — the accountability trail (PLAN.md Phase 4).

Every published prediction is logged to ``data/predictions_log.csv``:

    match_id, source, p_home, p_draw, p_away, predicted_score, timestamp

``source`` extends the CLAUDE.md minimum schema so per-source Brier can
accumulate: "consensus" is THE published, accountable call (equal-weight
average per CLAUDE.md); "model" and "opta" rows are its components, logged for
diagnostics (they are what will eventually justify non-equal weights with data
rather than opinion). p_home refers to the fixtures row's team_a.

Integrity rules (enforced, with tests):
  * Upsert keyed on (match_id, source): re-running a build never double-logs.
  * A prediction is REFUSED if the match's kickoff has already passed, and a
    logged row is IMMUTABLE once the match is played — no post-hoc predictions,
    no quiet revisions of graded calls. Pre-kickoff revisions are legitimate
    (Opta re-runs sims on team news) and update the row + timestamp.

Brier score (CLAUDE.md): the multiclass form — the SUM of squared errors of the
W/D/L probability vector against the 1/0/0 outcome vector. A certain correct
call scores 0; a certain wrong call scores 2; the uniform (⅓,⅓,⅓) scores ⅔.

Importable API:
    rows = load_ledger(path)
    rows, changed = upsert_prediction(rows, row, played_ids, kickoff_passed)
    b    = brier((p_h, p_d, p_a), outcome_index)
    graded = grade(matches, rows)          # per-match Brier for played matches
CLI:
    python scripts/ledger.py log 2026-06-12     # log today's slate (model+overlay)
    python scripts/ledger.py report             # per-day + cumulative Brier
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st      # noqa: E402
import build_edition as be  # noqa: E402  (editorial-date slate selection)
import predict as pr        # noqa: E402
import bracket as bk        # noqa: E402  (resolve knockout matchups for the advance ledger)
import knockout as ko       # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO_ROOT / "data" / "predictions_log.csv"
COLUMNS = ["match_id", "source", "p_home", "p_draw", "p_away",
           "predicted_score", "timestamp"]
ET = timezone(timedelta(hours=-4))   # Eastern Daylight Time (June)
PUBLISHED_SOURCE = "consensus"


def now_et() -> datetime:
    return datetime.now(tz=timezone.utc).astimezone(ET)


# ---------------------------------------------------------------- scoring

def outcome_index(score_a: int, score_b: int) -> int:
    """0 = team_a (home-slot) win, 1 = draw, 2 = team_b win."""
    if score_a > score_b:
        return 0
    if score_a == score_b:
        return 1
    return 2


def brier(p: tuple, outcome: int) -> float:
    """Multiclass Brier: sum of squared errors vs the 1/0/0 outcome vector."""
    return sum((p[i] - (1.0 if i == outcome else 0.0)) ** 2 for i in range(3))


def rps(p: tuple, outcome: int) -> float:
    """Ranked Probability Score for ORDERED outcomes. The order is the GOAL-MARGIN
    axis — team_a win (GD>0) → draw (GD=0) → team_b win (GD<0) — NOT home advantage,
    so it is fully meaningful for the neutral-venue WC group games: a draw is the
    middle outcome on margin regardless of venue. ('home/away' here just label
    team_a/team_b.) Reported ALONGSIDE Brier, not instead of it (Brier is the
    CLAUDE.md contract). Unlike Brier, RPS uses the ordering: when wrong, missing
    toward the DRAW (the middle) costs less than flipping to the opposite team's
    win — which is why the literature (Constantinou & Fenton 2012) prefers it for
    1X2, and it matters here given the ~25-30% group-stage draw rate. Range 0
    (perfect) to 1 (all mass on the opposite extreme); lower is better.
    outcome: 0=team_a win, 1=draw, 2=team_b win."""
    cum_p = (p[0], p[0] + p[1])                       # cumulative predicted (3rd term is 1≡1)
    cum_o = ((1.0, 1.0), (0.0, 1.0), (0.0, 0.0))[outcome]
    return 0.5 * sum((cum_p[i] - cum_o[i]) ** 2 for i in range(2))


def probs_valid(probs) -> bool:
    """True iff a W/D/L triple is finite, each in [0, 1], and sums to 1.0±0.001 —
    the CLAUDE.md probability contract. The single gate shared by the site's
    rendered call and the consensus that drives recorded bets, so the two cannot
    drift. Accepts strings or numbers; any parse failure is invalid."""
    try:
        p = [float(x) for x in probs]
    except (TypeError, ValueError):
        return False
    return (len(p) == 3
            and all(math.isfinite(x) and 0.0 <= x <= 1.0 for x in p)
            and abs(sum(p) - 1.0) <= 0.001)


# ---------------------------------------------------------------- ledger I/O

def load_ledger(path: str | Path = LEDGER_PATH) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_ledger(rows: list[dict], path: str | Path = LEDGER_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in COLUMNS} for r in rows)


class LedgerError(ValueError):
    """An integrity violation: post-kickoff logging or editing a graded row."""


def upsert_prediction(rows: list[dict], row: dict, played_ids: set,
                      kickoff_passed: bool) -> tuple[list[dict], bool]:
    """Insert or update the (match_id, source) row. Returns (rows, changed).

    Refuses to create OR modify a row for a played match, and refuses to create
    a new prediction after kickoff. Identical re-logs are no-ops (idempotent).
    """
    mid, src = row["match_id"], row["source"]
    existing = next((r for r in rows if r["match_id"] == mid and r["source"] == src), None)

    if mid in played_ids:
        raise LedgerError(f"{mid}: match already played — logged predictions are immutable "
                          "and new ones cannot be added")
    if existing is None and kickoff_passed:
        raise LedgerError(f"{mid}: kickoff has passed — refusing to log a post-hoc prediction")

    if existing is not None:
        same = all(existing.get(k) == row.get(k)
                   for k in ("p_home", "p_draw", "p_away", "predicted_score"))
        if same:
            return rows, False
        if kickoff_passed:
            raise LedgerError(f"{mid}: kickoff has passed — refusing to revise the "
                              "logged prediction")
        existing.update(row)
        return rows, True

    return rows + [row], True


# ---------------------------------------------------------------- grading

def grade(matches: list, ledger_rows: list[dict],
          source: str = PUBLISHED_SOURCE) -> dict:
    """Per-match Brier for played matches that have a logged `source` row.
    Returns {match_id: {"p": (...), "outcome": int, "brier": float,
                        "correct": bool, "predicted_score": str}}."""
    by_mid = {r["match_id"]: r for r in ledger_rows if r["source"] == source}
    out = {}
    for m in matches:
        if not m.is_played or m.match_id not in by_mid:
            continue
        r = by_mid[m.match_id]
        p = (float(r["p_home"]), float(r["p_draw"]), float(r["p_away"]))
        o = outcome_index(m.score_a, m.score_b)
        out[m.match_id] = {
            "p": p, "outcome": o, "brier": brier(p, o), "rps": rps(p, o),
            "correct": max(range(3), key=lambda i: p[i]) == o,
            "predicted_score": r.get("predicted_score", ""),
        }
    return out


def cumulative_line(matches: list, ledger_rows: list[dict]) -> str | None:
    """One-line running ledger summary across all graded consensus predictions."""
    graded = grade(matches, ledger_rows)
    if not graded:
        return None
    n = len(graded)
    mean_b = sum(g["brier"] for g in graded.values()) / n
    mean_r = sum(g["rps"] for g in graded.values()) / n
    hits = sum(1 for g in graded.values() if g["correct"])
    return (f"Ledger to date: {n} graded prediction{'s' if n != 1 else ''}, "
            f"{hits} correct call{'s' if hits != 1 else ''}, "
            f"cumulative Brier {mean_b:.3f} (0 = clairvoyant, 0.667 = coin-flip baseline, 2 = max), "
            f"RPS {mean_r:.3f} (0 = perfect, 1 = worst — rewards near-misses on the W-D-L order)")


# ---------------------------------------------------------------- logging a slate

def kickoff_dt(row: dict) -> datetime:
    """Kickoff as an ET datetime from a fixtures CSV row (date_et is the actual
    ET calendar date — already correct for 🌙 games)."""
    d = date.fromisoformat(row["date_et"].strip())
    h, mnt = (int(x) for x in row["kickoff_et_24h"].strip().split(":"))
    return datetime(d.year, d.month, d.day, h, mnt, tzinfo=ET)


def log_slate(editorial_date: date, fixtures_path: Path,
              ledger_path: Path = LEDGER_PATH, now: datetime | None = None) -> list[str]:
    """Log consensus + component predictions for every loggable match on the
    editorial date's slate. Returns human-readable status lines."""
    now = now or now_et()
    rows = be.read_rows(fixtures_path)
    slate = be.select_matches(rows, editorial_date)
    if not slate:
        return [f"no matches on editorial date {editorial_date}"]

    model = pr.load_ratings(fixtures=fixtures_path)
    overlay = pr.load_match_overlay(known_ids={r["match_id"] for r in rows})
    ledger = load_ledger(ledger_path)
    played = {r["match_id"] for r in rows if (r.get("status") or "").strip() == "played"}
    stamp = now.isoformat(timespec="seconds")
    lines = []

    for f_row in slate:
        mid, team_a, team_b = f_row["match_id"], f_row["team_a"], f_row["team_b"]
        passed = now >= kickoff_dt(f_row)
        host = pr.HOST_BY_COUNTRY.get((f_row.get("country") or "").strip())
        hfa = host if host in (team_a, team_b) else None
        pred = pr.predict_match(model, team_a, team_b, hfa_team=hfa)
        score = f"{pred.modal_score[0]}-{pred.modal_score[1]}"
        ov = overlay.get(mid)
        if ov and abs((pred.p_a - pred.p_b) - (ov["p_home"] - ov["p_away"])) > 0.40:
            lines.append(f"{mid}: WARNING overlay and model disagree sharply on the "
                         "home-vs-away lean — check the overlay isn't reversed (p_home/p_away)")

        to_log = [("model", (pred.p_a, pred.p_draw, pred.p_b), score)]
        if ov:
            to_log.append(("opta", (ov["p_home"], ov["p_draw"], ov["p_away"]), ""))
            to_log.append((PUBLISHED_SOURCE, pr.blend_wdl(pred, ov), score))
        else:
            # with a single source, the published call IS the model
            to_log.append((PUBLISHED_SOURCE, (pred.p_a, pred.p_draw, pred.p_b), score))

        logged = 0
        try:
            for src, p, sc in to_log:
                row = {"match_id": mid, "source": src,
                       "p_home": f"{p[0]:.4f}", "p_draw": f"{p[1]:.4f}",
                       "p_away": f"{p[2]:.4f}", "predicted_score": sc,
                       "timestamp": stamp}
                ledger, changed = upsert_prediction(ledger, row, played, passed)
                logged += changed
            lines.append(f"{mid} {team_a} vs {team_b}: "
                         + (f"logged {logged} row(s)" if logged else "already logged (unchanged)"))
        except LedgerError as e:
            # A slate match we failed to log BEFORE kickoff is a permanent hole
            # in the accountability ledger (immutable: no backfill). Mark it
            # MISSED so the CLI exits non-zero and the daily health gate goes red.
            lines.append(f"{mid}: MISSED — prediction not logged before kickoff ({e})")

    save_ledger(ledger, ledger_path)
    return lines


# ---------------------------------------------------------------- knockout advance ledger
#
# Single-elimination ties are 2-way (advance / out) — the group ledger above is UNCHANGED.
# This is a parallel, knockout-only accountability trail keyed on the FIFA match number,
# logging the model's pre-kickoff ADVANCE call (predict.resolve_knockout: 90' + extra time
# + coin-flip shootout) and grading it against knockout.csv's authoritative winner side
# with a 2-class Brier. Same leakage discipline: log strictly pre-kickoff, immutable once
# played, never double-log.

KO_LEDGER_PATH = REPO_ROOT / "data" / "ko_predictions_log.csv"
KO_COLUMNS = ["match_no", "team_a", "team_b", "p_advance_a", "p_advance_b", "timestamp"]


def brier2(p: tuple, outcome: int) -> float:
    """Two-class Brier for an advance call: sum of squared errors of the 2-vector
    (p_advance_a, p_advance_b) against the realized advance vector. Certain-correct 0;
    certain-wrong 2; an even (0.5, 0.5) call on a binary outcome scores 0.5."""
    return sum((p[i] - (1.0 if i == outcome else 0.0)) ** 2 for i in range(2))


def probs_valid2(probs) -> bool:
    """True iff an advance pair is finite, each in [0, 1], and sums to 1.0±0.001."""
    try:
        p = [float(x) for x in probs]
    except (TypeError, ValueError):
        return False
    return (len(p) == 2
            and all(math.isfinite(x) and 0.0 <= x <= 1.0 for x in p)
            and abs(sum(p) - 1.0) <= 0.001)


def load_ko_ledger(path: str | Path = KO_LEDGER_PATH) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_ko_ledger(rows: list[dict], path: str | Path = KO_LEDGER_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=KO_COLUMNS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in KO_COLUMNS} for r in rows)


def upsert_ko_prediction(rows: list[dict], row: dict, played_nos: set,
                         kickoff_passed: bool) -> tuple[list[dict], bool]:
    """Insert/update the advance call for a knockout match (keyed on match_no). Same
    integrity as upsert_prediction: refuse to create OR revise a row once the tie is
    played; refuse a new row after kickoff; identical re-logs are idempotent no-ops."""
    no = str(row["match_no"])
    existing = next((r for r in rows if str(r.get("match_no")) == no), None)
    if no in played_nos:
        raise LedgerError(f"M{no}: tie already played — advance calls are immutable")
    if existing is None and kickoff_passed:
        raise LedgerError(f"M{no}: kickoff has passed — refusing a post-hoc advance call")
    if existing is not None:
        if all(existing.get(k) == row.get(k) for k in ("p_advance_a", "p_advance_b")):
            return rows, False
        if kickoff_passed:
            raise LedgerError(f"M{no}: kickoff has passed — refusing to revise the advance call")
        existing.update(row)
        return rows, True
    return rows + [row], True


def ko_kickoff_dt(km) -> datetime:
    """Kickoff as an ET datetime from a KnockoutMatch (date_et + kickoff_et_24h)."""
    d = date.fromisoformat(km.date_et.strip())
    h, mnt = (int(x) for x in km.kickoff_et_24h.strip().split(":"))
    return datetime(d.year, d.month, d.day, h, mnt, tzinfo=ET)


def ko_outcome_index(km) -> int | None:
    """0 if the listed-first side (team_a) advanced, 1 if team_b — from knockout.csv's
    authoritative `winner` side (penalty wins included). None if not played."""
    if not km.is_played or km.winner not in ("A", "B"):
        return None
    return 0 if km.winner == "A" else 1


def log_ko_slate(editorial_date: date,
                 fixtures_path: Path = REPO_ROOT / "data" / "fixtures.csv",
                 knockout_path: Path = KO_LEDGER_PATH.parent / "knockout.csv",
                 ledger_path: Path = KO_LEDGER_PATH,
                 now: datetime | None = None) -> list[str]:
    """Log the model's advance call for every RESOLVED knockout tie on the editorial
    date, strictly pre-kickoff. Matchups are materialized from the current standings +
    any played knockout results; abstract/unresolved ties are skipped (nothing to call)."""
    now = now or now_et()
    matches = ko.load_knockout(knockout_path)
    if not matches:
        return ["no knockout schedule yet — nothing to log"]
    fixtures = st.load_fixtures(fixtures_path)
    standings = st.compute_standings(fixtures, fair_play=st.load_discipline())
    matches = ko.materialize_teams(bk.project(standings), matches)
    today = [km for km in matches if km.date_et == editorial_date.isoformat()]
    if not today:
        return [f"no knockout ties on {editorial_date}"]

    model = pr.load_ratings(fixtures=fixtures_path)
    rows = load_ko_ledger(ledger_path)
    played_nos = {str(km.match_no) for km in matches if km.is_played}
    stamp = now.isoformat(timespec="seconds")
    lines: list[str] = []
    for km in today:
        if not km.participants_known:
            lines.append(f"M{km.match_no}: matchup not set — no advance call to log")
            continue
        passed = now >= ko_kickoff_dt(km)
        kp = pr.resolve_knockout(model, km.team_a, km.team_b)   # neutral venue (no KO HFA)
        row = {"match_no": str(km.match_no), "team_a": km.team_a, "team_b": km.team_b,
               "p_advance_a": f"{kp.p_advance_a:.4f}", "p_advance_b": f"{kp.p_advance_b:.4f}",
               "timestamp": stamp}
        try:
            rows, changed = upsert_ko_prediction(rows, row, played_nos, passed)
            lines.append(f"M{km.match_no} {km.team_a} vs {km.team_b}: "
                         + ("logged advance call" if changed else "already logged (unchanged)"))
        except LedgerError as e:
            lines.append(f"M{km.match_no}: MISSED — advance call not logged before kickoff ({e})")

    save_ko_ledger(rows, ledger_path)
    return lines


def grade_ko(knockout_matches: list, ledger_rows: list[dict]) -> dict:
    """Per-tie 2-class Brier for played, logged knockout ties.
    {match_no: {"p": (pa, pb), "outcome": int, "brier": float, "correct": bool,
                "advancer": team}}."""
    by_no = {str(r.get("match_no")): r for r in ledger_rows}
    out = {}
    for km in knockout_matches:
        o = ko_outcome_index(km)
        key = str(km.match_no)
        if o is None or key not in by_no:
            continue
        r = by_no[key]
        try:
            p = (float(r["p_advance_a"]), float(r["p_advance_b"]))
        except (KeyError, ValueError):
            continue
        out[km.match_no] = {"p": p, "outcome": o, "brier": brier2(p, o),
                            "correct": (p[0] >= p[1]) == (o == 0),
                            "advancer": km.winner_team}
    return out


def ko_cumulative_line(knockout_matches: list, ledger_rows: list[dict]) -> str | None:
    """One-line running summary across all graded knockout advance calls."""
    graded = grade_ko(knockout_matches, ledger_rows)
    if not graded:
        return None
    n = len(graded)
    mean_b = sum(g["brier"] for g in graded.values()) / n
    hits = sum(1 for g in graded.values() if g["correct"])
    return (f"Knockout advance calls: {n} graded, {hits} correct, "
            f"cumulative 2-class Brier {mean_b:.3f} "
            f"(0 = clairvoyant, 0.5 = coin-flip baseline, 2 = max)")


# ---------------------------------------------------------------- CLI

def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prediction ledger: log a slate or report Brier.")
    ap.add_argument("command", choices=["log", "report", "log-ko", "report-ko"])
    ap.add_argument("date", nargs="?", help="editorial date YYYY-MM-DD (for `log`/`log-ko`)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--ko-ledger", type=Path, default=KO_LEDGER_PATH)
    ap.add_argument("--knockout", type=Path, default=REPO_ROOT / "data" / "knockout.csv")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if args.command in ("log", "log-ko"):
        if not args.date:
            print(f"error: `{args.command}` needs an editorial date (YYYY-MM-DD)", file=sys.stderr)
            return 2
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"error: bad date {args.date!r}", file=sys.stderr)
            return 2
        if args.command == "log":
            lines = log_slate(target, args.fixtures, args.ledger)
            miss_word = "slate prediction"
        else:
            lines = log_ko_slate(target, args.fixtures, args.knockout, args.ko_ledger)
            miss_word = "advance call"
        for line in lines:
            print(line)
        missed = [l for l in lines if "MISSED" in l]
        if missed:
            print(f"::error::{len(missed)} {miss_word}(s) not logged before "
                  "kickoff — run the morning pipeline earlier", file=sys.stderr)
            return 1
        return 0

    if args.command == "report-ko":
        matches = ko.load_knockout(args.knockout)
        graded = grade_ko(matches, load_ko_ledger(args.ko_ledger))
        if not graded:
            print("no graded knockout advance calls yet")
            return 0
        print(f"{'tie':5s} {'advance call (A/B)':20s} {'advanced':9s} {'Brier':>6s}  ok")
        for no, g in sorted(graded.items()):
            p = "/".join(f"{x:.0%}" for x in g["p"])
            print(f"M{no:<4d} {p:20s} {['A', 'B'][g['outcome']]:9s} "
                  f"{g['brier']:6.3f}  {'✓' if g['correct'] else '✗'}")
        print()
        print(ko_cumulative_line(matches, load_ko_ledger(args.ko_ledger)))
        return 0

    matches = st.load_fixtures(args.fixtures)
    ledger = load_ledger(args.ledger)
    graded = grade(matches, ledger)
    if not graded:
        print("no graded predictions yet")
        return 0
    print(f"{'match':8s} {'call (H/D/A)':20s} {'outcome':9s} {'Brier':>6s}  ok")
    for mid, g in sorted(graded.items()):
        p = "/".join(f"{x:.0%}" for x in g["p"])
        oc = ["home", "draw", "away"][g["outcome"]]
        print(f"{mid:8s} {p:20s} {oc:9s} {g['brier']:6.3f}  {'✓' if g['correct'] else '✗'}")
    print()
    print(cumulative_line(matches, ledger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
