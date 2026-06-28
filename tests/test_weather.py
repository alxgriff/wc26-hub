"""Tests for scripts/weather.py — Phase 7 Sweat Factor.

No network calls: the fetch path uses an injectable opener; parse_openmeteo
is tested against a canned fixture; weather_log round-trips use a tmp file.
"""

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import weather as wx


class TestEstimateWbgt(unittest.TestCase):
    def _check(self, temp_c, rh_pct, expected, tol=0.05):
        result = wx.estimate_wbgt(temp_c, rh_pct)
        self.assertAlmostEqual(result, expected, delta=tol,
                               msg=f"WBGT({temp_c}°C, {rh_pct}%) expected {expected}, got {result:.4f}")

    def test_warm_humid(self):
        # 30°C, 60% RH — e = 0.6*6.105*exp(17.27*30/267.7)
        # e ≈ 0.6*6.105*exp(1.934) ≈ 0.6*6.105*6.917 ≈ 25.33
        # wbgt ≈ 0.567*30 + 0.393*25.33 + 3.94 ≈ 17.01+9.95+3.94 ≈ 30.9
        self._check(30.0, 60.0, 30.9, tol=0.1)

    def test_cool_dry(self):
        # 15°C, 40% RH — should be well below 20
        result = wx.estimate_wbgt(15.0, 40.0)
        self.assertLess(result, 20.0)
        self.assertGreater(result, 10.0)

    def test_mild_moderate(self):
        # 25°C, 50% RH — in the moderate range
        result = wx.estimate_wbgt(25.0, 50.0)
        self.assertGreater(result, 22.0)
        self.assertLess(result, 28.0)

    def test_zero_humidity(self):
        # 35°C, 0% RH — dry heat, e=0 → WBGT = 0.567*35 + 0 + 3.94
        expected = 0.567 * 35 + 3.94
        self._check(35.0, 0.0, expected, tol=0.01)


class TestSweatComponents(unittest.TestCase):
    def test_hotter_than_baseline(self):
        # Match WBGT 30, baseline 20 — positive delta, positive disadvantage
        mhi, delta, dis = wx.sweat_components(30.0, 20.0)
        self.assertAlmostEqual(delta, 10.0, places=5)
        self.assertGreater(dis, 0)
        self.assertGreater(mhi, 0)

    def test_cooler_than_baseline(self):
        # Match WBGT 20, baseline 30 — disadvantage must be 0
        mhi, delta, dis = wx.sweat_components(20.0, 30.0)
        self.assertAlmostEqual(delta, -10.0, places=5)
        self.assertAlmostEqual(dis, 0.0, places=5)

    def test_same_as_baseline(self):
        # No delta → disadvantage 0
        mhi, delta, dis = wx.sweat_components(25.0, 25.0)
        self.assertAlmostEqual(delta, 0.0, places=5)
        self.assertAlmostEqual(dis, 0.0, places=5)

    def test_mhi_clamped_at_bounds(self):
        # Below lo=18 → mhi = 0; above hi=32 → mhi = 100
        mhi_low, _, _ = wx.sweat_components(10.0, 10.0)   # wbgt 10 < lo 18
        self.assertAlmostEqual(mhi_low, 0.0, places=5)

        mhi_high, _, _ = wx.sweat_components(40.0, 10.0)  # wbgt 40 > hi 32
        self.assertAlmostEqual(mhi_high, 100.0, places=5)

    def test_disadvantage_clamped_at_bounds(self):
        # delta = 20 > dis_hi=10 → disadvantage clamped to 100
        _, _, dis = wx.sweat_components(35.0, 15.0)
        self.assertAlmostEqual(dis, 100.0, places=5)

    def test_mhi_normalization(self):
        # WBGT at midpoint (25 = midpoint of 18–32) → mhi ≈ 50
        mhi, _, _ = wx.sweat_components(25.0, 25.0)  # delta=0, just checking mhi
        # lo=18, hi=32: mhi = (25-18)/(32-18)*100 = 7/14*100 = 50
        self.assertAlmostEqual(mhi, 50.0, places=5)


class TestSweatFactor(unittest.TestCase):
    def test_both_zero(self):
        self.assertEqual(wx.sweat_factor(0.0, 0.0), 0)

    def test_both_max(self):
        self.assertEqual(wx.sweat_factor(100.0, 100.0), 100)

    def test_weighted_blend(self):
        # w_mhi=0.5, w_dis=0.5 → (60*0.5 + 40*0.5) = 50
        self.assertEqual(wx.sweat_factor(60.0, 40.0), 50)


