#!/usr/bin/env python3
"""WC26 Matchday-3 scenario enumerator.

A group entering its final matchday has two games left, and they kick off
simultaneously (CLAUDE.md). That is 3 x 3 = 9 win/draw/loss combinations. For
each combination every team's final **points** are fixed exactly, but the goal
**margins** are not — a win adds an unknown amount to goal difference, a draw
adds exactly zero, a loss subtracts an unknown amount.

The FIFA 2026 group tiebreakers run points -> head-to-head (its points, then GD,
then goals among the level teams) -> overall goal difference -> overall goals ->
fair play -> FIFA World Ranking (no drawing of lots). Head-to-head comes BEFORE
overall goal difference, so a team level on points can already be decided by a
result that has been PLAYED even when the goal margins of the remaining games are
unknown. For the final-matchday case (<=3 games left), this module therefore
determines each team's EXACT reachable finish per outcome combination by replaying
the group through the real tiebreak engine (standings.compute_standings — head-to-
head and all) at the extreme goal margins (a narrow vs a wide win, a low- vs a
high-scoring draw). A finish that is identical at every margin is pinned; one that
still moves with the margins is reported as **margin-dependent**, never guessed.
(With 4+ games still to play it falls back to a cheap points + GD-interval
approximation that does not see head-to-head — conservative: it can over-report
margin-dependence but never the reverse.)

Each team is then bucketed per combo by its EXACT reachable finish — 1st / 2nd /
3rd / out (4th) — plus two "still-open" buckets: **top-2** (through for sure, but
the 1st-vs-2nd seed rides on goal difference) and **margin-dependent** (even the
qualify/eliminate bucket is unresolved). We tally these across the 9 combos. The
1st-vs-2nd split matters: winning the group earns the easier Round-of-32 side, so
a team already through but still chasing top spot has plenty to play for. Note: a
side can always win its last game to at least tie for 3rd, so no team is ever
*guaranteed* 4th — "eliminated" means it cannot reach the top two in any
combination (1st = 2nd = top-2 = 0).

Ranking of decided positions reuses the points/GD ordering; this module never
re-implements the full tiebreak engine (that lives in standings.py).

Importable API (for build_edition's MD3 Stakes slots):

    report = enumerate_scenarios("A", matches)   # ScenarioReport
    md     = render_markdown(report)             # str (caller adds 3rd-place tbl)

CLI:
    python scripts/scenarios.py A [--fixtures data/fixtures.csv]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st  # noqa: E402  (full tiebreak engine lives here, not here)

REPO_ROOT = Path(__file__).resolve().parents[1]
_INF = float("inf")
_OUTCOMES = ("a", "d", "b")               # team_a win / draw / team_b win
_POINT_DELTA = {"a": (3, 0), "d": (1, 1), "b": (0, 3)}
_BUCKET_ORDER = ("first", "second", "top2", "third", "margin", "out")


# ---------------------------------------------------------------- data

@dataclass
class TeamScenario:
    team: str
    group_pos: int          # current position in the live table
    points: int             # current points
    gd: int                 # current goal difference
    gf: int                 # current goals for
    counts: dict            # bucket -> number of combos
    stakes: list            # plain-language Win/Draw/Loss lines (MD3 only)


@dataclass
class ScenarioReport:
    group: str
    n_combos: int
    unplayed: list                     # list[Match] still to play
    teams: list                        # list[TeamScenario], in live-table order
    current_rows: list                 # list[TeamRow], the live group table
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------- classification

def _gd_delta(outcome: str, is_a: bool) -> tuple[float, float]:
    """The (low, high) bounds a single result adds to a team's goal difference."""
    if outcome == "d":
        return (0.0, 0.0)
    team_wins = (outcome == "a") if is_a else (outcome == "b")
    return (1.0, _INF) if team_wins else (-_INF, -1.0)


def _bucket(pos: int) -> str:
    """An EXACT finishing position -> its bucket (1st and 2nd kept distinct, since
    winning the group earns the easier Round-of-32 side)."""
    return {1: "first", 2: "second", 3: "third"}.get(pos, "out")


