#!/usr/bin/env python3
"""Knockout-stage data contract for the 2026 World Cup (matches 73-104).

`data/fixtures.csv` stays the GROUP-stage single source of truth — its `match_id` is
locked to the `A1..L6` form by `standings.parse_fixtures`, so a knockout fixture cannot
live there. The knockout stage — whose SCHEDULE is fixed in advance but whose
PARTICIPANTS resolve from group results — lives in `data/knockout.csv`, keyed by the
FIFA match number (73-104), the same numbering `scripts/bracket.py` uses.

This module is the loader + integrity layer + the small helpers that connect
`knockout.csv` to the bracket engine (`results_dict` -> `bracket.feed(results=...)`)
and to the builders (`slot_labels`, `round_of`). It carries NO model and makes NO
prediction — `bracket.py` owns the structure, `predict.py` owns the model, this owns
the schedule + results. Pure stdlib.

Data model:
  - The SCHEDULE columns (round, date_et, kickoff, stadium, city, country, tv_us) are
    static — entered once from the published FIFA calendar, never recomputed.
  - team_a / team_b are a MATERIALIZED VIEW of the bracket: blank until the match's
    participants are known, then filled by the R32 resolver (from final group
    standings) and the results feed (winner propagation). bracket.py remains the
    deriving authority; a consistency test guards drift.
  - score_a / score_b are the 90'+ET aggregate; `decided_by` records how it ended and
    `winner` (A|B) is authoritative for advancement — the only way to know who went
    through when a level game is settled on penalties.

CLI:  python scripts/knockout.py [--knockout data/knockout.csv]
"""
from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bracket as bk  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOCKOUT_CSV = REPO_ROOT / "data" / "knockout.csv"
VENUES_CSV = REPO_ROOT / "data" / "venues.csv"

KO_MIN, KO_MAX = 73, 104
DECIDED_BY = ("regulation", "extra_time", "penalties")
WINNER_SIDES = ("A", "B")
ROUND_ORDER = ("R32", "R16", "QF", "SF", "3RD", "Final")

# CSV column contract (header order). score_* blank until played; team_* blank until resolved.
COLUMNS = ("match_no", "round", "date_et", "kickoff_et_24h", "kickoff_et",
           "stadium", "city", "country", "tv_us", "team_a", "team_b",
           "score_a", "score_b", "decided_by", "winner", "status", "notes")
_REQUIRED_COLUMNS = ("match_no", "round", "status")


def round_of(match_no: int) -> str:
    """The round name for a knockout match number (73-104)."""
    if 73 <= match_no <= 88:
        return "R32"
    if 89 <= match_no <= 96:
        return "R16"
    if 97 <= match_no <= 100:
        return "QF"
    if match_no in (101, 102):
        return "SF"
    if match_no == bk.THIRD_PLACE_MATCH:        # 103
        return "3RD"
    if match_no == max(bk.BRACKET_TREE):        # 104
        return "Final"
    raise ValueError(f"match_no {match_no} is not a knockout match ({KO_MIN}-{KO_MAX})")


# Integrity: the knockout match-number space must be exactly bracket's R32 template ∪
# the winner-of tree ∪ the third-place play-off — no drift between the two modules.
_ALL_KO = frozenset(range(KO_MIN, KO_MAX + 1))
assert _ALL_KO == (frozenset(bk.R32_TEMPLATE) | frozenset(bk.BRACKET_TREE)
                   | {bk.THIRD_PLACE_MATCH}), \
    "knockout.py match-number space disagrees with bracket.py (R32_TEMPLATE/BRACKET_TREE)"


def _r32_slot_label(slot: tuple) -> str:
    kind, val = slot
    if kind == "W":
        return f"Winner {val}"
    if kind == "RU":
        return f"Runner-up {val}"
    if kind == "3RD":
        return "Best 3rd (" + "/".join(val) + ")"
    raise ValueError(f"unknown R32 slot {slot!r}")


