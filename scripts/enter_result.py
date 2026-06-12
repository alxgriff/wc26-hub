#!/usr/bin/env python3
"""Record a played result into data/fixtures.csv, contract-safely.

    python scripts/enter_result.py A2 0-0          # South Korea 0–0 Czechia
    python scripts/enter_result.py A2 2-2 --force  # overwrite an existing result

Sets ``score_a``, ``score_b`` and ``status=played`` on the matching row and
nothing else. Only the edited row is re-serialised (via the csv module); every
other line is written back byte-for-byte, so the daily git diff stays to the one
result that changed — the accountability trail PLAN.md asks for. Refuses to
overwrite a row already marked ``played`` unless ``--force`` is given.

Importable API (for tests):

    new_lines, msg = apply_result(lines, "A2", 0, 0, force=False)
    a, b           = parse_score("2-1")
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_MATCH_ID_RE = re.compile(r"^[A-L][1-6]$")
_SCORE_RE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*$")  # hyphen or en dash


class ResultError(ValueError):
    """A contract violation the user must fix (unknown id, bad score, overwrite)."""


def parse_score(text: str) -> tuple[int, int]:
    m = _SCORE_RE.match(text)
    if not m:
        raise ResultError(f"score must look like '2-1' (got {text!r})")
    return int(m.group(1)), int(m.group(2))


def _serialise_row(fields: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(fields)
    return buf.getvalue()


def apply_result(lines: list[str], match_id: str, score_a: int, score_b: int,
                 force: bool = False) -> tuple[list[str], str]:
    """Return new file lines (no trailing newlines) with ``match_id`` marked
    played at the given score, plus a human-readable confirmation message.
    Raises ResultError on any contract problem."""
    if not _MATCH_ID_RE.match(match_id):
        raise ResultError(f"bad match_id {match_id!r} (expected A1–L6)")
    if not lines:
        raise ResultError("empty fixtures file")

    header = next(csv.reader([lines[0]]))
    col = {name: i for i, name in enumerate(header)}
    for required in ("match_id", "score_a", "score_b", "status", "team_a", "team_b"):
        if required not in col:
            raise ResultError(f"fixtures header missing required column {required!r}")

    for idx in range(1, len(lines)):
        if not lines[idx].strip():
            continue
        fields = next(csv.reader([lines[idx]]))
        if fields[col["match_id"]] != match_id:
            continue

        if fields[col["status"]].strip().lower() == "played" and not force:
            old = f"{fields[col['score_a']]}–{fields[col['score_b']]}"
            raise ResultError(
                f"{match_id} is already played ({old}). Pass --force to overwrite.")

        fields[col["score_a"]] = str(score_a)
        fields[col["score_b"]] = str(score_b)
        fields[col["status"]] = "played"
        new_lines = list(lines)
        new_lines[idx] = _serialise_row(fields)
        msg = (f"{match_id}: {fields[col['team_a']]} {score_a}–{score_b} "
               f"{fields[col['team_b']]} (status → played)")
        return new_lines, msg

    raise ResultError(f"{match_id} not found in fixtures")


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Record a played result into data/fixtures.csv.")
    ap.add_argument("match_id", help="match id, e.g. A2")
    ap.add_argument("score", help="score as A-B, e.g. 2-1")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing played result")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.fixtures.exists():
        print(f"error: {args.fixtures} not found.", file=sys.stderr)
        return 1
    try:
        score_a, score_b = parse_score(args.score)
        lines = _read_lines(args.fixtures)
        new_lines, msg = apply_result(lines, args.match_id, score_a, score_b, args.force)
    except ResultError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    _write_lines(args.fixtures, new_lines)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
