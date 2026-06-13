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
            "p": p, "outcome": o, "brier": brier(p, o),
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
    hits = sum(1 for g in graded.values() if g["correct"])
    return (f"Ledger to date: {n} graded prediction{'s' if n != 1 else ''}, "
            f"{hits} correct call{'s' if hits != 1 else ''}, "
            f"cumulative Brier {mean_b:.3f} (0 = clairvoyant, 0.667 = coin-flip baseline, 2 = max)")


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
    overlay = pr.load_match_overlay()
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


# ---------------------------------------------------------------- CLI

def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prediction ledger: log a slate or report Brier.")
    ap.add_argument("command", choices=["log", "report"])
    ap.add_argument("date", nargs="?", help="editorial date YYYY-MM-DD (for `log`)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if args.command == "log":
        if not args.date:
            print("error: `log` needs an editorial date (YYYY-MM-DD)", file=sys.stderr)
            return 2
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"error: bad date {args.date!r}", file=sys.stderr)
            return 2
        lines = log_slate(target, args.fixtures, args.ledger)
        for line in lines:
            print(line)
        missed = [l for l in lines if "MISSED" in l]
        if missed:
            print(f"::error::{len(missed)} slate prediction(s) not logged before "
                  "kickoff — run the morning pipeline earlier", file=sys.stderr)
            return 1
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
