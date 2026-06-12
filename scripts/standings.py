#!/usr/bin/env python3
"""WC26 group-stage standings engine.

Computes the 12 group tables and the cross-group third-place ranking from
played matches in data/fixtures.csv, applying the tournament tiebreakers
documented in CLAUDE.md:

  Group:        points, goal difference, goals scored, head-to-head,
                fair play points, drawing of lots
  Third place:  points, goal difference, goals scored, fair play, lots

Head-to-head is applied as a mini-table (points, GD, goals scored) over the
matches among the tied teams, and re-applied recursively to any subset that
stays tied, per FIFA procedure.

Importable API (for build_edition.py, scenarios.py, ...):

    matches   = load_fixtures(path)            # list[Match]
    standings = compute_standings(matches)     # Standings
    markdown  = render_markdown(standings)     # str

Fair play points cannot be derived from fixtures.csv (it carries no card
data); pass ``fair_play={"Team": points}`` to compute_standings when
available, using the FIFA convention that points are deductions — higher
(closer to zero) ranks first. Keys must exact-match the team names in the
fixtures (the CLAUDE.md canon); unknown names raise ValueError rather than
silently scoring 0. Ties that survive every criterion are shown
alphabetically and flagged in the table notes: drawing of lots cannot be
simulated, only reported.

CLI:
    python scripts/standings.py [--fixtures data/fixtures.csv]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

GROUP_LETTERS = "ABCDEFGHIJKL"
QUALIFYING_THIRDS = 8
GROUP_SIZE = 4
MATCHES_PER_GROUP = 6
GAMES_PER_TEAM = 3

_MATCH_ID_RE = re.compile(r"^([A-L])([1-6])$")
_REQUIRED_COLUMNS = (
    "match_id", "group", "matchday", "team_a", "team_b",
    "score_a", "score_b", "status",
)


@dataclass(frozen=True)
class Match:
    match_id: str
    group: str
    matchday: int
    team_a: str
    team_b: str
    score_a: int | None
    score_b: int | None
    status: str

    @property
    def is_played(self) -> bool:
        return self.status == "played"


@dataclass
class TeamRow:
    team: str
    group: str
    played: int = 0
    won: int = 0
    drawn: int = 0
    lost: int = 0
    gf: int = 0
    ga: int = 0

    @property
    def gd(self) -> int:
        return self.gf - self.ga

    @property
    def points(self) -> int:
        return 3 * self.won + self.drawn


@dataclass
class GroupTable:
    group: str
    rows: list[TeamRow]      # ranked best to worst
    notes: list[str]         # tiebreak explanations / unresolved-tie flags


@dataclass
class Standings:
    groups: dict[str, GroupTable]   # keyed by group letter
    third_place: list[TeamRow]      # ranked best to worst
    third_place_notes: list[str]
    played: int                     # matches played
    total: int                      # fixtures provided
    warnings: list[str]             # data-integrity warnings (stderr, not editorial)


# ---------------------------------------------------------------- loading

def load_fixtures(path: str | Path) -> list[Match]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in _REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing required column(s): {', '.join(missing)}")
        return parse_fixtures(reader)


def parse_fixtures(rows: Iterable[Mapping[str, str | None]]) -> list[Match]:
    matches: list[Match] = []
    seen: set[str] = set()
    for row in rows:
        mid = (row.get("match_id") or "").strip()
        m = _MATCH_ID_RE.match(mid)
        if not m:
            raise ValueError(f"bad match_id {mid!r} (expected group letter A–L + digit 1–6)")
        if mid in seen:
            raise ValueError(f"duplicate match_id {mid}")
        seen.add(mid)

        group = (row.get("group") or "").strip()
        if group != m.group(1):
            raise ValueError(f"{mid}: group column {group!r} does not match match_id")

        expected_md = (int(m.group(2)) + 1) // 2
        md_raw = (row.get("matchday") or "").strip()
        try:
            matchday = int(md_raw)
        except ValueError:
            raise ValueError(f"{mid}: matchday is not an integer: {md_raw!r}") from None
        if matchday != expected_md:
            raise ValueError(
                f"{mid}: matchday {matchday} inconsistent with match_id (expected {expected_md})")

        team_a = (row.get("team_a") or "").strip()
        team_b = (row.get("team_b") or "").strip()
        if not team_a or not team_b:
            raise ValueError(f"{mid}: missing team name(s)")
        if team_a == team_b:
            raise ValueError(f"{mid}: team_a and team_b are both {team_a!r}")

        status = (row.get("status") or "").strip().lower()
        if status not in ("scheduled", "played"):
            raise ValueError(f"{mid}: status must be 'scheduled' or 'played', got {row.get('status')!r}")
        score_a = _parse_score(row.get("score_a"), mid, "score_a")
        score_b = _parse_score(row.get("score_b"), mid, "score_b")
        if status == "played" and (score_a is None or score_b is None):
            raise ValueError(f"{mid}: status is 'played' but scores are incomplete")
        if status == "scheduled" and (score_a is not None or score_b is not None):
            raise ValueError(f"{mid}: status is 'scheduled' but scores are present")

        matches.append(Match(mid, group, matchday, team_a, team_b, score_a, score_b, status))
    return matches


def _parse_score(value: str | None, mid: str, col: str) -> int | None:
    value = (value or "").strip()
    if value == "":
        return None
    try:
        score = int(value)
    except ValueError:
        raise ValueError(f"{mid}: {col} is not an integer: {value!r}") from None
    if score < 0:
        raise ValueError(f"{mid}: {col} is negative")
    return score


# ---------------------------------------------------------------- fair play

DISCIPLINE = Path(__file__).resolve().parents[1] / "data" / "discipline.csv"
# FIFA fair play deductions per card type (higher total = better ranking)
FAIR_PLAY_POINTS = {"yellows": -1, "second_yellow_reds": -3,
                    "direct_reds": -4, "yellow_plus_reds": -5}


def load_discipline(path: str | Path = DISCIPLINE) -> dict[str, int]:
    """data/discipline.csv -> {team: fair play points} (FIFA deductions,
    negative; higher ranks first). The single source for every consumer
    (site, editions, scenarios, blurb) so tied clusters rank identically
    everywhere. Team-name validation happens in compute_standings, which
    raises on canon mismatches per the join contract."""
    path = Path(path)
    if not path.exists():
        return {}
    totals: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            team = (row.get("team") or "").strip()
            if not team:
                continue
            pts = sum(FAIR_PLAY_POINTS[k] * int((row.get(k) or "0").strip() or 0)
                      for k in FAIR_PLAY_POINTS)
            totals[team] = totals.get(team, 0) + pts
    return totals


# ---------------------------------------------------------------- ranking

def compute_standings(
    matches: Iterable[Match],
    fair_play: Mapping[str, int] | None = None,
) -> Standings:
    """Rank every group present in `matches` and the third-place teams across them.

    Only matches with status "played" count toward the tables; scheduled
    matches still contribute team discovery so unstarted teams appear with
    zero rows.
    """
    matches = list(matches)
    fp = dict(fair_play or {})
    all_teams = {m.team_a for m in matches} | {m.team_b for m in matches}
    unknown = sorted(set(fp) - all_teams)
    if unknown:
        raise ValueError(
            "fair_play contains team name(s) not in the fixtures: "
            + ", ".join(repr(t) for t in unknown)
            + " — normalize to the team-name canon (CLAUDE.md) before joining")
    warnings: list[str] = []
    groups: dict[str, GroupTable] = {}
    thirds: list[TeamRow] = []

    for g in sorted({m.group for m in matches}):
        gm = [m for m in matches if m.group == g]
        teams = sorted({m.team_a for m in gm} | {m.team_b for m in gm})
        if len(teams) != GROUP_SIZE:
            warnings.append(f"Group {g}: expected {GROUP_SIZE} teams, found {len(teams)}")
        if len(gm) != MATCHES_PER_GROUP:
            warnings.append(f"Group {g}: expected {MATCHES_PER_GROUP} fixtures, found {len(gm)}")

        rows = _accumulate(teams, gm, g)
        notes: list[str] = []
        ordered: list[TeamRow] = []
        for cluster in _clusters(rows, _overall_key):
            ordered.extend(_break_group_tie(cluster, gm, fp, notes, g))
        groups[g] = GroupTable(g, ordered, notes)
        if len(ordered) >= 3:
            thirds.append(ordered[2])

    third_notes: list[str] = []
    third_ranked: list[TeamRow] = []
    for cluster in _clusters(thirds, _overall_key):
        third_ranked.extend(_fair_play_then_lots(cluster, fp, third_notes, "Third-place ranking"))

    played = sum(1 for m in matches if m.is_played)
    return Standings(groups, third_ranked, third_notes, played, len(matches), warnings)


def _overall_key(r: TeamRow) -> tuple[int, int, int]:
    return (r.points, r.gd, r.gf)


def _accumulate(teams: Iterable[str], matches: Iterable[Match], group: str) -> list[TeamRow]:
    """Stat rows for `teams` over the played matches where both sides are in `teams`."""
    rows = {t: TeamRow(team=t, group=group) for t in teams}
    for m in matches:
        if not m.is_played or m.score_a is None or m.score_b is None:
            continue
        if m.team_a not in rows or m.team_b not in rows:
            continue
        a, b = rows[m.team_a], rows[m.team_b]
        a.played += 1
        b.played += 1
        a.gf += m.score_a
        a.ga += m.score_b
        b.gf += m.score_b
        b.ga += m.score_a
        if m.score_a > m.score_b:
            a.won += 1
            b.lost += 1
        elif m.score_a < m.score_b:
            b.won += 1
            a.lost += 1
        else:
            a.drawn += 1
            b.drawn += 1
    return [rows[t] for t in teams]


def _clusters(rows: Iterable[TeamRow], key: Callable[[TeamRow], tuple]) -> list[list[TeamRow]]:
    """Sort descending by `key` and group rows whose keys are exactly equal."""
    out: list[list[TeamRow]] = []
    for r in sorted(rows, key=key, reverse=True):
        if out and key(out[-1][-1]) == key(r):
            out[-1].append(r)
        else:
            out.append([r])
    return out


def _break_group_tie(
    cluster: list[TeamRow],
    group_matches: list[Match],
    fair_play: Mapping[str, int],
    notes: list[str],
    group: str,
    _prev: frozenset[str] | None = None,
) -> list[TeamRow]:
    """Order teams tied on overall points/GD/GF: head-to-head mini-table,
    re-applied recursively to subsets that stay tied, then fair play, then lots."""
    if len(cluster) == 1:
        return list(cluster)
    teams = frozenset(r.team for r in cluster)
    if teams != _prev:  # _prev guard: head-to-head already failed to split this exact set
        mini = {r.team: r for r in _accumulate(sorted(teams), group_matches, group)}
        subs = _clusters(cluster, key=lambda r: _overall_key(mini[r.team]))
        if len(subs) > 1:
            _note(notes, f"Group {group}: head-to-head separates {_names(cluster)} "
                         f"(level on points, goal difference and goals scored).")
            out: list[TeamRow] = []
            for sub in subs:
                out.extend(_break_group_tie(sub, group_matches, fair_play, notes, group, _prev=teams))
            return out
    return _fair_play_then_lots(cluster, fair_play, notes, f"Group {group}")


def _fair_play_then_lots(
    cluster: list[TeamRow],
    fair_play: Mapping[str, int],
    notes: list[str],
    label: str,
) -> list[TeamRow]:
    if len(cluster) == 1:
        return list(cluster)
    subs = _clusters(cluster, key=lambda r: (fair_play.get(r.team, 0),))
    if len(subs) > 1:
        _note(notes, f"{label}: fair play points separate {_names(cluster)}.")
    out: list[TeamRow] = []
    for sub in subs:
        if len(sub) > 1:
            sub = sorted(sub, key=lambda r: r.team)
            if all(r.played == 0 for r in sub):
                pass  # nothing played yet; alphabetical is just display order
            elif all(r.played == GAMES_PER_TEAM for r in sub):
                _note(notes, f"⚠️ {label}: {_names(sub)} cannot be separated by any "
                             f"tiebreaker — drawing of lots required (shown alphabetically).")
            else:
                _note(notes, f"{label}: {_names(sub)} level on all criteria so far "
                             f"(order provisional, shown alphabetically).")
        out.extend(sub)
    return out


def _names(rows: Iterable[TeamRow]) -> str:
    return " / ".join(r.team for r in rows)


def _note(notes: list[str], text: str) -> None:
    if text not in notes:
        notes.append(text)


# ---------------------------------------------------------------- projection

def to_dict(s: Standings) -> dict:
    """JSON-safe projection of a Standings object (schema 1) for renderers
    (build_site.py) and any future consumer. Pure: no I/O, no timestamps."""
    def row(r: TeamRow, pos: int, extra: dict | None = None) -> dict:
        d = {
            "pos": pos, "team": r.team, "group": r.group,
            "p": r.played, "w": r.won, "d": r.drawn, "l": r.lost,
            "gf": r.gf, "ga": r.ga, "gd": r.gd, "pts": r.points,
        }
        if extra:
            d.update(extra)
        return d

    return {
        "schema": 1,
        "played": s.played,
        "total": s.total,
        "groups": {
            g: {
                "rows": [row(r, i) for i, r in enumerate(t.rows, 1)],
                "notes": list(t.notes),
            }
            for g, t in s.groups.items()
        },
        "third_place": {
            "rows": [row(r, i, {"qualifying": i <= QUALIFYING_THIRDS})
                     for i, r in enumerate(s.third_place, 1)],
            "notes": list(s.third_place_notes),
        },
        "warnings": list(s.warnings),
    }


# ---------------------------------------------------------------- rendering

def render_markdown(s: Standings) -> str:
    lines = ["# WC26 Group Stage — Standings", ""]
    lines.append(f"_{s.played} of {s.total} fixtures played._")
    lines.append("")

    for g in sorted(s.groups):
        t = s.groups[g]
        lines.append(f"## Group {g}")
        lines.append("")
        lines.append("| Pos | Team | P | W | D | L | GF | GA | GD | Pts |")
        lines.append("|---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for pos, r in enumerate(t.rows, 1):
            lines.append(
                f"| {pos} | {r.team} | {r.played} | {r.won} | {r.drawn} | {r.lost} "
                f"| {r.gf} | {r.ga} | {_fmt_gd(r.gd)} | {r.points} |")
        lines.append("")
        for n in t.notes:
            lines.append(f"- {n}")
        if t.notes:
            lines.append("")

    if s.third_place:
        lines.append("## Third-place ranking")
        lines.append("")
        lines.append(f"_Best {QUALIFYING_THIRDS} third-placed teams advance to the Round of 32 "
                     "alongside the group winners and runners-up._")
        lines.append("")
        lines.append("| Pos | Team | Grp | P | W | D | L | GF | GA | GD | Pts | In |")
        lines.append("|---:|:---|:---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|")
        for pos, r in enumerate(s.third_place, 1):
            mark = "✅" if pos <= QUALIFYING_THIRDS else ""
            lines.append(
                f"| {pos} | {r.team} | {r.group} | {r.played} | {r.won} | {r.drawn} | {r.lost} "
                f"| {r.gf} | {r.ga} | {_fmt_gd(r.gd)} | {r.points} | {mark} |")
        lines.append("")
        for n in s.third_place_notes:
            lines.append(f"- {n}")
        if s.third_place_notes:
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _fmt_gd(gd: int) -> str:
    return f"+{gd}" if gd > 0 else str(gd)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    default = Path(__file__).resolve().parents[1] / "data" / "fixtures.csv"
    ap = argparse.ArgumentParser(
        description="Compute WC26 group tables and the third-place ranking from played matches.")
    ap.add_argument("--fixtures", type=Path, default=default,
                    help=f"path to fixtures CSV (default: {default})")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.fixtures.exists():
        print(f"error: {args.fixtures} not found.\n"
              "Per wc26_claude_code_port.md, copy wc26_group_stage_fixtures.csv from the "
              "claude.ai project into data/fixtures.csv.", file=sys.stderr)
        return 1
    try:
        matches = load_fixtures(args.fixtures)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    standings = compute_standings(matches)
    for w in standings.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(render_markdown(standings), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