def _range_to_bucket(lo_pos: int, hi_pos: int) -> str:
    """A reachable EXACT-position range -> a finish bucket. Pinned to one seed ->
    that seed; through but 1st-vs-2nd open -> 'top2'; straddling the qualify/eliminate
    line -> 'margin'."""
    lo_b, hi_b = _bucket(lo_pos), _bucket(hi_pos)
    if lo_b == hi_b:
        return lo_b                                   # exact finish pinned
    if {lo_b, hi_b} <= {"first", "second"}:           # through, but 1st vs 2nd on GD
        return "top2"
    return "margin"                                   # qualify/eliminate still open


# Goal-margin extremes per fixed outcome: a narrow vs a wide win, a low- vs a
# high-scoring draw. Replaying a combo through the real standings engine at these
# extremes brackets every margin-sensitive tiebreaker (overall GD/goals, and the
# head-to-head GD/goals of a tied pair whose mutual game is itself this matchday).
_MARGIN_VARIANTS = {"a": ((1, 0), (8, 0)), "b": ((0, 1), (0, 8)), "d": ((0, 0), (4, 4))}
_PRECISE_MAX_UNPLAYED = 3      # above this the margin-extreme replay is costly and almost
#                                everything is margin-dependent anyway -> GD-interval fallback


def _provisional_bands(notes, order) -> dict:
    """{team: set(tied positions)} for teams flagged in a residual-tie note. compute_standings
    shows such ties alphabetically but flags them — so the scenario treats each tied team's
    finish as the whole tied band, never the silent alphabetical pick (per the project rule:
    a residual tie after fair play is reported, never silently resolved)."""
    bands: dict[str, set] = {}
    for n in notes:
        if "level on all modelled criteria" not in n:
            continue
        seg = n.split(":", 1)[-1].split("level on all modelled criteria")[0]
        tied = [t for t in (p.strip() for p in seg.split("/")) if t in order]
        band = {order[t] for t in tied}
        for t in tied:
            bands[t] = band
    return bands


def _combo_positions(group, played, unplayed, combo, fair_play, cache) -> dict:
    """{team: set(reachable final positions)} for ONE outcome combination, over the
    goal-margin extremes and ranked by the FULL 2026 tiebreakers (head-to-head BEFORE
    overall GD) via standings.compute_standings — the single source of tiebreak truth.
    A singleton set means the exact rank is pinned for that combo; a spread means the
    placing rides on goal margins (or an unresolved residual tie). Cached per combo."""
    import dataclasses
    if combo in cache:
        return cache[combo]
    positions: dict[str, set] = {}
    for margins in product(*(_MARGIN_VARIANTS[oc] for oc in combo)):
        proj = list(played) + [
            dataclasses.replace(m, status="played", score_a=sa, score_b=sb)
            for m, (sa, sb) in zip(unplayed, margins)]
        gt = st.compute_standings(proj, fair_play=fair_play).groups[group]
        order = {r.team: i for i, r in enumerate(gt.rows, 1)}
        bands = _provisional_bands(gt.notes, order)
        for team, pos in order.items():
            positions.setdefault(team, set()).update(bands.get(team, {pos}))
    cache[combo] = positions
    return positions


def _classify_combo_exact(group, played, unplayed, combo, fair_play, cache) -> dict:
    """Final-matchday classifier: bucket every team for one combo by its EXACT reachable
    finish, head-to-head honoured (see _combo_positions). This is what fixes a clinched-
    by-H2H seed being mislabelled 'seed TBD' (e.g. a group winner who holds the H2H over
    the only side that can match its points)."""
    pos = _combo_positions(group, played, unplayed, combo, fair_play, cache)
    return {t: _range_to_bucket(min(ps), max(ps)) for t, ps in pos.items()}


def _classify_combo_gd(teams, current, unplayed, combo) -> dict:
    """Cheaper points + goal-difference-interval fallback for the non-canonical case of
    4+ games still to play (where replaying every margin extreme is costly and almost
    everything is margin-dependent anyway). It does NOT see head-to-head, so it can
    over-report margin-dependence but never under-report a pinned finish."""
    pts = {t: current[t].points for t in teams}
    gd_lo = {t: float(current[t].gd) for t in teams}
    gd_hi = {t: float(current[t].gd) for t in teams}
    for m, oc in zip(unplayed, combo):
        da, db = _POINT_DELTA[oc]
        pts[m.team_a] += da
        pts[m.team_b] += db
        la, ha = _gd_delta(oc, True)
        lb, hb = _gd_delta(oc, False)
        gd_lo[m.team_a] += la
        gd_hi[m.team_a] += ha
        gd_lo[m.team_b] += lb
        gd_hi[m.team_b] += hb
    rng = _position_range(teams, pts, gd_lo, gd_hi)
    return {t: _range_to_bucket(rng[t][0], rng[t][1]) for t in teams}


