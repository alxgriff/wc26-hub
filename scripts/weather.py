#!/usr/bin/env python3
"""WC26 weather module — Phase 7 Sweat Factor.

Fetches match-time weather from Open-Meteo (CC BY 4.0, no API key needed)
and computes per-match heat stress (estimated WBGT) + per-team climate
disadvantage relative to their home city baseline.

Pure functions (no network; unit-tested):
    load_venues(path)                     -> dict[str, Venue]
    load_team_climate(path)               -> dict[str, Baseline]
    estimate_wbgt(temp_c, rh_pct)         -> float
    sweat_components(match_wbgt, base)    -> tuple[float, float, float]
    sweat_factor(mhi, disadvantage)       -> int  (0-100)
    severity_label(sf, cc)                -> str
    render_edition_line(team_a, team_b, wx, baselines) -> str
    to_dict(match_id, log_path)           -> dict | None

Network-boundary (injectable opener for testing):
    fetch_forecast(lat, lon, kickoff_utc, *, opener)  -> dict
    parse_openmeteo(payload, kickoff_utc)              -> dict (TESTED w/ fixture)
    load_weather_log(path)                             -> list[dict]
    upsert_weather_row(path, row)                      -> None

CLI:
    python scripts/weather.py --date YYYY-MM-DD   fetch/upsert forecast rows
    python scripts/weather.py --baselines          one-shot historical baseline build
    python scripts/weather.py --backfill           write actual rows for played matches
    (no args)                                       render today's slate to stdout

Attribution: Weather data by Open-Meteo.com (CC BY 4.0).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import exp
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")

CONFIG = {
    "cc_wbgt": 21.0,    # climate-controlled WBGT clamp (°C)
    "mhi_lo": 18,        # match heat index normalization lower bound (°C WBGT)
    "mhi_hi": 32,        # match heat index normalization upper bound (°C WBGT)
    "dis_lo": 0,         # disadvantage lower bound (delta °C)
    "dis_hi": 10,        # disadvantage upper bound (delta °C)
    "w_mhi": 0.5,        # weight of MHI in sweat factor
    "w_dis": 0.5,        # weight of disadvantage in sweat factor
}

SEVERITY_THRESHOLDS = [(75, "Severe"), (50, "High"), (25, "Moderate"), (0, "Mild")]

BASELINE_YEARS = [2022, 2023, 2024]  # historical years for --baselines pull

_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,shortwave_radiation"
    "&timezone=GMT&start_date={date}&end_date={date}"
)
_ARCHIVE_URL = (
    "https://archive-api.open-meteo.com/v1/archive"
    "?latitude={lat}&longitude={lon}"
    "&hourly=temperature_2m,relative_humidity_2m"
    "&timezone=GMT&start_date={start}&end_date={end}"
)

LOG_COLUMNS = [
    "match_id", "source", "temp_c", "rh_pct", "wind_ms",
    "solar_wm2", "wbgt_est", "climate_controlled", "as_of",
]
BASELINE_COLUMNS = ["team", "baseline_lat", "baseline_lon", "baseline_wbgt", "source", "asof"]


# ---------------------------------------------------------------- data types

@dataclass(frozen=True)
class Venue:
    stadium: str
    city: str
    lat: float
    lon: float
    roof: str           # "open" | "retractable" | "canopy"
    air_conditioned: bool


@dataclass(frozen=True)
class Baseline:
    team: str
    baseline_lat: float
    baseline_lon: float
    baseline_wbgt: float | None  # None until --baselines has been run
    source: str
    asof: str


# ---------------------------------------------------------------- loaders (pure)

def load_venues(path: str | Path) -> dict[str, Venue]:
    """Load venues.csv keyed by stadium (exact string). Raises ValueError on duplicate."""
    path = Path(path)
    venues: dict[str, Venue] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = row["stadium"]
            if key in venues:
                raise ValueError(f"duplicate stadium in venues.csv: {key!r}")
            venues[key] = Venue(
                stadium=key,
                city=row["city"],
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                roof=row["roof"],
                air_conditioned=row["air_conditioned"].strip().lower() == "true",
            )
    return venues


def load_team_climate(path: str | Path) -> dict[str, Baseline]:
    """Load team_climate.csv keyed by team name (exact canon string).
    Raises ValueError on duplicate. baseline_wbgt may be None if pending."""
    path = Path(path)
    baselines: dict[str, Baseline] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = row["team"]
            if key in baselines:
                raise ValueError(f"duplicate team in team_climate.csv: {key!r}")
            raw = (row.get("baseline_wbgt") or "").strip()
            baselines[key] = Baseline(
                team=key,
                baseline_lat=float(row["baseline_lat"]),
                baseline_lon=float(row["baseline_lon"]),
                baseline_wbgt=float(raw) if raw else None,
                source=row.get("source", ""),
                asof=row.get("asof", ""),
            )
    return baselines


# ---------------------------------------------------------------- pure math

def estimate_wbgt(temp_c: float, rh_pct: float) -> float:
    """BOM shade WBGT approximation (temperature + humidity; no solar term).

    e    = (rh/100) * 6.105 * exp(17.27 * T / (237.7 + T))
    wbgt = 0.567 * T + 0.393 * e + 3.94
    """
    e = (rh_pct / 100.0) * 6.105 * exp(17.27 * temp_c / (237.7 + temp_c))
    return 0.567 * temp_c + 0.393 * e + 3.94


def _norm(x: float, lo: float, hi: float) -> float:
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))


def sweat_components(
    match_wbgt: float,
    baseline_wbgt: float,
) -> tuple[float, float, float]:
    """Compute (mhi_0_100, delta_c, disadvantage_0_100) for one team in one match.

    mhi_0_100:         normalized match heat index (0 = below floor, 100 = extreme)
    delta_c:           match_wbgt - baseline_wbgt (negative = cooler than home)
    disadvantage_0_100: how unprepared this team is for the heat (0 if cooler/equal)
    """
    mhi = _norm(match_wbgt, CONFIG["mhi_lo"], CONFIG["mhi_hi"])
    delta = match_wbgt - baseline_wbgt
    disadvantage = _norm(max(delta, 0.0), CONFIG["dis_lo"], CONFIG["dis_hi"])
    return (mhi, delta, disadvantage)


def sweat_factor(mhi: float, disadvantage: float) -> int:
    """Weighted blend of match heat index and disadvantage → integer 0..100."""
    return round(CONFIG["w_mhi"] * mhi + CONFIG["w_dis"] * disadvantage)


def severity_label(sf: int, climate_controlled: bool = False) -> str:
    if climate_controlled:
        return "Indoors"
    for threshold, label in SEVERITY_THRESHOLDS:
        if sf >= threshold:
            return label
    return "Mild"


# ---------------------------------------------------------------- rendering (pure)

def _humidity_desc(rh_pct: float) -> str:
    if rh_pct >= 70:
        return "humid"
    if rh_pct >= 40:
        return "moderate humidity"
    return "dry"


def render_edition_line(
    team_a: str,
    team_b: str,
    wx: dict,
    baselines: dict[str, Baseline],
) -> str:
    """One factual conditions sentence for the edition's Today's slate.
    Example: 'Conditions (forecast): ~31°C, humid — WBGT 29 (Severe). Big disadvantage for Norway.'
    """
    cc = str(wx.get("climate_controlled", "")).strip().lower() == "true"
    if cc:
        return "Conditions: Indoors — climate-controlled. Heat not a factor."

    temp = float(wx["temp_c"])
    rh = float(wx["rh_pct"])
    wbgt = float(wx["wbgt_est"])
    as_of = (wx.get("as_of") or "").strip()
    source = (wx.get("source") or "forecast").strip()

    dis_a = dis_b = 0.0
    for team, slot in ((team_a, "a"), (team_b, "b")):
        bl = baselines.get(team)
        if bl and bl.baseline_wbgt is not None:
            _, _, dis = sweat_components(wbgt, bl.baseline_wbgt)
            if slot == "a":
                dis_a = dis
            else:
                dis_b = dis

    mhi_val = _norm(wbgt, CONFIG["mhi_lo"], CONFIG["mhi_hi"])
    sf = sweat_factor(mhi_val, max(dis_a, dis_b))
    label = severity_label(sf)

    dis_note = ""
    if dis_a >= 60 or dis_b >= 60:
        worst = team_a if dis_a >= dis_b else team_b
        dis_note = f" Big disadvantage for {worst}."
    elif dis_a >= 30 or dis_b >= 30:
        worst = team_a if dis_a >= dis_b else team_b
        dis_note = f" Some disadvantage for {worst}."

    src_tag = f" ({source})" if source and source != "forecast" else ""
    return (
        f"Conditions{src_tag}: ~{temp:.0f}°C, {_humidity_desc(rh)} — "
        f"WBGT {wbgt:.0f} ({label}).{dis_note}"
    )


def to_dict(
    match_id: str,
    log_path: str | Path | None = None,
) -> dict | None:
    """Return the best available weather row for match_id, or None (placeholder).

    Prefers 'actual' over 'forecast'. Returns None when no row exists yet,
    signalling the caller to display a placeholder — never invented data.
    """
    if log_path is None:
        log_path = REPO_ROOT / "data" / "weather_log.csv"
    rows = load_weather_log(log_path)
    for source in ("actual", "forecast"):
        for r in rows:
            if r["match_id"] == match_id and r["source"] == source:
                return dict(r)
    return None


# ---------------------------------------------------------------- network boundary

def _default_opener(url: str) -> bytes:
    from urllib.request import urlopen
    with urlopen(url, timeout=20) as resp:
        return resp.read()


def fetch_forecast(
    lat: float,
    lon: float,
    kickoff_utc: datetime,
    *,
    opener: Callable[[str], bytes] = _default_opener,
) -> dict:
    """Fetch one hourly row from Open-Meteo forecast for the kickoff UTC hour.
    Returns the parsed row dict (temp_c, rh_pct, wind_ms, solar_wm2).
    Raises ValueError if the kickoff hour is outside the 16-day forecast horizon.
    """
    date_str = kickoff_utc.strftime("%Y-%m-%d")
    url = _FORECAST_URL.format(lat=lat, lon=lon, date=date_str)
    raw = opener(url)
    payload = json.loads(raw)
    return parse_openmeteo(payload, kickoff_utc)


def fetch_actual(
    lat: float,
    lon: float,
    kickoff_utc: datetime,
    *,
    opener: Callable[[str], bytes] = _default_opener,
) -> dict:
    """Fetch historical weather from Open-Meteo archive for a past kickoff.
    Uses the archive endpoint; date must be in the past (at least 5 days ago).
    """
    date_str = kickoff_utc.strftime("%Y-%m-%d")
    url = _ARCHIVE_URL.format(lat=lat, lon=lon, start=date_str, end=date_str)
    raw = opener(url)
    payload = json.loads(raw)
    return parse_openmeteo(payload, kickoff_utc)


def parse_openmeteo(payload: dict, kickoff_utc: datetime) -> dict:
    """Extract one hourly row from an Open-Meteo JSON payload.

    kickoff_utc must be a UTC datetime (timezone-aware or naive-treated-as-UTC).
    Matches the hour string "YYYY-MM-DDTHH:00" in the hourly.time array.
    Raises ValueError if the target hour is absent.
    """
    hourly = payload["hourly"]
    times = hourly["time"]
    target = kickoff_utc.strftime("%Y-%m-%dT%H:00")
    try:
        idx = times.index(target)
    except ValueError:
        span = f"{times[0]}..{times[-1]}" if times else "empty"
        raise ValueError(
            f"kickoff hour {target!r} not found in Open-Meteo payload (range: {span})"
        )
    temps = hourly["temperature_2m"]
    rhs = hourly["relative_humidity_2m"]
    winds = hourly.get("wind_speed_10m") or []
    solar = hourly.get("shortwave_radiation") or []
    return {
        "temp_c": temps[idx],
        "rh_pct": rhs[idx],
        "wind_ms": winds[idx] if idx < len(winds) else None,
        "solar_wm2": solar[idx] if idx < len(solar) else None,
    }


# ---------------------------------------------------------------- weather log CSV

def load_weather_log(path: str | Path) -> list[dict]:
    """Read weather_log.csv. Returns [] if file does not exist."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def upsert_weather_row(path: str | Path, row: dict) -> None:
    """Insert or update a row in weather_log.csv. Upsert key = (match_id, source).
    Re-running with the same key updates the existing row; never double-logs.
    """
    path = Path(path)
    rows = load_weather_log(path)
    key = (row["match_id"], row["source"])
    updated = False
    for i, r in enumerate(rows):
        if (r["match_id"], r["source"]) == key:
            rows[i] = {k: row.get(k, r.get(k, "")) for k in LOG_COLUMNS}
            updated = True
            break
    if not updated:
        rows.append({k: row.get(k, "") for k in LOG_COLUMNS})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------- kickoff → UTC

