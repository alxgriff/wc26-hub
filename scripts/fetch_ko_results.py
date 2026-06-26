#!/usr/bin/env python3
"""Pull completed KNOCKOUT scores from The Odds API into data/knockout.csv.

Companion to fetch_results.py (group stage -> fixtures.csv). Knockout participants
resolve from group results, so this first materializes team_a/team_b
(knockout.materialize_teams), then maps each completed API event to a resolved tie by
canon team-set and enters DECISIVE results (winner = the higher score). A LEVEL result
was settled on penalties — the API score can't say who won the shootout, so it is
REPORTED for manual entry (`knockout.py --enter ...`), never guessed. The API also
doesn't flag extra time, so a decisive auto-entry is recorded as 'regulation' and can be
revised. Same join contract as fetch_results: unmatched events are skipped silently
(group events belong to the other fetcher); a knockout tie whose scores can't be mapped
is reported, never fuzzy-matched.

CLI: python scripts/fetch_ko_results.py [--days-from 3] [--dry-run]
Exit codes: 0 = ran (skips reported on stdout); 1 = hard error (no key / API down).
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds as od              # noqa: E402  (API client + key resolution)
import predict as pr           # noqa: E402  (canon team-name normalization)
import standings as st         # noqa: E402
import bracket as bk           # noqa: E402
import knockout as ko          # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _match_ko_event(ev: dict, matches: list) -> "ko.KnockoutMatch | None":
    """The resolved, not-yet-played knockout tie whose two teams are this event's teams
    (canon-normalized). Single-elimination => a given pair meets at most once, so the
    team-set is a unique key. Returns None for group events / unresolved ties."""
    want = {pr._canon(ev.get("home_team", "") or ""), pr._canon(ev.get("away_team", "") or "")}
    if "" in want:
        return None
    for km in matches:
        if km.participants_known and not km.is_played and {km.team_a, km.team_b} == want:
            return km
    return None


def apply_ko_completed(events: list, knockout_path: Path, fixtures_path: Path,
                       dry_run: bool = False) -> list[str]:
    """Enter every completed, canon-matched, DECISIVE knockout result; report the rest."""
    matches = ko.load_knockout(knockout_path)
    if not matches:
        return ["no knockout schedule found — nothing to do"]
    fixtures = st.load_fixtures(fixtures_path)
    standings = st.compute_standings(fixtures, fair_play=st.load_discipline())
    matches = ko.materialize_teams(bk.project(standings), matches)

    status: list[str] = []
    changed = 0
    for ev in events:
        if not ev.get("completed"):
            continue
        km = _match_ko_event(ev, matches)
        if km is None:
            continue                              # group event or unresolved tie — not ours
        scores = {pr._canon(s.get("name", "")): s.get("score")
                  for s in (ev.get("scores") or [])}
        sa, sb = scores.get(km.team_a), scores.get(km.team_b)
        if sa is None or sb is None:
            status.append(f"M{km.match_no}: completed but scores unmapped "
                          f"({list(scores)}) — enter manually")
            continue
        sa, sb = int(sa), int(sb)
        if sa == sb:
            status.append(
                f"M{km.match_no}: {km.team_a} {sa}–{sb} {km.team_b} level after play — "
                "settled on penalties; the shootout winner can't be read from the score. "
                f"Enter manually: knockout.py --enter {km.match_no} --score {sa}-{sb} "
                "--decided penalties --winner A|B")
            continue
        try:
            matches, msg = ko.enter_ko_result(matches, km.match_no, sa, sb)
        except ValueError as e:
            status.append(f"M{km.match_no}: NOT entered — {e}")
            continue
        status.append(msg)
        changed += 1

    if changed and not dry_run:
        ko.write_knockout(knockout_path, matches)
    status.append(f"{changed} knockout result(s) entered" + (" (dry run)" if dry_run else ""))
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Enter completed knockout scores from The Odds API into knockout.csv.")
    ap.add_argument("--days-from", type=int, default=3,
                    help="how many days back to ask the API for (default 3)")
    ap.add_argument("--sport", default=od.SPORT_KEY)
    ap.add_argument("--knockout", type=Path, default=REPO_ROOT / "data" / "knockout.csv")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.knockout.exists():
        print(f"no knockout schedule at {args.knockout} — group stage only; nothing to do.")
        return 0
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

    for line in apply_ko_completed(events, args.knockout, args.fixtures, dry_run=args.dry_run):
        print(line)
    print(f"API requests remaining this month: {remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