def _position_range(teams, pts, gd_lo, gd_hi) -> dict:
    """{team: (min_pos, max_pos)} — the reachable EXACT position range within points
    clusters (GD as an interval). min==max means the exact rank is pinned."""
    out = {}
    for t in teams:
        p = pts[t]
        cluster = [u for u in teams if pts[u] == p]
        base = 1 + sum(1 for u in teams if pts[u] > p)
        above = sum(1 for u in cluster if u != t and gd_lo[u] > gd_hi[t])   # must rank ahead
        below = sum(1 for u in cluster if u != t and gd_lo[t] > gd_hi[u])   # must rank behind
        out[t] = (base + above, base + (len(cluster) - 1 - below))
    return out


_CLINCH_SCORES = ((8, 0), (0, 0), (0, 8))   # big home win / draw / big away win


def clinched_ranks(group: str, matches) -> dict:
    """{team: rank} (1=winner, 2=runner-up, 3=third, 4=last) for teams whose EXACT final
    group position is mathematically LOCKED — it is the same in EVERY outcome of the
    remaining games. Uses the FULL 2026 tiebreakers (compute_standings: points → head-to-head
    → GD → goals) by replaying each remaining game at extreme margins, so an H2H- or
    cushion-secured rank is detected (the points+GD-interval bucketing alone misses head-to-
    head). A finished group locks every position; an un-/part-played one yields fewer or none."""
    import dataclasses
    gms = sorted((m for m in matches if m.group == group), key=lambda m: m.match_id)
    if not gms:
        return {}
    played = [m for m in gms if m.is_played]
    rem = [m for m in gms if not m.is_played]
    fp = st.load_discipline()
    if not rem:                                    # group finished — all positions locked
        rows = st.compute_standings(gms, fair_play=fp).groups[group].rows
        return {r.team: i for i, r in enumerate(rows, 1)}
    possible: dict[str, set] = {}
    for combo in product(_CLINCH_SCORES, repeat=len(rem)):
        proj = played + [dataclasses.replace(m, status="played", score_a=s[0], score_b=s[1])
                         for m, s in zip(rem, combo)]
        for i, r in enumerate(st.compute_standings(proj, fair_play=fp).groups[group].rows, 1):
            possible.setdefault(r.team, set()).add(i)
    return {t: next(iter(ps)) for t, ps in possible.items() if len(ps) == 1}


# ---------------------------------------------------------------- plain language

def _word(bucket: str) -> str:
    return {
        "first": "win the group (1st — the easier Round-of-32 side)",
        "second": "runners-up (2nd)",
        "top2": "through, but 1st vs 2nd rides on goal difference",
        "third": "3rd — into the best-thirds race",
        "out": "eliminated (4th)",
        "margin": "margin-dependent (goal difference/goals, then head-to-head, decide)",
    }[bucket]


def _cond(oc: str, x: str, y: str) -> str:
    return {"a": f"{x} beat {y}", "d": f"{x} and {y} draw", "b": f"{y} beat {x}"}[oc]


def _summarise(by_oc: dict, x: str, y: str) -> str:
    if len(set(by_oc.values())) == 1:
        return _word(next(iter(by_oc.values())))
    segs = []
    for bucket in _BUCKET_ORDER:
        ocs = [oc for oc in _OUTCOMES if by_oc.get(oc) == bucket]
        if ocs:
            segs.append(f"{_word(bucket)} if {' or '.join(_cond(oc, x, y) for oc in ocs)}")
    return "; ".join(segs)


