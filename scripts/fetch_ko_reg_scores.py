#!/usr/bin/env python3
"""Fetch the 90-minute (regulation) score for played WC knockout ties from ESPN's free
`fifa.world` feed and write it to `data/knockout.csv` (score_a_reg/score_b_reg).

WHY: the knockout 90' bet markets — totals (O/U), Asian handicap, BTTS — settle on
**90 minutes** (regulation + stoppage), not extra time. `fetch_ko_results.py` enters the
90'+ET **aggregate** (for advancement); this fills the separate regulation score so those
bets settle correctly. Only a tie that reached **extra time / penalties** needs it — a game
decided in regulation has its 90' score == final (derived, no fetch).

Source: ESPN's keyless hidden API. The 90' score is the sum of the first two half
line-scores per team: `…competitors[i].linescores[0..1]` (extra time / shootout add later
entries). No key, no quota; an unofficial endpoint, so it's a SECONDARY feed — fail-soft,
canon-joined on team names (predict.ALIAS already maps ESPN's 'Congo DR' / 'Bosnia-
Herzegovina' / 'Ivory Coast'), never guessed.

CLI:  python scripts/fetch_ko_reg_scores.py [--knockout data/knockout.csv] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as pr           # noqa: E402  (canon team-name normalization)
import knockout as ko          # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
SCOREBOARD_URL = _BASE + "/scoreboard?dates={d}"     # by date -> event ids (no line-scores)
SUMMARY_URL = _BASE + "/summary?event={id}"          # by event -> per-half line-scores


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wc26-hub/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _ls_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def extract_reg_score(competition: dict, team_a: str, team_b: str) -> tuple[int, int] | None:
    """The 90-minute (regulation) score for the tie ``team_a`` vs ``team_b`` from an ESPN
    *summary* competition dict (`summary.header.competitions[0]`), as (reg_a, reg_b) — or None
    if it isn't this tie, isn't completed, or the line-scores can't be read. Regulation = the
    first two half line-scores summed; any extra-time / shootout entries that follow are
    excluded. Teams are canon-normalised (predict._canon) so ESPN spellings join the
    knockout.csv names (predict.ALIAS maps 'Congo DR' / 'Bosnia-Herzegovina' / 'Ivory Coast')."""
    status = ((competition.get("status") or {}).get("type") or {})
    if not status.get("completed"):
        return None
    by_team: dict[str, int | None] = {}
    for c in competition.get("competitors", []):
        name = pr._canon(((c.get("team") or {}).get("displayName") or "").strip())
        ls = c.get("linescores") or []
        if len(ls) >= 2:
            a = _ls_int(ls[0].get("value", ls[0].get("displayValue")))
            b = _ls_int(ls[1].get("value", ls[1].get("displayValue")))
            by_team[name] = (a + b) if (a is not None and b is not None) else None
        else:                                   # no half split -> regulation final (defensive)
            by_team[name] = _ls_int(c.get("score"))
    if (by_team.get(team_a) is not None) and (by_team.get(team_b) is not None):
        return by_team[team_a], by_team[team_b]
    return None


def _scoreboard_event_id(events: list, team_a: str, team_b: str) -> str | None:
    """The ESPN event id whose two competitors canon-match the tie, else None."""
    want = {team_a, team_b}
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        names = {pr._canon(((c.get("team") or {}).get("displayName") or "").strip())
                 for c in comp.get("competitors", [])}
        if names == want:
            return str(ev.get("id")) if ev.get("id") is not None else None
    return None


def apply_reg_scores(matches: list, opener=_default_opener) -> tuple[list, list[str]]:
    """Fetch + set the regulation score for every played tie that NEEDS one (reached extra
    time / penalties, no reg score yet). Two-step per tie: the scoreboard (by date) gives the
    event id, the summary (by event) gives the per-half line-scores. Returns (updated_matches,
    status_lines). Each date is fetched once (plus the next day, since an evening kickoff can
    file under the next UTC date); a tie ESPN hasn't completed/published yet is reported and
    left for the next run."""
    need = [km for km in matches
            if km.is_played and km.participants_known and km.reg_score is None]
    if not need:
        return matches, ["no knockout ties need a regulation score"]

    dates: set[date] = set()
    for km in need:
        try:
            d = date.fromisoformat(km.date_et)
        except ValueError:
            continue
        dates.add(d)
        dates.add(d + timedelta(days=1))        # evening kickoff can land on the next UTC day

    lines: list[str] = []
    events: list[dict] = []
    for d in sorted(dates):
        try:
            payload = json.loads(opener(SCOREBOARD_URL.format(d=d.strftime("%Y%m%d"))))
            events.extend(payload.get("events", []))
        except (urllib.error.URLError, ValueError, KeyError) as e:
            lines.append(f"ESPN scoreboard {d}: fetch failed ({e.__class__.__name__})")

    updated = matches
    changed = 0
    for km in need:
        eid = _scoreboard_event_id(events, km.team_a, km.team_b)
        if eid is None:
            lines.append(f"M{km.match_no}: ESPN event not found yet for "
                         f"{km.team_a} vs {km.team_b} — regulation score not set")
            continue
        try:
            summary = json.loads(opener(SUMMARY_URL.format(id=eid)))
        except (urllib.error.URLError, ValueError) as e:
            lines.append(f"M{km.match_no}: ESPN summary fetch failed ({e.__class__.__name__})")
            continue
        comps = (summary.get("header") or {}).get("competitions") or [{}]
        reg = extract_reg_score(comps[0], km.team_a, km.team_b)
        if reg is None:
            lines.append(f"M{km.match_no}: ESPN line-scores not available yet "
                         "— regulation score not set")
            continue
        try:
            updated, msg = ko.set_reg_score(updated, km.match_no, reg[0], reg[1])
            lines.append(msg)
            changed += 1
        except ValueError as e:
            lines.append(f"M{km.match_no}: regulation score rejected — {e}")
    lines.append(f"{changed} regulation score(s) set")
    return updated, lines


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fetch 90-minute (regulation) knockout scores from ESPN into knockout.csv.")
    ap.add_argument("--knockout", type=Path, default=REPO_ROOT / "data" / "knockout.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.knockout.exists():
        print(f"no knockout schedule at {args.knockout} — nothing to do.")
        return 0
    matches = ko.load_knockout(args.knockout)
    updated, lines = apply_reg_scores(matches)
    for ln in lines:
        print(ln)
    if not args.dry_run and any(u is not o for u, o in zip(updated, matches)):
        ko.write_knockout(args.knockout, updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