def slot_labels(match_no: int) -> tuple[str, str]:
    """The structural (home, away) labels for a match — used to render a fixture before
    its participants resolve. R32 reads the group-position template; later rounds name
    their feeder matches; the third-place play-off names the two semi-final losers."""
    if match_no in bk.R32_TEMPLATE:
        a, b = bk.R32_TEMPLATE[match_no]
        return _r32_slot_label(a), _r32_slot_label(b)
    if match_no == bk.THIRD_PLACE_MATCH:
        return "Loser M101", "Loser M102"
    f1, f2 = bk.BRACKET_TREE[match_no]
    return f"Winner M{f1}", f"Winner M{f2}"


@dataclass
class KnockoutMatch:
    match_no: int
    round: str
    date_et: str
    kickoff_et_24h: str
    kickoff_et: str
    stadium: str
    city: str
    country: str
    tv_us: str
    team_a: str            # "" until the home participant resolves
    team_b: str            # "" until the away participant resolves
    score_a: int | None    # 90'+ET aggregate; None until played
    score_b: int | None
    decided_by: str        # "" | regulation | extra_time | penalties
    winner: str            # "" | "A" | "B" — authoritative for advancement
    status: str            # scheduled | played
    notes: str

    @property
    def is_played(self) -> bool:
        return self.status == "played"

    @property
    def participants_known(self) -> bool:
        return bool(self.team_a and self.team_b)

    @property
    def winner_team(self) -> str | None:
        if self.winner == "A":
            return self.team_a or None
        if self.winner == "B":
            return self.team_b or None
        return None

    @property
    def loser_team(self) -> str | None:
        if self.winner == "A":
            return self.team_b or None
        if self.winner == "B":
            return self.team_a or None
        return None

    @property
    def labels(self) -> tuple[str, str]:
        return slot_labels(self.match_no)


def _venue_canon() -> set[str] | None:
    """The exact stadium strings from data/venues.csv (the Sweat-Factor join key), or
    None if the file is absent (skip the canon check rather than coupling hard to it)."""
    if not VENUES_CSV.exists():
        return None
    with VENUES_CSV.open(encoding="utf-8-sig", newline="") as f:
        return {(row.get("stadium") or "").strip() for row in csv.DictReader(f)}


def _parse_score(value: str | None, no: int, col: str) -> int | None:
    value = (value or "").strip()
    if value == "":
        return None
    try:
        score = int(value)
    except ValueError:
        raise ValueError(f"M{no}: {col} is not an integer: {value!r}") from None
    if score < 0:
        raise ValueError(f"M{no}: {col} is negative: {score}")
    return score


