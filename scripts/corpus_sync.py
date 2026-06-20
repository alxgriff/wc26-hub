"""corpus_sync.py — fold played WC2026 results into the historical corpus for the lever-fits.

data/History/results.csv (martj42, CC0, gitignored) is a periodic snapshot that lags the
tournament — the 2026-06-20 audit found it held scores for only 4 of 28 played WC games, so
fit_rho.py / fit_hfa.py (which read it via load_curated) were not actually folding the
tournament in. merge_wc() upserts each PLAYED fixtures.csv game as a corpus-format row
(replacing any stale/NA row for the same fixture), IN MEMORY — the snapshot file is never
mutated (a re-fetch would clobber it). Names are the project canon, which the corpus already
matches after predict._canon. Leak-safe for the fits: they estimate corpus-wide rho / HFA,
they don't predict specific games.
"""
from __future__ import annotations
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as P
import ledger as L

FIXTURES = P.FIXTURES
WC_TOURNAMENT = "FIFA World Cup"


def wc_rows(fixtures: "str | Path" = FIXTURES) -> list[dict]:
    """Played WC2026 fixtures as corpus-format rows (date/home_team/away_team/scores/
    tournament/city/country/neutral). neutral=FALSE only when a host plays in its country."""
    out = []
    with open(fixtures, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("status") or "").strip().lower() != "played":
                continue
            try:
                hs, as_ = int(r["score_a"]), int(r["score_b"])
            except (ValueError, TypeError):
                continue
            try:
                d = L.kickoff_dt(r).date().isoformat()
            except Exception:
                continue
            a, b = P._canon(r["team_a"]), P._canon(r["team_b"])
            host = P.HOST_BY_COUNTRY.get((r.get("country") or "").strip())
            out.append({"date": d, "home_team": a, "away_team": b,
                        "home_score": str(hs), "away_score": str(as_),
                        "tournament": WC_TOURNAMENT, "city": (r.get("city") or ""),
                        "country": (r.get("country") or ""),
                        "neutral": "FALSE" if host in (a, b) else "TRUE"})
    return out


def merge_wc(rows: list[dict], fixtures: "str | Path" = FIXTURES) -> tuple[list[dict], int]:
    """Upsert played WC games into corpus ``rows`` — drop any existing row for the same
    fixture (NA or stale) and append the authoritative played version. Returns
    (merged, n_wc). dedup key = (date, canon home, canon away)."""
    wc = wc_rows(fixtures)
    keys = {(w["date"], w["home_team"], w["away_team"]) for w in wc}
    kept = [r for r in rows
            if (r.get("date"), P._canon(r.get("home_team", "")),
                P._canon(r.get("away_team", ""))) not in keys]
    return kept + wc, len(wc)


if __name__ == "__main__":
    n = len(wc_rows())
    print(f"{n} played WC2026 games available to fold into the corpus (in-memory; "
          f"fit_rho/fit_hfa apply this automatically via load_curated).")