def kickoff_to_utc(date_et_str: str, kickoff_24h: str) -> datetime:
    """Convert a fixtures.csv kickoff (ET calendar date + 24h time) to UTC.

    Handles the EDT (UTC-4) offset for June/July. Uses stdlib zoneinfo so
    DST transitions are handled by the OS timezone database, not hardcoded.
    """
    naive = datetime.strptime(f"{date_et_str} {kickoff_24h}", "%Y-%m-%d %H:%M")
    et_aware = naive.replace(tzinfo=ET)
    return et_aware.astimezone(timezone.utc)


# ---------------------------------------------------------------- build log row

def _build_log_row(
    match_id: str,
    venue: Venue,
    kickoff_utc: datetime,
    wx_data: dict,
    source: str,
    as_of: str,
) -> dict:
    cc = venue.air_conditioned
    if cc:
        wbgt = CONFIG["cc_wbgt"]
    else:
        temp = float(wx_data["temp_c"])
        rh = float(wx_data["rh_pct"])
        wbgt = estimate_wbgt(temp, rh)
    return {
        "match_id": match_id,
        "source": source,
        "temp_c": wx_data["temp_c"],
        "rh_pct": wx_data["rh_pct"],
        "wind_ms": wx_data.get("wind_ms") or "",
        "solar_wm2": wx_data.get("solar_wm2") or "",
        "wbgt_est": f"{wbgt:.2f}",
        "climate_controlled": "true" if cc else "false",
        "as_of": as_of,
    }