class TestSeverityLabel(unittest.TestCase):
    def test_climate_controlled(self):
        self.assertEqual(wx.severity_label(80, True), "Indoors")
        self.assertEqual(wx.severity_label(0, True), "Indoors")

    def test_severe(self):
        self.assertEqual(wx.severity_label(75), "Severe")
        self.assertEqual(wx.severity_label(99), "Severe")

    def test_high(self):
        self.assertEqual(wx.severity_label(50), "High")
        self.assertEqual(wx.severity_label(74), "High")

    def test_moderate(self):
        self.assertEqual(wx.severity_label(25), "Moderate")
        self.assertEqual(wx.severity_label(49), "Moderate")

    def test_mild(self):
        self.assertEqual(wx.severity_label(0), "Mild")
        self.assertEqual(wx.severity_label(24), "Mild")


class TestClimateControlledClamp(unittest.TestCase):
    def _make_venue(self, air_conditioned: bool) -> wx.Venue:
        return wx.Venue(
            stadium="Test Arena",
            city="Test City",
            lat=30.0,
            lon=-90.0,
            roof="retractable",
            air_conditioned=air_conditioned,
        )

    def test_ac_venue_clamps_wbgt(self):
        venue = self._make_venue(True)
        # When building a log row for an AC venue, wbgt_est should be cc_wbgt
        raw = {"temp_c": 38, "rh_pct": 60, "wind_ms": 5, "solar_wm2": 900}
        from datetime import datetime, timezone
        utc = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)
        row = wx._build_log_row("T1", venue, utc, raw, "forecast", "now")
        self.assertEqual(float(row["wbgt_est"]), wx.CONFIG["cc_wbgt"])
        self.assertEqual(row["climate_controlled"], "true")

    def test_open_venue_uses_estimate(self):
        venue = self._make_venue(False)
        raw = {"temp_c": 30, "rh_pct": 60, "wind_ms": 5, "solar_wm2": 600}
        utc = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)
        row = wx._build_log_row("T2", venue, utc, raw, "forecast", "now")
        self.assertNotAlmostEqual(float(row["wbgt_est"]), wx.CONFIG["cc_wbgt"])
        self.assertEqual(row["climate_controlled"], "false")