def _team_stakes(team, unplayed, group, played, fair_play, cache) -> list:
    """Win/Draw/Loss prospects for `team`, conditioned on the other MD3 game. Only
    called in the standard two-games-left layout, where the exact (head-to-head-aware)
    classifier always applies."""
    i = next(idx for idx, m in enumerate(unplayed) if team in (m.team_a, m.team_b))
    j = 1 - i
    mine, other = unplayed[i], unplayed[j]
    x, y = other.team_a, other.team_b
    own = ({"Win": "a", "Draw": "d", "Loss": "b"} if team == mine.team_a
           else {"Win": "b", "Draw": "d", "Loss": "a"})
    lines = []
    for label, own_oc in own.items():
        by_oc = {}
        for oc in _OUTCOMES:
            combo = [None, None]
            combo[i] = own_oc
            combo[j] = oc
            by_oc[oc] = _classify_combo_exact(group, played, unplayed, tuple(combo),
                                              fair_play, cache)[team]
        lines.append(f"{label}: {_summarise(by_oc, x, y)}")
    return lines


# ---------------------------------------------------------------- public API

def enumerate_scenarios(group: str, matches) -> ScenarioReport:
    """Enumerate the final-matchday outcomes for `group` and bucket each team."""
    group_matches = sorted((m for m in matches if m.group == group), key=lambda m: m.match_id)
    if not group_matches:
        raise ValueError(f"no matches found for group {group!r}")

    gt = st.compute_standings(group_matches,
                              fair_play=st.load_discipline()).groups[group]
    rows = gt.rows
    teams = [r.team for r in rows]
    current = {r.team: r for r in rows}
    pos = {r.team: i for i, r in enumerate(rows, 1)}
    unplayed = [m for m in group_matches if not m.is_played]

    notes = []
    played = [m for m in group_matches if m.is_played]
    # Final-matchday (<=3 games left): replay each combo through the real tiebreak engine
    # at margin extremes, so head-to-head is honoured. Earlier than that -> cheap fallback.
    precise = 0 < len(unplayed) <= _PRECISE_MAX_UNPLAYED
    fair_play = st.load_discipline() if precise else None
    cache: dict = {}
    combos = list(product(_OUTCOMES, repeat=len(unplayed)))
    counts = {t: {b: 0 for b in _BUCKET_ORDER} for t in teams}
    for combo in combos:
        buckets = (_classify_combo_exact(group, played, unplayed, combo, fair_play, cache)
                   if precise else _classify_combo_gd(teams, current, unplayed, combo))
        for t in teams:
            counts[t][buckets[t]] += 1

    # Plain-language stakes only apply to the canonical "two simultaneous games,
    # each team in exactly one of them" layout.
    standard = (len(unplayed) == 2
                and all(sum(t in (m.team_a, m.team_b) for m in unplayed) == 1 for t in teams))
    if not unplayed:
        notes.append("Group already complete — no remaining games to enumerate.")
    elif not standard:
        notes.append(f"{len(unplayed)} games still unplayed — not the standard two-game "
                     "final matchday, so the per-team Win/Draw/Loss breakdown is omitted.")

    team_scenarios = []
    for r in rows:
        team_scenarios.append(TeamScenario(
            team=r.team, group_pos=pos[r.team], points=r.points, gd=r.gd, gf=r.gf,
            counts=counts[r.team],
            stakes=(_team_stakes(r.team, unplayed, group, played, fair_play, cache)
                    if standard else []),
        ))

    return ScenarioReport(group, len(combos), unplayed, team_scenarios, rows, notes)


# ---------------------------------------------------------------- rendering

_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def _ordinal(pos: int) -> str:
    return _ORDINALS.get(pos, f"{pos}th")


def _fmt_gd(gd: int) -> str:
    return f"+{gd}" if gd > 0 else str(gd)