def parse_knockout(rows, warnings: list | None = None) -> list[KnockoutMatch]:
    """Validate + parse knockout rows. Raises on any structural / contract violation
    (the same stop-and-report discipline as standings.parse_fixtures); appends soft
    issues (incomplete schedule) to ``warnings`` if given."""
    warn = warnings if warnings is not None else []
    venues = _venue_canon()
    out: list[KnockoutMatch] = []
    seen: set[int] = set()
    for raw in rows:
        no_s = (raw.get("match_no") or "").strip()
        try:
            no = int(no_s)
        except ValueError:
            raise ValueError(f"bad match_no {no_s!r} (expected an integer {KO_MIN}-{KO_MAX})") from None
        if not (KO_MIN <= no <= KO_MAX):
            raise ValueError(f"match_no {no} outside the knockout range {KO_MIN}-{KO_MAX}")
        if no in seen:
            raise ValueError(f"duplicate match_no {no}")
        seen.add(no)

        rnd = (raw.get("round") or "").strip() or round_of(no)
        if rnd != round_of(no):
            raise ValueError(f"M{no}: round {rnd!r} inconsistent with match_no (expected {round_of(no)!r})")

        status = (raw.get("status") or "").strip().lower()
        if status not in ("scheduled", "played"):
            raise ValueError(f"M{no}: status must be 'scheduled' or 'played', got {raw.get('status')!r}")

        team_a = (raw.get("team_a") or "").strip()
        team_b = (raw.get("team_b") or "").strip()
        if team_a and team_b and team_a == team_b:
            raise ValueError(f"M{no}: team_a and team_b are both {team_a!r}")

        stadium = (raw.get("stadium") or "").strip()
        if stadium and venues is not None and stadium not in venues:
            raise ValueError(
                f"M{no}: stadium {stadium!r} is not in data/venues.csv canon "
                "(Sweat Factor joins on the exact stadium string — normalise or add the venue)")

        score_a = _parse_score(raw.get("score_a"), no, "score_a")
        score_b = _parse_score(raw.get("score_b"), no, "score_b")
        decided_by = (raw.get("decided_by") or "").strip().lower()
        winner = (raw.get("winner") or "").strip().upper()

        if status == "scheduled":
            if score_a is not None or score_b is not None:
                raise ValueError(f"M{no}: status is 'scheduled' but a score is present")
            if winner:
                raise ValueError(f"M{no}: status is 'scheduled' but a winner is set")
            if decided_by:
                raise ValueError(f"M{no}: status is 'scheduled' but decided_by is set")
        else:  # played
            if score_a is None or score_b is None:
                raise ValueError(f"M{no}: status is 'played' but scores are incomplete")
            if not (team_a and team_b):
                raise ValueError(f"M{no}: status is 'played' but the participants are not both known")
            if winner not in WINNER_SIDES:
                raise ValueError(f"M{no}: a played knockout match needs winner in {WINNER_SIDES}, got {winner!r}")
            if decided_by not in DECIDED_BY:
                raise ValueError(f"M{no}: a played knockout match needs decided_by in {DECIDED_BY}, got {decided_by!r}")
            # A knockout match cannot end level: unequal score => decided in regulation/ET on the
            # higher side; equal score => settled on penalties (winner is the shootout winner).
            if score_a == score_b:
                if decided_by != "penalties":
                    raise ValueError(
                        f"M{no}: level after play ({score_a}-{score_b}) must be decided_by 'penalties'")
            else:
                if decided_by == "penalties":
                    raise ValueError(
                        f"M{no}: scores differ ({score_a}-{score_b}) so it was not settled on penalties")
                higher = "A" if score_a > score_b else "B"
                if winner != higher:
                    raise ValueError(
                        f"M{no}: winner {winner!r} contradicts the score {score_a}-{score_b} "
                        f"(the {higher} side won in normal/extra time)")

        out.append(KnockoutMatch(
            no, rnd, (raw.get("date_et") or "").strip(),
            (raw.get("kickoff_et_24h") or "").strip(), (raw.get("kickoff_et") or "").strip(),
            stadium, (raw.get("city") or "").strip(), (raw.get("country") or "").strip(),
            (raw.get("tv_us") or "").strip(), team_a, team_b, score_a, score_b,
            decided_by, winner, status, (raw.get("notes") or "").strip()))

        if not (raw.get("date_et") or "").strip() or not stadium:
            warn.append(f"M{no}: schedule incomplete (date_et/stadium missing)")

    out.sort(key=lambda k: k.match_no)
    return out


