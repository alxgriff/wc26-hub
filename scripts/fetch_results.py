#!/usr/bin/env python3
"""Pull completed scores from The Odds API and enter them into fixtures.csv.

Reuses the odds-engine plumbing (API client, key resolution, canon team-name
matching) and the result-entry contract (apply_result refuses to overwrite a
played match). Per the CLAUDE.md join contract, events whose teams don't map
to the canon are REPORTED and skipped, never fuzzy-matched; scores are never
invented — a match the API hasn't settled stays "scheduled" and the edition
flags it loudly.

CLI:
    python scripts/fetch_results.py [--days-from 2] [--fixtures ...] [--dry-run]
Exit codes: 0 = ran (possibly with skips, reported on stdout); 1 = hard error
(no API key, API unreachable).
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds as od              # noqa: E402  (API client + canon event matching)
import enter_result as er      # noqa: E402  (contract-safe CSV mutation)
import build_edition as be     # noqa: E402
import predict as pr           # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def apply_completed(events: list, fixtures_path: Path,
                    dry_run: bool = False) -> list[str]:
    """Enter every completed, canon-matched event's score. Returns status lines."""
    rows = be.read_rows(fixtures_path)
    lines = er._read_lines(fixtures_path)
    status: list[str] = []
    changed = 0

    for ev in events:
        if not ev.get("completed"):
            continue
        fr = od._match_event(ev, rows)
        if fr is None:
            status.append(f"UNMATCHED completed event: {ev.get('home_team')!r} vs "
                          f"{ev.get('away_team')!r} — not in fixtures after canon "
                          "normalization; skipped (report, never fuzzy-match)")
            continue
        if (fr.get("status") or "").strip().lower() == "played":
            continue  # already entered

        scores = {pr._canon(s.get("name", "")): s.get("score")
                  for s in (ev.get("scores") or [])}
        sa, sb = scores.get(fr["team_a"]), scores.get(fr["team_b"])
        if sa is None or sb is None:
            status.append(f"{fr['match_id']}: completed but scores unmapped "
                          f"({list(scores)}) — left scheduled; enter manually")
            continue
        try:
            lines, msg = er.apply_result(lines, fr["match_id"], int(sa), int(sb))
        except (er.ResultError, ValueError) as e:
            status.append(f"{fr['match_id']}: NOT entered — {e}")
            continue
        status.append(msg)
        changed += 1

    if changed and not dry_run:
        er._write_lines(fixtures_path, lines)
    status.append(f"{changed} result(s) entered" + (" (dry run)" if dry_run else ""))
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Enter completed scores from The Odds API into fixtures.csv.")
    ap.add_argument("--days-from", type=int, default=2,
                    help="how many days back to ask the API for (default 2)")
    ap.add_argument("--sport", default=od.SPORT_KEY)
    ap.add_argument("--fixtures", type=Path,
                    default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    key = od._read_key()
    if not key:
        print("error: no API key. Set ODDS_API_KEY or write data/.odds_api_key.",
              file=sys.stderr)
        return 1
    try:
        events, remaining = od._api_get(f"/sports/{args.sport}/scores", key,
                                        daysFrom=args.days_from, dateFormat="iso")
    except (urllib.error.HTTPError, od.OddsError) as e:
        print(f"error: scores API: {e}", file=sys.stderr)
        return 1

    for line in apply_completed(events, args.fixtures, dry_run=args.dry_run):
        print(line)
    print(f"API requests remaining this month: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
