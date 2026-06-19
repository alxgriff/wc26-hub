"""update_elo.py — roll the verified pre-tournament Elo forward through played WC
results, so the model's Elo stays current for the rest of the tournament.

Uses the World Football Elo formula (eloratings.net, our Elo source): K=60 for World
Cup matches, the standard goal-difference multiplier, and a +100 home edge for a host
nation playing in its own country (neutral otherwise). Writes
`data/Ratings/Elo_Ratings_World_Cup_2026_CURRENT.csv`; predict.py prefers CURRENT over
the VERIFIED anchor when present.

Why roll our own instead of scraping eloratings.net: it's deterministic, dependency-
free, and reproducible from committed inputs (the VERIFIED 6/11 baseline + fixtures.csv),
and it implements eloratings' own WC formula — so CURRENT is a build artifact, not a new
source of truth. CURRENT is GITIGNORED and regenerated each nightly build (so it can never
destabilise the exact-value regression tests, which pin to the VERIFIED anchor).

Forward-only & leak-free: rolls through PLAYED games only, in kickoff order. Upcoming
predictions therefore use ratings current through PRIOR results — an unplayed game can't
leak into its own prediction — and already-logged calls are graded from the immutable
ledger, never recomputed. `--as-of DATE` rolls only through games that kicked off BEFORE
DATE (for reproducing a past as-of state).
"""
from __future__ import annotations
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as P
import ledger as L

REPO = Path(__file__).resolve().parent.parent
RATINGS = REPO / "data" / "Ratings"
BASELINE = RATINGS / P.ELO_FILE                 # VERIFIED 6/11 anchor (committed)
CURRENT = RATINGS / P.ELO_CURRENT               # rolled-forward output (gitignored)
FIXTURES = REPO / "data" / "fixtures.csv"

K_WC = 60.0            # World Football Elo K-factor for World Cup finals matches
ELO_HOME_ADV = 100.0   # eloratings.net home advantage (host nation at home; 0 at neutral)


def gd_mult(gd: int) -> float:
    """eloratings goal-difference weight: 1 for 0/1, 1.5 for 2, 1.75 for 3, then +1/8 each."""
    g = abs(gd)
    if g <= 1:
        return 1.0
    if g == 2:
        return 1.5
    if g == 3:
        return 1.75
    return 1.75 + (g - 3) / 8.0


def expected(ra: float, rb: float) -> float:
    """Elo expected score for A vs B (ratings already include any home-edge bonus)."""
    return 1.0 / (1.0 + 10 ** (-(ra - rb) / 400.0))


def _baseline() -> dict:
    with BASELINE.open(encoding="utf-8-sig", newline="") as f:
        return {P._canon(r["Team"]): float(r["Elo_Rating"]) for r in csv.DictReader(f)}


def played_games(as_of: str | None) -> list:
    """Played WC fixtures with valid scores, in kickoff order. as_of (YYYY-MM-DD) keeps
    only games that kicked off strictly before that ET date (leak-free reproduction)."""
    out = []
    with FIXTURES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("status") or "").strip().lower() != "played":
                continue
            try:
                sa, sb = int(r["score_a"]), int(r["score_b"])
            except (ValueError, TypeError):
                continue
            try:
                ko = L.kickoff_dt(r)
            except Exception:
                continue
            if as_of and ko.date().isoformat() >= as_of:
                continue
            out.append((ko, r, sa, sb))
    out.sort(key=lambda x: x[0])
    return out


def roll(as_of: str | None = None) -> tuple[dict, dict, int, str]:
    """Return (baseline, current_elo, n_games_applied, asof_date). Pure — no file write."""
    base = _baseline()
    elo = dict(base)
    games = played_games(as_of)
    n = 0
    last_date = ""
    for ko, r, sa, sb in games:
        a, b = P._canon(r["team_a"]), P._canon(r["team_b"])
        if a not in elo or b not in elo:
            continue
        host = P.HOST_BY_COUNTRY.get((r.get("country") or "").strip())
        ha = ELO_HOME_ADV if host == a else 0.0
        hb = ELO_HOME_ADV if host == b else 0.0
        wa = 1.0 if sa > sb else (0.5 if sa == sb else 0.0)
        delta = K_WC * gd_mult(sa - sb) * (wa - expected(elo[a] + ha, elo[b] + hb))
        elo[a] += delta
        elo[b] -= delta
        n += 1
        last_date = ko.date().isoformat()
    return base, elo, n, last_date


def write(elo: dict, asof: str) -> None:
    RATINGS.mkdir(parents=True, exist_ok=True)
    with CURRENT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Team", "Elo_Rating", "source", "asof"])
        for t in sorted(elo, key=lambda t: -elo[t]):
            w.writerow([t, f"{elo[t]:.0f}", "eloratings.net WC roll-forward (K=60)", asof])


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", help="roll only through games before this ET date (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="print the biggest movers, don't write")
    args = ap.parse_args()

    base, elo, n, last = roll(args.as_of)
    asof = args.as_of or last or "pre-tournament"
    movers = sorted(elo, key=lambda t: abs(elo[t] - base[t]), reverse=True)
    print(f"rolled {n} played game(s) onto the {BASELINE.name} baseline (asof {asof}).")
    print("biggest Elo moves:")
    for t in movers[:8]:
        d = elo[t] - base[t]
        if abs(d) < 0.5:
            break
        print(f"  {t:<20} {base[t]:.0f} -> {elo[t]:.0f}  ({d:+.0f})")
    if args.dry_run:
        print("(dry-run — CURRENT not written)")
        return
    write(elo, asof)
    print(f"wrote {CURRENT.name} ({len(elo)} teams). predict.py will prefer it over VERIFIED.")


if __name__ == "__main__":
    main()