class TestLoadVenues(unittest.TestCase):
    def _write_csv(self, tmpdir, content: str) -> Path:
        p = Path(tmpdir) / "venues.csv"
        p.write_text(content, encoding="utf-8")
        return p

    def test_load_all_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_csv(d, (
                "stadium,city,lat,lon,roof,air_conditioned\n"
                "NRG Stadium,Houston,29.685,-95.411,retractable,true\n"
                "MetLife Stadium,East Rutherford,40.814,-74.074,open,false\n"
            ))
            venues = wx.load_venues(p)
        self.assertIn("NRG Stadium", venues)
        self.assertTrue(venues["NRG Stadium"].air_conditioned)
        self.assertFalse(venues["MetLife Stadium"].air_conditioned)
        self.assertEqual(venues["NRG Stadium"].roof, "retractable")

    def test_duplicate_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_csv(d, (
                "stadium,city,lat,lon,roof,air_conditioned\n"
                "NRG Stadium,Houston,29.685,-95.411,retractable,true\n"
                "NRG Stadium,Houston,29.685,-95.411,retractable,true\n"
            ))
            with self.assertRaises(ValueError):
                wx.load_venues(p)

    def test_unknown_stadium_not_in_venues(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_csv(d, (
                "stadium,city,lat,lon,roof,air_conditioned\n"
                "Known Arena,City,30.0,-90.0,open,false\n"
            ))
            venues = wx.load_venues(p)
        self.assertNotIn("Unknown Arena", venues)
        self.assertIn("Known Arena", venues)


class TestLoadTeamClimate(unittest.TestCase):
    def _write_csv(self, tmpdir, content: str) -> Path:
        p = Path(tmpdir) / "team_climate.csv"
        p.write_text(content, encoding="utf-8")
        return p

    def test_load_with_wbgt(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_csv(d, (
                "team,baseline_lat,baseline_lon,baseline_wbgt,source,asof\n"
                "Norway,59.913,10.752,18.5,estimate,2026-06-13\n"
                "Qatar,25.286,51.533,34.5,estimate,2026-06-13\n"
            ))
            baselines = wx.load_team_climate(p)
        self.assertIn("Norway", baselines)
        self.assertAlmostEqual(baselines["Norway"].baseline_wbgt, 18.5)
        self.assertAlmostEqual(baselines["Qatar"].baseline_wbgt, 34.5)

    def test_load_with_empty_wbgt(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_csv(d, (
                "team,baseline_lat,baseline_lon,baseline_wbgt,source,asof\n"
                "Norway,59.913,10.752,,pending,\n"
            ))
            baselines = wx.load_team_climate(p)
        self.assertIsNone(baselines["Norway"].baseline_wbgt)

    def test_duplicate_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_csv(d, (
                "team,baseline_lat,baseline_lon,baseline_wbgt,source,asof\n"
                "Norway,59.913,10.752,18.5,estimate,2026-06-13\n"
                "Norway,59.913,10.752,18.5,estimate,2026-06-13\n"
            ))
            with self.assertRaises(ValueError):
                wx.load_team_climate(p)

    def test_unknown_team_not_in_baselines(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_csv(d, (
                "team,baseline_lat,baseline_lon,baseline_wbgt,source,asof\n"
                "Norway,59.913,10.752,18.5,estimate,2026-06-13\n"
            ))
            baselines = wx.load_team_climate(p)
        self.assertNotIn("Xanadu FC", baselines)


class TestParseOpenMeteo(unittest.TestCase):
    def _make_payload(self, hours: list[str], temps: list, rhs: list,
                      winds: list | None = None, solar: list | None = None) -> dict:
        h = {"time": hours, "temperature_2m": temps, "relative_humidity_2m": rhs}
        if winds is not None:
            h["wind_speed_10m"] = winds
        if solar is not None:
            h["shortwave_radiation"] = solar
        return {"hourly": h}

    def test_exact_hour_match(self):
        hours = [f"2026-06-15T{i:02d}:00" for i in range(24)]
        temps = [float(20 + i) for i in range(24)]
        rhs = [float(60 - i * 0.5) for i in range(24)]
        payload = self._make_payload(hours, temps, rhs)
        utc = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)
        result = wx.parse_openmeteo(payload, utc)
        self.assertAlmostEqual(result["temp_c"], 39.0)   # 20+19
        self.assertAlmostEqual(result["rh_pct"], 50.5)   # 60-9.5

    def test_missing_hour_raises(self):
        hours = ["2026-06-15T12:00", "2026-06-15T13:00"]
        payload = self._make_payload(hours, [25.0, 26.0], [70, 71])
        utc = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            wx.parse_openmeteo(payload, utc)

    def test_tz_conversion_respected(self):
        # Kickoff at 15:00 ET (UTC-4) = 19:00 UTC
        hours = [f"2026-06-15T{i:02d}:00" for i in range(24)]
        temps = [float(i) for i in range(24)]
        rhs = [50.0] * 24
        payload = self._make_payload(hours, temps, rhs, winds=[3.0] * 24)
        utc = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)
        result = wx.parse_openmeteo(payload, utc)
        self.assertAlmostEqual(result["temp_c"], 19.0)   # index 19
        self.assertAlmostEqual(result["wind_ms"], 3.0)

    def test_optional_fields_absent(self):
        hours = ["2026-06-20T20:00"]
        payload = self._make_payload(hours, [28.0], [75])
        utc = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)
        result = wx.parse_openmeteo(payload, utc)
        self.assertIsNone(result["wind_ms"])
        self.assertIsNone(result["solar_wm2"])


class TestKickoffToUtc(unittest.TestCase):
    def test_afternoon_et(self):
        # 15:00 ET (UTC-4) → 19:00 UTC
        utc = wx.kickoff_to_utc("2026-06-11", "15:00")
        self.assertEqual(utc.hour, 19)
        self.assertEqual(utc.date().isoformat(), "2026-06-11")

    def test_midnight_et(self):
        # 00:00 ET on June 20 → 04:00 UTC on June 20
        utc = wx.kickoff_to_utc("2026-06-20", "00:00")
        self.assertEqual(utc.hour, 4)
        self.assertEqual(utc.date().isoformat(), "2026-06-20")

    def test_late_evening_et(self):
        # 22:00 ET → 02:00 UTC next day
        utc = wx.kickoff_to_utc("2026-06-12", "22:00")
        self.assertEqual(utc.hour, 2)
        self.assertEqual(utc.date().isoformat(), "2026-06-13")


class TestWeatherLogRoundTrip(unittest.TestCase):
    def _row(self, match_id: str, source: str, temp: float = 28.0) -> dict:
        return {
            "match_id": match_id, "source": source,
            "temp_c": str(temp), "rh_pct": "70",
            "wind_ms": "4.5", "solar_wm2": "600",
            "wbgt_est": "28.50", "climate_controlled": "false",
            "as_of": "2026-06-13 07:00 ET",
        }

    def test_insert_and_retrieve(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "weather_log.csv"
            row = self._row("A1", "forecast")
            wx.upsert_weather_row(p, row)
            loaded = wx.load_weather_log(p)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["match_id"], "A1")
        self.assertEqual(loaded[0]["source"], "forecast")

    def test_upsert_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "weather_log.csv"
            row = self._row("B1", "forecast", temp=25.0)
            wx.upsert_weather_row(p, row)
            row2 = self._row("B1", "forecast", temp=27.0)  # update same key
            wx.upsert_weather_row(p, row2)
            loaded = wx.load_weather_log(p)
        # Key=(B1, forecast): must have exactly one row, updated temp
        matching = [r for r in loaded if r["match_id"] == "B1" and r["source"] == "forecast"]
        self.assertEqual(len(matching), 1)
        self.assertAlmostEqual(float(matching[0]["temp_c"]), 27.0)

    def test_different_sources_both_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "weather_log.csv"
            wx.upsert_weather_row(p, self._row("C1", "forecast"))
            wx.upsert_weather_row(p, self._row("C1", "actual"))
            loaded = wx.load_weather_log(p)
        keys = [(r["match_id"], r["source"]) for r in loaded]
        self.assertIn(("C1", "forecast"), keys)
        self.assertIn(("C1", "actual"), keys)
        self.assertEqual(len(loaded), 2)

    def test_multiple_matches(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "weather_log.csv"
            for mid in ("A1", "A2", "B1"):
                wx.upsert_weather_row(p, self._row(mid, "forecast"))
            loaded = wx.load_weather_log(p)
        self.assertEqual(len(loaded), 3)

    def test_nonexistent_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nonexistent.csv"
            result = wx.load_weather_log(p)
        self.assertEqual(result, [])


class TestToDict(unittest.TestCase):
    def _write_log(self, tmpdir, rows: list[dict]) -> Path:
        p = Path(tmpdir) / "weather_log.csv"
        if rows:
            with p.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=wx.LOG_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        return p

    def _row(self, match_id, source) -> dict:
        return {
            "match_id": match_id, "source": source,
            "temp_c": "30", "rh_pct": "70", "wind_ms": "5", "solar_wm2": "700",
            "wbgt_est": "30.00", "climate_controlled": "false",
            "as_of": "2026-06-13 07:00 ET",
        }

    def test_returns_none_when_no_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "no_log.csv"
            result = wx.to_dict("A1", p)
        self.assertIsNone(result)

    def test_returns_none_for_absent_match(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_log(d, [self._row("B1", "forecast")])
            result = wx.to_dict("A1", p)
        self.assertIsNone(result)

    def test_returns_dict_for_present_match(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write_log(d, [self._row("A1", "forecast")])
            result = wx.to_dict("A1", p)
        self.assertIsNotNone(result)
        self.assertEqual(result["match_id"], "A1")

    def test_prefers_actual_over_forecast(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [self._row("A1", "forecast"), self._row("A1", "actual")]
            rows[1]["temp_c"] = "32"  # actual has higher temp
            p = self._write_log(d, rows)
            result = wx.to_dict("A1", p)
        self.assertEqual(result["source"], "actual")
        self.assertEqual(result["temp_c"], "32")


class TestRenderEditionLine(unittest.TestCase):
    def _baseline(self, team: str, wbgt: float) -> wx.Baseline:
        return wx.Baseline(team=team, baseline_lat=0, baseline_lon=0,
                           baseline_wbgt=wbgt, source="estimate", asof="")

    def _wx(self, temp=30, rh=70, wbgt=None, cc=False, source="forecast"):
        return {
            "temp_c": str(temp),
            "rh_pct": str(rh),
            "wbgt_est": str(wbgt if wbgt is not None else wx.estimate_wbgt(temp, rh)),
            "climate_controlled": "true" if cc else "false",
            "source": source,
            "as_of": "2026-06-13 07:00 ET",
        }

    def test_climate_controlled_venue(self):
        wx_row = self._wx(cc=True)
        line = wx.render_edition_line("Norway", "Brazil", wx_row, {})
        self.assertIn("Indoors", line)
        self.assertIn("climate-controlled", line)

    def test_returns_string(self):
        baselines = {
            "Norway": self._baseline("Norway", 18.5),
            "Brazil": self._baseline("Brazil", 22.0),
        }
        wx_row = self._wx(temp=30, rh=70, wbgt=30.0)
        line = wx.render_edition_line("Norway", "Brazil", wx_row, baselines)
        self.assertIsInstance(line, str)
        self.assertGreater(len(line), 10)

    def test_disadvantage_phrase_appears(self):
        baselines = {
            "Norway": self._baseline("Norway", 18.5),
            "Ghana": self._baseline("Ghana", 29.0),
        }
        wx_row = self._wx(temp=30, rh=75, wbgt=30.0)
        line = wx.render_edition_line("Norway", "Ghana", wx_row, baselines)
        # Norway (baseline 18.5) is more disadvantaged vs Ghana (29.0) in 30° WBGT
        self.assertIn("Norway", line)

    def test_no_baselines_graceful(self):
        wx_row = self._wx(temp=28, rh=65, wbgt=28.0)
        line = wx.render_edition_line("TeamA", "TeamB", wx_row, {})
        self.assertIsInstance(line, str)

    def test_contains_wbgt_value(self):
        wx_row = self._wx(temp=30, rh=60, wbgt=27.0)
        line = wx.render_edition_line("Spain", "Morocco", wx_row, {})
        # WBGT formatted as integer in the line
        self.assertIn("27", line)

    def test_shape_stable(self):
        baselines = {"A": self._baseline("A", 20.0), "B": self._baseline("B", 25.0)}
        wx_row = self._wx(temp=30, rh=70, wbgt=30.0)
        line = wx.render_edition_line("A", "B", wx_row, baselines)
        self.assertTrue(line.startswith("Conditions"))
        self.assertIn("WBGT", line)


class TestFetchForecastInjector(unittest.TestCase):
    """Tests the network boundary via injectable opener — no real HTTP."""

    def _canned_payload(self, utc_hour: int = 19) -> bytes:
        hours = [f"2026-06-15T{h:02d}:00" for h in range(24)]
        payload = {
            "hourly": {
                "time": hours,
                "temperature_2m": [float(20 + h) for h in range(24)],
                "relative_humidity_2m": [float(60) for _ in range(24)],
                "wind_speed_10m": [5.0] * 24,
                "shortwave_radiation": [0.0] * 24,
            }
        }
        return json.dumps(payload).encode()

    def test_fetch_calls_opener_with_correct_url(self):
        calls: list[str] = []

        def fake_opener(url: str) -> bytes:
            calls.append(url)
            return self._canned_payload()

        utc = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)
        result = wx.fetch_forecast(29.685, -95.411, utc, opener=fake_opener)
        self.assertEqual(len(calls), 1)
        self.assertIn("latitude=29.685", calls[0])
        self.assertIn("longitude=-95.411", calls[0])
        self.assertEqual(result["temp_c"], 39.0)

    def test_fetch_raises_on_missing_hour(self):
        def fake_opener(url: str) -> bytes:
            payload = {
                "hourly": {
                    "time": ["2026-06-15T12:00"],
                    "temperature_2m": [25.0],
                    "relative_humidity_2m": [70],
                }
            }
            return json.dumps(payload).encode()

        utc = datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            wx.fetch_forecast(29.685, -95.411, utc, opener=fake_opener)


class TestLoadKnockout(unittest.TestCase):
    """weather._load_knockout: knockout.csv as fixtures-shaped rows for the weather pipeline."""

    def _write(self, body: str) -> Path:
        import tempfile
        p = Path(tempfile.mkdtemp()) / "knockout.csv"
        header = ("match_no,round,date_et,kickoff_et_24h,kickoff_et,stadium,city,country,"
                  "tv_us,team_a,team_b,score_a,score_b,decided_by,winner,status,notes\n")
        p.write_text(header + body, encoding="utf-8-sig")
        return p

    def test_shapes_rows_with_kickoff_utc(self):
        p = self._write("73,R32,2026-06-28,15:00,3:00 PM,SoFi Stadium,Inglewood,USA,,"
                        "South Africa,Canada,,,,,scheduled,\n")
        rows = wx._load_knockout(p)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["match_id"], "M73")
        self.assertEqual(r["stadium"], "SoFi Stadium")
        self.assertEqual(r["status"], "scheduled")
        self.assertEqual(r["_kickoff_utc"], wx.kickoff_to_utc("2026-06-28", "15:00"))

    def test_missing_file_returns_empty(self):
        self.assertEqual(wx._load_knockout(Path("does/not/exist/knockout.csv")), [])


if __name__ == "__main__":
    unittest.main()
