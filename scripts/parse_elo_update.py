#!/usr/bin/env python3
"""Parse raw eloratings.net pasted text, extract WC2026 team ratings,
roll back any June 18 games already captured, and write the updated CSV.

Usage:
    python3 scripts/parse_elo_update.py <raw_text_file> [--out <csv_path>]

The raw_text_file should contain the pasted text from eloratings.net verbatim.
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Canonical WC2026 team names (from CLAUDE.md) → how they appear on eloratings.net
NAME_MAP = {
    "Türkiye": "Turkey",
    "Côte d'Ivoire": "Ivory Coast",
    "DR Congo": "DR Congo",          # same
    "United States": "United States", # same
    "South Korea": "South Korea",     # same
    "Bosnia and Herzegovina": "Bosnia and Herzegovina",
    "Cape Verde": "Cape Verde",
    "Saudi Arabia": "Saudi Arabia",
    "New Zealand": "New Zealand",
    "South Africa": "South Africa",
}

# June 18 Elo changes already baked in (from eloratings.net results page).
# Format: {canonical_name: points_to_ADD_BACK (i.e. reverse the change)}
# Czechia lost 16 pts → add 16 back; South Africa gained 16 → subtract 16, etc.
JUNE18_ROLLBACK = {
    "Czechia": +16,
    "South Africa": -16,
    "Switzerland": -20,
    "Bosnia and Herzegovina": +20,
    # Mexico/South Korea (9PM ET) and Canada/Qatar (6PM ET) not yet played
    # when data was pulled — no rollback needed for those.
}

SOURCE = "eloratings.net"
ASOF = "2026-06-17"   # post-MD1, pre-June-18-games


def parse_ratings(raw: str, wc_teams: list[str]) -> dict[str, int]:
    """Extract ratings for each WC2026 team from raw pasted text.
    Strategy: for each team, find its display name in the text and grab
    the next 4-digit number as the rating."""
    ratings = {}
    for canon in wc_teams:
        display = NAME_MAP.get(canon, canon)
        # Escape for regex; ratings follow immediately after the name
        pat = re.escape(display) + r"\D{0,3}(\d{4})"
        m = re.search(pat, raw)
        if m:
            ratings[canon] = int(m.group(1))
        else:
            print(f"  WARNING: could not find '{display}' in raw text", file=sys.stderr)
    return ratings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw_file", help="File containing raw pasted text from eloratings.net")
    ap.add_argument("--out", default=str(REPO / "data" / "ratings" / "Elo_Ratings_PostMD1_VERIFIED.csv"))
    args = ap.parse_args()

    raw = Path(args.raw_file).read_text(encoding="utf-8")

    # Load canonical team list from existing file
    existing = REPO / "data" / "ratings" / "Elo_Ratings_World_Cup_2026_VERIFIED.csv"
    wc_teams = []
    with existing.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            wc_teams.append(row["Team"])

    print(f"Parsing ratings for {len(wc_teams)} WC2026 teams...")
    ratings = parse_ratings(raw, wc_teams)

    # Apply June 18 rollbacks
    for team, delta in JUNE18_ROLLBACK.items():
        if team in ratings:
            before = ratings[team]
            ratings[team] += delta
            print(f"  Rolled back {team}: {before} → {ratings[team]} ({delta:+d})")

    # Write output CSV
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Team", "Elo_Rating", "source", "asof"])
        found = 0
        for team in wc_teams:
            if team in ratings:
                w.writerow([team, ratings[team], SOURCE, ASOF])
                found += 1
            else:
                print(f"  MISSING: {team} — skipped", file=sys.stderr)
    print(f"\nWrote {found}/{len(wc_teams)} teams to {out.relative_to(REPO)}")

    # Quick sanity check vs June 11
    print("\nTop movers vs June 11 frozen ratings:")
    deltas = []
    with existing.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = row["Team"]
            if t in ratings:
                d = ratings[t] - int(row["Elo_Rating"])
                deltas.append((t, int(row["Elo_Rating"]), ratings[t], d))
    deltas.sort(key=lambda x: abs(x[3]), reverse=True)
    for t, old, new, d in deltas[:10]:
        print(f"  {t}: {old} → {new} ({d:+d})")


if __name__ == "__main__":
    raise SystemExit(main())