# ---------------------------------------------------------------- fixture loader (thin wrapper)

def _load_fixtures(fixtures_path: Path) -> list[dict]:
    """Read fixtures.csv rows. Adds _editorial and _kickoff_utc helper keys."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import build_edition as be  # noqa: avoid circular at module level
    rows = be.read_rows(fixtures_path)
    for r in rows:
        if r.get("kickoff_et_24h"):
            try:
                r["_kickoff_utc"] = kickoff_to_utc(
                    r["date_et"].strip(), r["kickoff_et_24h"].strip()
                )
            except ValueError:
                r["_kickoff_utc"] = None
        else:
            r["_kickoff_utc"] = None
    return rows


# ---------------------------------------------------------------- CLI modes

def _run_date(
    target_date: date,
    venues: dict[str, Venue],
    fixtures_path: Path,
    log_path: Path,
    opener: Callable[[str], bytes] = _default_opener,
) -> int:
    """Fetch forecast rows for all matches on target editorial date."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import build_edition as be  # noqa
    rows = _load_fixtures(fixtures_path)
    today = be.select_matches(rows, target_date)
    if not today:
        print(f"No matches on {target_date}.", file=sys.stderr)
        return 0

    as_of = datetime.now(tz=ET).strftime("%Y-%m-%d %H:%M ET")
    ok = errors = 0
    for r in today:
        mid = r["match_id"]
        stadium = (r.get("stadium") or "").strip()
        if stadium not in venues:
            print(f"ERROR: stadium {stadium!r} (match {mid}) not in venues.csv — stop",
                  file=sys.stderr)
            errors += 1
            continue
        venue = venues[stadium]
        utc = r.get("_kickoff_utc")
        if utc is None:
            print(f"warning: {mid} has no valid kickoff time — skipped", file=sys.stderr)
            continue
        try:
            wx_data = fetch_forecast(venue.lat, venue.lon, utc, opener=opener)
        except ValueError as e:
            print(f"warning: {mid} — {e} (likely beyond 16-day window)", file=sys.stderr)
            continue
        except Exception as e:
            print(f"warning: {mid} — fetch failed: {e}", file=sys.stderr)
            errors += 1
            continue
        row = _build_log_row(mid, venue, utc, wx_data, "forecast", as_of)
        upsert_weather_row(log_path, row)
        cc_note = " (CC — clamped)" if venue.air_conditioned else ""
        print(f"{mid}: {row['wbgt_est']}°C WBGT, {row['temp_c']}°C, {row['rh_pct']}% RH{cc_note}")
        ok += 1

    if errors:
        return 1
    return 0


