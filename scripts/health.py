#!/usr/bin/env python3
"""Data-freshness invariants — the silent-stall detector.

WHY: every pipeline data step is fail-soft by design (a dead API must never block the
morning publish), which means a *data* stall doesn't make anything red: "0 results
entered" is a healthy-looking no-op. That is exactly how three R32 shootout results sat
unentered for five days (2026-06-29 → 07-04) with green CI throughout — R16 ties could
not resolve, their cards never generated, and nothing alarmed. These checks look at the
DATA, not the steps, and fail loudly when the world and the repo disagree:

  1. STALL — a resolved knockout tie whose date has passed is still `scheduled`
     (results feed failing / a shootout awaiting entry nobody saw).
  2. STALL — a played extra-time/penalties tie has no 90' regulation score
     (its totals/handicap/BTTS picks can never settle).
  3. GAP — a resolved, unplayed tie kicking off within WINDOW days has no card
     (cards/ko/M{no}.md missing: generation failed or never ran).

Read-only. Exit 0 = healthy (or nothing to check); exit 1 = at least one invariant
violated. Run in CI after the knockout data steps (so the current run had its chance to
fix things first) with continue-on-error + the health gate, matching the publish-then-
alarm pattern: a stall ships the best build it can, then turns the run red.

CLI: python scripts/health.py [--knockout data/knockout.csv] [--cards-dir cards/ko]
     [--window 3] [--today YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import knockout as ko          # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WINDOW = 3             # matches knockout_cards --window in the workflows

# date_et is an ET calendar date, so "today" MUST be the ET date too (same fixed-EDT
# convention as ledger.py — the tournament sits entirely inside daylight time). Using
# UTC false-flagged evening games: past 8 PM ET the UTC day rolls over, so a game still
# being played (or minutes final) read as "played yesterday, no result", and the card
# window disagreed with knockout_cards' ET editorial date (both bit the 02:33 UTC
# verification run on 2026-07-06).
ET = timezone(timedelta(hours=-4))


def today_et() -> date:
    return datetime.now(tz=timezone.utc).astimezone(ET).date()


def check_knockout(matches: list, today: date, cards_dir: Path,
                   window: int = DEFAULT_WINDOW) -> list[str]:
    """Every violated invariant as a human-readable line (empty list = healthy).
    Pure — no I/O beyond the card-file existence probe."""
    issues: list[str] = []
    for km in matches:
        try:
            d = date.fromisoformat(km.date_et)
        except ValueError:
            issues.append(f"STALL M{km.match_no}: unparseable date_et {km.date_et!r}")
            continue
        if km.participants_known and not km.is_played and d < today:
            issues.append(
                f"STALL M{km.match_no}: {km.team_a} vs {km.team_b} was played "
                f"{km.date_et} but no result is entered — run fetch_ko_results.py; "
                "if it reports a shootout ESPN can't confirm, enter manually "
                f"(knockout.py --enter {km.match_no} ...)")
        if km.is_played and km.decided_by in ("extra_time", "penalties") \
                and km.reg_score is None:
            issues.append(
                f"STALL M{km.match_no}: {km.team_a} vs {km.team_b} reached "
                f"{km.decided_by} but has no 90' regulation score — its 90' bets "
                "cannot settle; run fetch_ko_reg_scores.py")
        if km.participants_known and not km.is_played \
                and 0 <= (d - today).days <= window \
                and not (Path(cards_dir) / f"M{km.match_no}.md").exists():
            issues.append(
                f"GAP M{km.match_no}: {km.team_a} vs {km.team_b} kicks off "
                f"{km.date_et} but cards/ko/M{km.match_no}.md does not exist — "
                "run knockout_cards.py (or the ko-cards skill without a key)")
    return issues


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Data-freshness invariants (silent-stall detector).")
    ap.add_argument("--knockout", type=Path, default=REPO_ROOT / "data" / "knockout.csv")
    ap.add_argument("--cards-dir", type=Path, default=REPO_ROOT / "cards" / "ko")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help="days ahead a resolved tie must already have a card (default 3)")
    ap.add_argument("--today", help="override the ET date, for testing (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.knockout.exists():
        print("no knockout schedule — nothing to check.")
        return 0
    today = date.fromisoformat(args.today) if args.today else today_et()
    matches = ko.load_knockout(args.knockout)
    issues = check_knockout(matches, today, args.cards_dir, window=args.window)
    for line in issues:
        print(line)
    if issues:
        print(f"{len(issues)} data-freshness issue(s) — the pipeline is silently stalled.")
        return 1
    print("data-freshness invariants OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