def _render_table(rows) -> list:
    out = ["| Pos | Team | P | W | D | L | GF | GA | GD | Pts |",
           "|---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for pos, r in enumerate(rows, 1):
        out.append(f"| {pos} | {r.team} | {r.played} | {r.won} | {r.drawn} | {r.lost} "
                   f"| {r.gf} | {r.ga} | {_fmt_gd(r.gd)} | {r.points} |")
    return out


def render_markdown(report: ScenarioReport) -> str:
    """Group-scoped scenario markdown. Callers append the cross-group
    third-place table so '3rd' can be read against the eight-team cutline."""
    g = report.group
    lines = [f"# Group {g} — Matchday 3 scenarios", ""]
    if report.unplayed:
        rem = "; ".join(f"{m.team_a} vs {m.team_b}" for m in report.unplayed)
        lines += [f"_{report.n_combos} outcome combinations from the remaining game(s) "
                  f"({rem}). Goal margins are unknown, so any placing that comes down to "
                  "goal difference is flagged **margin-dependent**._", ""]
    else:
        lines += ["_Group complete._", ""]
    for n in report.notes:
        lines += [f"> ⚠️ {n}", ""]

    lines += ["## Current table", "", *_render_table(report.current_rows), ""]

    lines += ["## Where each team can finish",
              f"_Across all {report.n_combos} combinations. Top two advance directly — winning "
              "the group earns the easier Round-of-32 side, so 1st and 2nd are kept apart; the "
              "best eight third-placed teams also reach the Round of 32. **Top 2 (seed TBD)** = "
              "through, but the 1st-vs-2nd seed still rides on goal difference._", "",
              "| Team | 1st | 2nd | Top 2 (seed TBD) | 3rd | Out (4th) | Margin |",
              "|:---|---:|---:|---:|---:|---:|---:|"]
    for ts in report.teams:
        c = ts.counts
        lines.append(f"| {ts.team} | {c['first']} | {c['second']} | {c['top2']} | "
                     f"{c['third']} | {c['out']} | {c['margin']} |")
    lines.append("")

    if any(ts.stakes for ts in report.teams):
        lines += ["## What each team needs", ""]
        for ts in report.teams:
            if not ts.stakes:
                continue
            unit = "pt" if ts.points == 1 else "pts"
            lines.append(f"- **{ts.team}** — now {_ordinal(ts.group_pos)} on {ts.points} {unit} "
                         f"({_fmt_gd(ts.gd)} GD):")
            lines += [f"  - {s}" for s in ts.stakes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _team_scenario(report: ScenarioReport, name: str):
    return next((ts for ts in report.teams if ts.team == name), None)


def render_match_stakes(report: ScenarioReport, team_a: str, team_b: str) -> str:
    """Compact Stakes block for a single MD3 card: the finish distribution and
    Win/Draw/Loss prospects for just the two teams in this game. Used by
    build_edition to fill an MD3 card's Stakes slot."""
    ta, tb = _team_scenario(report, team_a), _team_scenario(report, team_b)
    if ta is None or tb is None:
        return f"*[No MD3 scenario available for {team_a} vs {team_b}.]*"
    lines = [
        f"_Final matchday: both Group {report.group} games kick off simultaneously — "
        f"{report.n_combos} possible outcomes. Placings that come down to goal difference "
        "are flagged margin-dependent._",
        "",
        "| Team | 1st | 2nd | Top 2 (seed TBD) | 3rd | Out | Margin |",
        "|:---|---:|---:|---:|---:|---:|---:|",
    ]
    for ts in (ta, tb):
        c = ts.counts
        lines.append(f"| {ts.team} | {c['first']} | {c['second']} | {c['top2']} | "
                     f"{c['third']} | {c['out']} | {c['margin']} |")
    lines.append("")
    for ts in (ta, tb):
        if ts.stakes:
            lines.append(f"**{ts.team}:**")
            lines += [f"- {s}" for s in ts.stakes]
            lines.append("")
    return "\n".join(lines).rstrip()


def _third_place_section(full_md: str) -> str | None:
    marker = "## Third-place ranking"
    idx = full_md.find(marker)
    return full_md[idx:].rstrip() if idx != -1 else None


# ---------------------------------------------------------------- CLI

def main(argv: list | None = None) -> int:
    default = REPO_ROOT / "data" / "fixtures.csv"
    ap = argparse.ArgumentParser(
        description="Enumerate a group's final-matchday qualification scenarios.")
    ap.add_argument("group", help="group letter A-L")
    ap.add_argument("--fixtures", type=Path, default=default)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    group = args.group.strip().upper()
    if group not in st.GROUP_LETTERS:
        print(f"error: group must be one of {st.GROUP_LETTERS}, got {args.group!r}", file=sys.stderr)
        return 2
    if not args.fixtures.exists():
        print(f"error: {args.fixtures} not found.", file=sys.stderr)
        return 1
    try:
        matches = st.load_fixtures(args.fixtures)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    full = st.compute_standings(matches, fair_play=st.load_discipline())
    for w in full.warnings:
        print(f"warning: {w}", file=sys.stderr)

    report = enumerate_scenarios(group, matches)
    out = render_markdown(report)

    third = _third_place_section(st.render_markdown(full))
    if third:
        out = out.rstrip() + "\n\n## Third-place race (live)\n\n" + third + "\n"
    print(out, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