def load_knockout(path: str | Path = KNOCKOUT_CSV,
                  warnings: list | None = None) -> list[KnockoutMatch]:
    """Load + validate data/knockout.csv. A MISSING file returns [] (the knockout stage
    hasn't started / been scheduled yet — fail-soft, like predict.load_match_overlay);
    a malformed file RAISES (stop-and-report)."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in _REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing required column(s): {', '.join(missing)}")
        return parse_knockout(reader, warnings)


def results_dict(matches: list[KnockoutMatch]) -> dict[int, str]:
    """{match_no: winning_team_name} for every played match whose winner is known — the
    `results` argument bracket.feed propagates through the tree (an actual result
    overrides the model). Penalty wins are honoured via the explicit `winner` side."""
    return {k.match_no: k.winner_team for k in matches if k.is_played and k.winner_team}


def by_no(matches: list[KnockoutMatch]) -> dict[int, KnockoutMatch]:
    return {k.match_no: k for k in matches}


def _no_winner(_a, _b):
    """A feed resolver that decides nothing — used to propagate PARTICIPANTS through the
    tree from actual results only (no model projection), so team-materialization is
    fact-driven, never speculative."""
    return None


def materialize_teams(proj: dict, matches: list[KnockoutMatch]) -> list[KnockoutMatch]:
    """Fill team_a/team_b on each SCHEDULED knockout match from FACTS, returning a new
    list (idempotent; played rows are never touched — their participants are history):

      - R32 (73-88): from locked group positions — a side is written only when its
        group slot is non-provisional (the group is complete / the third-place set is
        settled), so a not-yet-sealed position stays blank rather than guessing.
      - R16+ (89-104): from the winners of already-PLAYED feeder matches, propagated by
        bracket.feed using only real results. A round fills the moment both its feeders
        are decided; until then it stays blank.

    `proj` is a bracket.project(...) dict; bracket.py remains the deriving authority and
    this only materializes its output into the self-contained knockout.csv rows."""
    import dataclasses
    r32 = {int(k): v for k, v in proj["r32"].items()}
    fed = bk.feed(proj, _no_winner, results=results_dict(matches))
    participants = {int(k): v for k, v in fed.get("participants", {}).items()}
    out: list[KnockoutMatch] = []
    for k in matches:
        if k.is_played:
            out.append(k)
            continue
        if k.match_no in r32:
            e = r32[k.match_no]
            ta = e["home"] if (e.get("home") and not e.get("home_provisional")) else ""
            tb = e["away"] if (e.get("away") and not e.get("away_provisional")) else ""
        else:
            pair = participants.get(k.match_no)
            ta, tb = (pair[0], pair[1]) if pair else ("", "")
        out.append(dataclasses.replace(k, team_a=ta or "", team_b=tb or ""))
    return out


def write_knockout(path: str | Path, matches: list[KnockoutMatch]) -> None:
    """Write the knockout table (sorted by match_no) with a UTF-8 BOM and the COLUMNS
    order — a deterministic, minimal-quote round-trip so machine updates (resolver,
    results feed) produce clean diffs."""
    path = Path(path)
    rows = []
    for k in sorted(matches, key=lambda m: m.match_no):
        rows.append({
            "match_no": k.match_no, "round": k.round, "date_et": k.date_et,
            "kickoff_et_24h": k.kickoff_et_24h, "kickoff_et": k.kickoff_et,
            "stadium": k.stadium, "city": k.city, "country": k.country, "tv_us": k.tv_us,
            "team_a": k.team_a, "team_b": k.team_b,
            "score_a": "" if k.score_a is None else k.score_a,
            "score_b": "" if k.score_b is None else k.score_b,
            "decided_by": k.decided_by, "winner": k.winner, "status": k.status, "notes": k.notes,
        })
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Load + summarise the WC26 knockout schedule.")
    ap.add_argument("--knockout", type=Path, default=KNOCKOUT_CSV)
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--resolve", action="store_true",
                    help="materialize team_a/team_b from current standings + played results, then write")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    warnings: list[str] = []
    matches = load_knockout(args.knockout, warnings)
    if not matches:
        print(f"No knockout schedule yet at {args.knockout} (group stage in progress).")
        return 0

    if args.resolve:
        import standings as st
        fixtures = st.load_fixtures(args.fixtures)
        standings = st.compute_standings(fixtures, fair_play=st.load_discipline())
        proj = bk.project(standings)
        matches = materialize_teams(proj, matches)
        write_knockout(args.knockout, matches)
        known = sum(1 for m in matches if m.participants_known)
        print(f"Resolved {known}/{len(matches)} knockout matchups into {args.knockout}", file=sys.stderr)
    current = None
    for k in matches:
        if k.round != current:
            current, _ = k.round, print(f"\n## {k.round}")
        a, b = (k.team_a or f"*{k.labels[0]}*"), (k.team_b or f"*{k.labels[1]}*")
        when = f"{k.date_et} {k.kickoff_et}".strip()
        if k.is_played:
            tail = f"  →  {k.score_a}-{k.score_b} ({k.decided_by}), {k.winner_team} advance"
        else:
            tail = ""
        print(f"- M{k.match_no} [{when} · {k.stadium}]: {a} vs {b}{tail}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