def _run_backfill(
    venues: dict[str, Venue],
    fixtures_path: Path,
    log_path: Path,
    opener: Callable[[str], bytes] = _default_opener,
) -> int:
    """Write 'actual' rows for played matches using the archive API."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    rows = _load_fixtures(fixtures_path)
    played = [r for r in rows if (r.get("status") or "").strip().lower() == "played"]
    if not played:
        print("No played matches found.", file=sys.stderr)
        return 0

    as_of = datetime.now(tz=ET).strftime("%Y-%m-%d %H:%M ET")
    ok = errors = 0
    for r in played:
        mid = r["match_id"]
        stadium = (r.get("stadium") or "").strip()
        if stadium not in venues:
            print(f"ERROR: stadium {stadium!r} (match {mid}) not in venues.csv", file=sys.stderr)
            errors += 1
            continue
        venue = venues[stadium]
        utc = r.get("_kickoff_utc")
        if utc is None:
            continue
        # Archive API requires data to be at least a few days old
        try:
            wx_data = fetch_actual(venue.lat, venue.lon, utc, opener=opener)
        except Exception as e:
            print(f"warning: {mid} backfill failed: {e}", file=sys.stderr)
            continue
        row = _build_log_row(mid, venue, utc, wx_data, "actual", as_of)
        upsert_weather_row(log_path, row)
        print(f"{mid} (actual): {row['wbgt_est']}°C WBGT")
        ok += 1

    return 1 if errors else 0


def _run_baselines(
    climate_path: Path,
    opener: Callable[[str], bytes] = _default_opener,
) -> int:
    """One-shot: fetch June–July historical archive for each team's capital city
    and compute mean WBGT. Updates baseline_wbgt + source + asof in team_climate.csv.
    """
    baselines = load_team_climate(climate_path)
    as_of = datetime.now(tz=ET).strftime("%Y-%m-%d")
    updated: dict[str, Baseline] = {}

    for team, bl in baselines.items():
        wbgts: list[float] = []
        for year in BASELINE_YEARS:
            start = f"{year}-06-01"
            end = f"{year}-07-31"
            url = _ARCHIVE_URL.format(lat=bl.baseline_lat, lon=bl.baseline_lon,
                                      start=start, end=end)
            try:
                raw = opener(url)
                payload = json.loads(raw)
            except Exception as e:
                print(f"warning: {team} {year} archive fetch failed: {e}", file=sys.stderr)
                continue
            hourly = payload.get("hourly", {})
            temps = hourly.get("temperature_2m", [])
            rhs = hourly.get("relative_humidity_2m", [])
            for t, rh in zip(temps, rhs):
                if t is not None and rh is not None:
                    wbgts.append(estimate_wbgt(float(t), float(rh)))
        if not wbgts:
            print(f"warning: {team} — no archive data; baseline unchanged", file=sys.stderr)
            updated[team] = bl
            continue
        mean_wbgt = sum(wbgts) / len(wbgts)
        updated[team] = Baseline(
            team=team,
            baseline_lat=bl.baseline_lat,
            baseline_lon=bl.baseline_lon,
            baseline_wbgt=round(mean_wbgt, 1),
            source="openmeteo-archive",
            asof=as_of,
        )
        print(f"{team}: baseline WBGT {mean_wbgt:.1f}°C (n={len(wbgts)})")

    climate_path.parent.mkdir(parents=True, exist_ok=True)
    with climate_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=BASELINE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for team, bl in updated.items():
            writer.writerow({
                "team": bl.team,
                "baseline_lat": bl.baseline_lat,
                "baseline_lon": bl.baseline_lon,
                "baseline_wbgt": bl.baseline_wbgt if bl.baseline_wbgt is not None else "",
                "source": bl.source,
                "asof": bl.asof,
            })
    return 0


def _run_render(
    target_date: date,
    fixtures_path: Path,
    log_path: Path,
    climate_path: Path,
) -> int:
    """Render today's conditions to stdout for a sanity check (no network)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import build_edition as be  # noqa
    rows = _load_fixtures(fixtures_path)
    today = be.select_matches(rows, target_date)
    baselines = load_team_climate(climate_path)

    if not today:
        print(f"No matches on {target_date}.")
        return 0

    for r in today:
        mid = r["match_id"]
        team_a, team_b = r["team_a"].strip(), r["team_b"].strip()
        wx = to_dict(mid, log_path)
        if wx is None:
            print(f"{mid} {team_a} vs {team_b}: forecast pending")
        else:
            line = render_edition_line(team_a, team_b, wx, baselines)
            print(f"{mid}: {line}")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="WC26 weather fetch + Sweat Factor computation (Phase 7).")
    ap.add_argument("--date", metavar="YYYY-MM-DD",
                    help="editorial date to fetch forecasts for")
    ap.add_argument("--baselines", action="store_true",
                    help="one-shot historical baseline build for all 48 teams")
    ap.add_argument("--backfill", action="store_true",
                    help="write actual rows for played matches via archive API")
    ap.add_argument("--fixtures", type=Path,
                    default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--log", type=Path,
                    default=REPO_ROOT / "data" / "weather_log.csv")
    ap.add_argument("--climate", type=Path,
                    default=REPO_ROOT / "data" / "team_climate.csv")
    ap.add_argument("--venues", type=Path,
                    default=REPO_ROOT / "data" / "venues.csv")
    args = ap.parse_args(argv)

    venues_path: Path = args.venues
    log_path: Path = args.log
    climate_path: Path = args.climate
    fixtures_path: Path = args.fixtures

    try:
        venues = load_venues(venues_path)
    except Exception as e:
        print(f"error: could not load venues.csv: {e}", file=sys.stderr)
        return 1

    if args.baselines:
        return _run_baselines(climate_path)

    if args.backfill:
        return _run_backfill(venues, fixtures_path, log_path)

    if args.date:
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            return 2
        return _run_date(target, venues, fixtures_path, log_path)

    # Default: render today's slate
    today = datetime.now(tz=ET).date()
    return _run_render(today, fixtures_path, log_path, climate_path)


if __name__ == "__main__":
    raise SystemExit(main())
