#!/usr/bin/env python3
"""Pull completed KNOCKOUT scores into data/knockout.csv (The Odds API + ESPN).

Companion to fetch_results.py (group stage -> fixtures.csv). Knockout participants
resolve from group results, so this first materializes team_a/team_b
(knockout.materialize_teams), then maps each completed API event to a resolved tie by
canon team-set and enters DECISIVE results (winner = the higher score). A LEVEL result
was settled on penalties — the Odds API score can't say who won the shootout, so a
second, keyless ESPN pass (apply_espn_results) reads the shootout tally from
`competitors[].shootoutScore` (STATUS_FINAL_PEN) and enters decided_by='penalties' with
the shootout winner. The winner is still NEVER inferred from the 90'+ET score — it comes
from ESPN's explicit shootout result, and a level tie ESPN shows no tally for falls back
to the manual-entry report (`knockout.py --enter ...`), never guessed. The ESPN pass
also sweeps up any decisive tie the Odds API's --days-from window has already expired
past, and reads extra time from ESPN's status (AET) where flagged. Same join contract as
fetch_results: unmatched events are skipped silently (group events belong to the other
fetcher); a knockout tie whose scores can't be mapped is reported, never fuzzy-matched.

CLI: python scripts/fetch_ko_results.py [--days-from 3] [--dry-run]
Exit codes: 0 = ran (skips reported on stdout); 1 = hard error (no key / API down —
the keyless ESPN pass still runs first so results land even then).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds as od              # noqa: E402  (API client + key resolution)
import predict as pr           # noqa: E402  (canon team-name normalization)
import standings as st         # noqa: E402
import bracket as bk           # noqa: E402
import knockout as ko          # noqa: E402
import fetch_ko_reg_scores as espn  # noqa: E402  (keyless ESPN feed + canon extractors)

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


def apply_espn_results(matches: list, opener=None,
                       today: date | None = None) -> tuple[list, list[str]]:
    """Enter completed knockout results from ESPN's keyless scoreboard — the pass that CAN
    settle a penalty tie: ESPN publishes the shootout tally (`shootoutScore`), so a level
    game is entered decided_by='penalties' with the SHOOTOUT winner (never inferred from
    the 90'+ET score; a level tie ESPN shows no tally for is reported for manual entry).
    Decisive ties the Odds API already entered are skipped (is_played); ones its window
    expired past are entered here, with extra time read from ESPN's AET status where
    flagged. Only ties whose date_et has arrived are queried; each date is fetched once
    (plus the next UTC day, for evening kickoffs). Expects MATERIALIZED matches. Fail-soft
    throughout. Returns (updated_matches, status_lines)."""
    opener = opener or espn._default_opener
    today = today or datetime.now(timezone.utc).date()

    due, dates = [], set()
    for km in matches:
        if km.is_played or not km.participants_known:
            continue
        try:
            d = date.fromisoformat(km.date_et)
        except ValueError:
            continue
        if d > today:
            continue
        due.append(km)
        dates.add(d)
        dates.add(d + timedelta(days=1))        # evening kickoff can file under the next UTC day
    if not due:
        return matches, []

    lines: list[str] = []
    events: list[dict] = []
    for d in sorted(dates):
        try:
            payload = json.loads(opener(espn.SCOREBOARD_URL.format(d=d.strftime("%Y%m%d"))))
            events.extend(payload.get("events", []))
        except (urllib.error.URLError, ValueError, KeyError) as e:
            lines.append(f"ESPN scoreboard {d}: fetch failed ({e.__class__.__name__})")

    changed = 0
    for km in due:
        comp = espn.scoreboard_competition(events, km.team_a, km.team_b)
        res = espn.extract_scoreboard_result(comp, km.team_a, km.team_b) if comp else None
        if res is None:
            continue                            # not listed / not final yet — next run's job
        (sa, sb), so, sname = res["score"], res["shootout"], res["status_name"]
        if sa == sb:
            if so is None or so[0] == so[1]:
                lines.append(
                    f"M{km.match_no}: {km.team_a} {sa}–{sb} {km.team_b} level after play "
                    "but ESPN shows no shootout tally — enter manually: "
                    f"knockout.py --enter {km.match_no} --score {sa}-{sb} "
                    "--decided penalties --winner A|B")
                continue
            winner = "A" if so[0] > so[1] else "B"
            try:
                matches, msg = ko.enter_ko_result(matches, km.match_no, sa, sb,
                                                  decided_by="penalties", winner=winner)
            except ValueError as e:
                lines.append(f"M{km.match_no}: NOT entered — {e}")
                continue
            lines.append(f"{msg} — shootout {so[0]}–{so[1]} (ESPN)")
        else:
            decided = "extra_time" if "AET" in sname.upper() else ""
            try:
                matches, msg = ko.enter_ko_result(matches, km.match_no, sa, sb,
                                                  decided_by=decided)
            except ValueError as e:
                lines.append(f"M{km.match_no}: NOT entered — {e}")
                continue
            lines.append(f"{msg} (ESPN)")
        changed += 1
    lines.append(f"{changed} knockout result(s) entered from ESPN")
    return matches, lines


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

    # ESPN pass FIRST (keyless): shootout winners + Odds-API-window-expired ties. Running
    # it before the Odds API pass means a dead/keyless Odds API still lands results.
    matches = ko.load_knockout(args.knockout)
    if matches:
        fixtures = st.load_fixtures(args.fixtures)
        standings = st.compute_standings(fixtures, fair_play=st.load_discipline())
        materialized = ko.materialize_teams(bk.project(standings), matches)
        updated, lines = apply_espn_results(materialized)
        for line in lines:
            print(line)
        if not args.dry_run and any(u is not o for u, o in zip(updated, materialized)):
            ko.write_knockout(args.knockout, updated)

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
