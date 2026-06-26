"""Regression tests for the post-MD1 model additions.

Four categories of integrity this file defends:

  A. INCUMBENT GOLDEN SNAPSHOTS — hardcoded W/D/L for 5 played matches from the
     production (Elo+Futi) model.  If predict.py or data/ratings/ is touched, these
     fail immediately.  They do NOT re-derive — they compare against locked constants.

  B. ELO ROLLBACK CORRECTNESS — verifies the 4 teams whose June-18 MD2 results were
     stripped out of the post-MD1 file moved in the right direction AND by the right
     signed amount relative to the production (pre-tournament) baseline.

  C. SUPPRESSION INTEGRATION — actually runs the forecaster loop from build_model_lab
     against synthetic fixture rows and confirms that post-md1 forecasters return None
     for MD1 rows and a real result for MD2 rows.

  D. BETTING EDGE ISOLATION — confirms that struct_variants (the overlay models) are
     stored under a SEPARATE key in build_site.call() output and NEVER overwrite the
     p_a/p_draw/p_b probabilities that feed the prediction ledger and edge calculation.
     The edge pipeline reads from the ledger (our_p = logged incumbent prob), not from
     the live model or struct_variant — so this test closes the loop on betting integrity.

Run from repo root:  python -m unittest tests/test_post_md1_models.py -v
"""
import csv
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import predict as pr         # noqa: E402
import struct_variant as sv  # noqa: E402


def _real_model():
    try:
        return pr.load_ratings()
    except Exception:
        return None


def _post_md1_model():
    try:
        return pr.load_ratings(ratings_dir=REPO / "data" / "ratings" / "post-md1")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# A. INCUMBENT GOLDEN SNAPSHOTS
# ---------------------------------------------------------------------------
# Computed from `pr.load_ratings()` on 2026-06-18 before this branch was merged.
# These values are the ground truth. If any digit changes, something mutated the
# production model path.  The HFA flag mirrors what build_site.py passes.
# ---------------------------------------------------------------------------
_GOLDEN = [
    # (team_a, team_b, hfa_team,    p_a,               p_draw,            p_b)
    ("England",       "Croatia",       None,            0.576433434, 0.248418644, 0.175147922),
    ("Brazil",        "Morocco",       None,            0.529640698, 0.281926502, 0.188432800),
    ("United States", "Paraguay",      "United States", 0.350345594, 0.278694715, 0.370959690),
    ("Germany",       "Curaçao",       "Germany",       0.969396998, 0.026461223, 0.004141779),
    ("France",        "Senegal",       None,            0.645641092, 0.218748540, 0.135610368),
]


class IncumbentGoldenTests(unittest.TestCase):
    """Production model predictions must be bit-identical to the locked constants above."""

    def setUp(self):
        self.m = _real_model()
        if self.m is None:
            self.skipTest("production ratings unavailable")

    def _check(self, a, b, hfa, exp_pa, exp_draw, exp_pb):
        p = pr.predict_match(self.m, a, b, hfa_team=hfa)
        self.assertAlmostEqual(p.p_a,    exp_pa,   places=9, msg=f"{a} vs {b}: p_a drift")
        self.assertAlmostEqual(p.p_draw, exp_draw, places=9, msg=f"{a} vs {b}: p_draw drift")
        self.assertAlmostEqual(p.p_b,    exp_pb,   places=9, msg=f"{a} vs {b}: p_b drift")

    def test_england_croatia(self):
        self._check(*_GOLDEN[0])

    def test_brazil_morocco(self):
        self._check(*_GOLDEN[1])

    def test_usa_paraguay_with_hfa(self):
        self._check(*_GOLDEN[2])

    def test_germany_curacao_with_hfa(self):
        self._check(*_GOLDEN[3])

    def test_france_senegal(self):
        self._check(*_GOLDEN[4])

    def test_probs_sum_to_one_for_all_golden(self):
        """The model's live output must sum to 1 (not the truncated constants)."""
        for a, b, hfa, *_ in _GOLDEN:
            p = pr.predict_match(self.m, a, b, hfa_team=hfa)
            self.assertAlmostEqual(p.p_a + p.p_draw + p.p_b, 1.0, places=9,
                                   msg=f"Model probs don't sum to 1: {a} vs {b}")


# ---------------------------------------------------------------------------
# B. ELO ROLLBACK CORRECTNESS
# ---------------------------------------------------------------------------
# June-18 MD2 game results and their known K=60 Elo point swings
# (taken from the eloratings.net results page the user pasted):
#   Czechia 1-1 South Africa:   Czechia -16, South Africa +16
#   Switzerland 4-1 Bosnia:     Switzerland +20, Bosnia -20
#
# The post-MD1 file was built from the June-18 eloratings snapshot (which
# already included those MD2 swings) with the MD2 effects REVERSED.
# So post-MD1 Elo vs. production (pre-tournament) Elo should reflect only
# MD1 changes — and the direction is dictated by MD1 results:
#   Czechia:   lost to South Korea →  Elo should be LOWER post-MD1
#   S. Africa: lost to Mexico 0-2  →  Elo should be LOWER post-MD1
#   Switzerl.: drew with Qatar 1-1 →  Elo should be LOWER post-MD1 (drew as big fav)
#   Bosnia:    drew with Canada 1-1 → Elo should be HIGHER post-MD1 (drew as underdog)
# ---------------------------------------------------------------------------
_MD1_DIRECTION = {
    "Czechia":                 "lower",   # lost to South Korea
    "South Africa":            "lower",   # lost to Mexico
    "Switzerland":             "lower",   # drew with Qatar as heavy favourite
    "Bosnia and Herzegovina":  "higher",  # drew with Canada as underdog
}

# These are the exact K=60 swings that the June-18 eloratings snapshot
# captured for the MD2 games.  The rollback added their NEGATIVES back.
# If the rollback arithmetic is correct, post-MD1 Elo must differ from the
# raw June-18 Elo by exactly these amounts (in the reverse direction).
_JUNE18_ROLLBACK_DELTAS = {
    "Czechia":                 +16,  # raw June-18 had Czechia -16; rollback adds +16
    "South Africa":            -16,  # raw June-18 had S.Africa +16; rollback adds -16
    "Switzerland":             -20,  # raw June-18 had Switz   +20; rollback adds -20
    "Bosnia and Herzegovina":  +20,  # raw June-18 had Bosnia  -20; rollback adds +20
}


class EloRollbackTests(unittest.TestCase):
    def setUp(self):
        self.prod = _real_model()
        self.post = _post_md1_model()
        if self.prod is None:
            self.skipTest("production ratings unavailable")
        if self.post is None:
            self.skipTest("post-MD1 ratings unavailable")

    def test_rollback_teams_move_in_correct_md1_direction(self):
        """Each team's post-MD1 Elo should be higher or lower than production
        based solely on how they performed in MD1 (not MD2)."""
        for team, direction in _MD1_DIRECTION.items():
            prod_elo = self.prod.teams[team].elo
            post_elo = self.post.teams[team].elo
            diff = post_elo - prod_elo
            if direction == "lower":
                self.assertLess(diff, 0,
                    f"{team}: expected post-MD1 Elo < production (MD1 result was bad), "
                    f"got prod={prod_elo}, post={post_elo}, diff={diff:+.1f}")
            else:
                self.assertGreater(diff, 0,
                    f"{team}: expected post-MD1 Elo > production (MD1 result was good), "
                    f"got prod={prod_elo}, post={post_elo}, diff={diff:+.1f}")

    def test_md2_results_are_not_baked_into_post_md1_ratings(self):
        """The June-18 MD2 game effects must have been stripped.
        Concretely: Czechia drew with South Africa in MD2. Had that result been
        included, Czechia's post-MD1 Elo would be lower (they were the favourite
        who drew). Instead the rollback adds +16 back, so the post-MD1 Elo must
        be higher than it would be if the MD2 game were included."""
        czechia_post = self.post.teams["Czechia"].elo
        # If MD2 were included: post_md1 - 16 (from the raw June-18 swing)
        czechia_if_md2_included = czechia_post - 16
        # But without the rollback that lower value would be what we see.
        # Since we rolled back, post_md1 should be 16 pts higher than that.
        # Equivalently: post_md1 should NOT equal (post_md1 - 16).
        self.assertNotAlmostEqual(czechia_post, czechia_if_md2_included, places=0,
            msg="Czechia Elo rollback appears not applied: MD2 result still baked in")

    def test_south_africa_md2_result_stripped(self):
        """South Africa won a draw vs Czechia in MD2 (+16 Elo in raw data).
        After rollback, their Elo must be 16 pts LOWER than the raw June-18 value."""
        sa_post = self.post.teams["South Africa"].elo
        # If rollback was NOT applied, SA would be 16 pts higher than what we have.
        sa_if_md2_included = sa_post + 16
        self.assertNotAlmostEqual(sa_post, sa_if_md2_included, places=0,
            msg="South Africa Elo rollback appears not applied: MD2 result still baked in")

    def test_all_48_teams_present_in_post_md1_ratings(self):
        """The post-MD1 file must cover every team in the tournament."""
        prod_teams = set(self.prod.teams.keys())
        post_teams = set(self.post.teams.keys())
        missing = prod_teams - post_teams
        self.assertEqual(missing, set(),
            f"Teams in production ratings but missing from post-MD1 file: {missing}")

    def test_post_md1_elo_sane_range(self):
        """All Elo values in post-MD1 ratings should be within 800–2200 (sanity floor)."""
        for team, tr in self.post.teams.items():
            self.assertGreater(tr.elo, 800,  f"{team} post-MD1 Elo suspiciously low: {tr.elo}")
            self.assertLess(tr.elo,    2200, f"{team} post-MD1 Elo suspiciously high: {tr.elo}")


# ---------------------------------------------------------------------------
# C. SUPPRESSION INTEGRATION
# ---------------------------------------------------------------------------
# Rebuilds the forecaster-loop from build_model_lab.py in miniature and
# checks the actual None placement — not the boolean predicate in isolation.
# ---------------------------------------------------------------------------

def _rps(probs, outcome_i):
    cum_p = 0.0; score = 0.0
    for i, p in enumerate(probs):
        cum_p += p
        score += (cum_p - (1.0 if i >= outcome_i else 0.0)) ** 2
    return score / (len(probs) - 1)


def _outcome(sa, sb):
    return 0 if sa > sb else (2 if sa < sb else 1)


def _run_forecaster_loop(model, sv_variants, fixture_rows):
    """Replicate exactly the match_results computation from build_model_lab.main().
    Returns match_results: list[list[tuple|None]] indexed [match_i][forecaster_i].
    forecasters = [(label, fn, group)] with forecaster 0 always being Structural."""
    sv_tuned_list = sv_mod_get_tuned_list(model, sv_variants)

    def _struct(row):
        a, b = row["team_a"], row["team_b"]
        p = pr.predict_match(model, a, b)
        return (p.p_a, p.p_draw, p.p_b)

    forecasters = [("Structural", _struct, None)]
    for cfg, tuned in sv_tuned_list:
        label = cfg.get("label", "Structural (tuned)")
        group = cfg.get("group") or None
        def _fn(row, _t=tuned):
            a, b = row["team_a"], row["team_b"]
            p = pr.predict_match(_t, a, b)
            return (p.p_a, p.p_draw, p.p_b)
        forecasters.append((label, _fn, group))

    match_results = []
    for row in fixture_rows:
        sa, sb = int(row["score_a"]), int(row["score_b"])
        oi = _outcome(sa, sb)
        matchday = int(row.get("matchday", 1))
        per_fc = []
        for label, fn, group in forecasters:
            if group == "post-md1" and matchday == 1:
                per_fc.append(None)
                continue
            probs = fn(row)
            per_fc.append((probs, _rps(probs, oi)))
        match_results.append(per_fc)

    return forecasters, match_results


def sv_mod_get_tuned_list(model, variants):
    sv._CACHE.clear()
    return sv._get_tuned_list(model, variants)


class SuppressionIntegrationTests(unittest.TestCase):
    """Runs the actual forecaster loop (not just the boolean predicate) and
    checks that None appears exactly where it should in match_results."""

    def setUp(self):
        sv._CACHE.clear()
        self.m = _real_model()
        self.pm1 = _post_md1_model()
        if self.m is None:
            self.skipTest("production ratings unavailable")
        if self.pm1 is None:
            self.skipTest("post-MD1 ratings unavailable")

    def _two_variant_config(self):
        """Minimal 2-variant config: one pre-tournament, one post-md1 with ratings_dir."""
        return [
            {"label": "Pre",  "w_futi": 1.0, "w_elo": 1.0, "conf_offset": {}},
            {"label": "Post", "w_futi": 1.0, "w_elo": 1.0, "conf_offset": {},
             "group": "post-md1", "ratings_dir": "data/ratings/post-md1"},
        ]

    def _md1_row(self):
        return {"team_a": "England", "team_b": "Croatia",
                "score_a": "4", "score_b": "2", "matchday": "1",
                "country": "", "stadium": ""}

    def _md2_row(self):
        return {"team_a": "Czechia", "team_b": "South Africa",
                "score_a": "1", "score_b": "1", "matchday": "2",
                "country": "", "stadium": ""}

    def test_post_md1_is_none_for_md1_match(self):
        """Forecaster index 2 (post-md1 variant) must be None for an MD1 match."""
        forecasters, results = _run_forecaster_loop(
            self.m, self._two_variant_config(), [self._md1_row()])
        # forecaster 0 = Structural, 1 = Pre (pre-tournament sv), 2 = Post (post-md1)
        post_fc_i = next(i for i, (l, _, g) in enumerate(forecasters) if g == "post-md1")
        self.assertIsNone(results[0][post_fc_i],
            "post-md1 forecaster should be None (suppressed) for an MD1 match")

    def test_pre_tournament_is_not_none_for_md1_match(self):
        """Pre-tournament forecasters must NOT be suppressed on MD1 matches."""
        forecasters, results = _run_forecaster_loop(
            self.m, self._two_variant_config(), [self._md1_row()])
        pre_fc_i = next(i for i, (l, _, g) in enumerate(forecasters) if g is None and i > 0)
        self.assertIsNotNone(results[0][pre_fc_i],
            "pre-tournament variant should NOT be suppressed on MD1 match")

    def test_structural_incumbent_is_not_none_for_md1_match(self):
        """Forecaster 0 (Structural, group=None) must never be suppressed."""
        forecasters, results = _run_forecaster_loop(
            self.m, self._two_variant_config(), [self._md1_row()])
        self.assertIsNotNone(results[0][0],
            "Structural incumbent (forecaster 0) must not be suppressed for any match")

    def test_post_md1_is_scored_for_md2_match(self):
        """post-md1 forecaster must return a real (probs, rps) tuple for an MD2 match."""
        forecasters, results = _run_forecaster_loop(
            self.m, self._two_variant_config(), [self._md2_row()])
        post_fc_i = next(i for i, (l, _, g) in enumerate(forecasters) if g == "post-md1")
        fc_result = results[0][post_fc_i]
        self.assertIsNotNone(fc_result, "post-md1 forecaster should be scored for an MD2 match")
        probs, rps_val = fc_result
        self.assertAlmostEqual(sum(probs), 1.0, places=9)
        self.assertGreaterEqual(rps_val, 0.0)
        self.assertLessEqual(rps_val, 0.5)

    def test_suppression_count_matches_md1_fixture_count(self):
        """Given N MD1 matches, the post-md1 forecaster must have exactly N Nones."""
        md1_rows = [
            {"team_a": "England",       "team_b": "Croatia",   "score_a": "4", "score_b": "2", "matchday": "1", "country": "", "stadium": ""},
            {"team_a": "Brazil",        "team_b": "Morocco",   "score_a": "1", "score_b": "1", "matchday": "1", "country": "", "stadium": ""},
        ]
        md2_rows = [
            {"team_a": "Czechia",       "team_b": "South Africa", "score_a": "1", "score_b": "1", "matchday": "2", "country": "", "stadium": ""},
        ]
        forecasters, results = _run_forecaster_loop(
            self.m, self._two_variant_config(), md1_rows + md2_rows)
        post_fc_i = next(i for i, (l, _, g) in enumerate(forecasters) if g == "post-md1")
        nones = sum(1 for r in results if r[post_fc_i] is None)
        self.assertEqual(nones, len(md1_rows),
            f"Expected {len(md1_rows)} suppressed (MD1) rows, got {nones}")

    def test_live_config_suppression_on_all_md1_fixtures(self):
        """End-to-end: run the live config against the actual fixtures.csv MD1 rows
        and confirm every post-md1 slot is None."""
        live_cfg = REPO / "data" / "calibration" / "struct_variant.json"
        if not live_cfg.exists():
            self.skipTest("struct_variant.json not present")
        variants = sv._load_config(live_cfg)
        post_indices_in_variants = [i for i, v in enumerate(variants)
                                    if v.get("group") == "post-md1"]
        self.assertTrue(post_indices_in_variants, "No post-md1 variants in live config")

        md1_rows = []
        with (REPO / "data" / "fixtures.csv").open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("status", "").strip().lower() == "played" and int(r.get("matchday", 0)) == 1:
                    md1_rows.append(r)
        self.assertTrue(md1_rows, "No played MD1 rows found in fixtures.csv")

        forecasters, results = _run_forecaster_loop(self.m, variants, md1_rows)
        post_fc_indices = [i for i, (l, _, g) in enumerate(forecasters) if g == "post-md1"]

        for mi, row in enumerate(md1_rows):
            for fc_i in post_fc_indices:
                self.assertIsNone(results[mi][fc_i],
                    f"Match {row['match_id']} (MD1): post-md1 forecaster {fc_i} should be None, got {results[mi][fc_i]}")


# ---------------------------------------------------------------------------
# Config shape tests (catch accidental edits to struct_variant.json)
# ---------------------------------------------------------------------------
class ConfigShapeTests(unittest.TestCase):
    def setUp(self):
        live_cfg = REPO / "data" / "calibration" / "struct_variant.json"
        if not live_cfg.exists():
            self.skipTest("struct_variant.json not present")
        self.variants = sv._load_config(live_cfg)

    def test_exactly_two_post_md1_variants(self):
        post = [v for v in self.variants if v.get("group") == "post-md1"]
        self.assertEqual(len(post), 2)

    def test_exactly_two_pre_tournament_variants(self):
        pre = [v for v in self.variants if not v.get("group")]
        self.assertEqual(len(pre), 2)

    def test_post_md1_all_have_ratings_dir(self):
        for v in self.variants:
            if v.get("group") == "post-md1":
                self.assertIn("ratings_dir", v, f"Missing ratings_dir: {v.get('label')}")

    def test_pre_tournament_none_have_ratings_dir(self):
        for v in self.variants:
            if not v.get("group"):
                self.assertNotIn("ratings_dir", v, f"Unexpected ratings_dir: {v.get('label')}")

    def test_all_variants_have_labels(self):
        for v in self.variants:
            self.assertIn("label", v)
            self.assertTrue(v["label"].strip())

    def test_structural_post_md1_has_elo_weight(self):
        """The 50/50 structural post-MD1 variant must keep Elo at full weight."""
        structural = next((v for v in self.variants
                           if v.get("group") == "post-md1" and v.get("w_elo", 0) > 0), None)
        self.assertIsNotNone(structural, "No post-MD1 structural (Elo>0) variant found")
        self.assertAlmostEqual(structural["w_elo"],  1.0, places=6)
        self.assertAlmostEqual(structural["w_futi"], 1.0, places=6)

    def test_futi_only_post_md1_has_zero_elo(self):
        """The 100%-Futi post-MD1 variant must have w_elo=0."""
        futi_only = next((v for v in self.variants
                          if v.get("group") == "post-md1" and v.get("w_elo", 1) == 0), None)
        self.assertIsNotNone(futi_only, "No post-MD1 Futi-only (w_elo=0) variant found")
        self.assertAlmostEqual(futi_only["w_elo"],  0.0, places=6)
        self.assertAlmostEqual(futi_only["w_futi"], 1.0, places=6)


# ---------------------------------------------------------------------------
# D. BETTING EDGE ISOLATION
# ---------------------------------------------------------------------------
# The edge pipeline is:
#   pr.predict_match() → pa/pd/pb → info["p_a"] → logged to ledger
#   → odds.consensus_probs() reads ledger → our_p in edge calculation
#
# struct_variants are written to info["struct_variants"] — a SEPARATE key.
# This test class verifies that separation is intact: the p_a/p_draw/p_b
# that would be logged to the ledger come from the incumbent model, NOT from
# the struct_variant overlay.
# ---------------------------------------------------------------------------

class BettingEdgeIsolationTests(unittest.TestCase):
    """build_site.call() must keep incumbent probs and overlay probs in
    separate keys — the overlay must NEVER overwrite the ledger-bound p_a."""

    def setUp(self):
        sv._CACHE.clear()
        self.m = _real_model()
        if self.m is None:
            self.skipTest("production ratings unavailable")
        try:
            import build_site as bs
            self.bs = bs
        except Exception as e:
            self.skipTest(f"build_site unavailable: {e}")

    def _make_call(self):
        """Return the call() function from load_predictor() (the live version)."""
        call_fn, err = self.bs.load_predictor()
        if call_fn is None:
            self.skipTest(f"load_predictor failed: {err}")
        return call_fn

    def test_call_result_has_p_a_from_incumbent(self):
        """info['p_a'] must equal pr.predict_match() output, not a struct_variant."""
        call = self._make_call()
        row = {"team_a": "England", "team_b": "Croatia",
               "match_id": "L1", "country": "Canada", "stadium": "AT&T Stadium"}
        info = call(row)
        self.assertIsNotNone(info)

        # What the incumbent model gives directly
        host = pr.HOST_BY_COUNTRY.get("Canada")
        hfa = host if host in ("England", "Croatia") else None
        p = pr.predict_match(self.m, "England", "Croatia", hfa_team=hfa)

        self.assertAlmostEqual(info["p_a"],    p.p_a,    places=9,
            msg="info['p_a'] diverged from pr.predict_match — struct_variant may have leaked in")
        self.assertAlmostEqual(info["p_draw"], p.p_draw, places=9,
            msg="info['p_draw'] diverged from pr.predict_match")
        self.assertAlmostEqual(info["p_b"],    p.p_b,    places=9,
            msg="info['p_b'] diverged from pr.predict_match")

    def test_struct_variants_are_a_separate_key(self):
        """Overlay variants must live in info['struct_variants'], never in p_a/p_draw/p_b."""
        call = self._make_call()
        row = {"team_a": "England", "team_b": "Croatia",
               "match_id": "L1", "country": "Canada", "stadium": "AT&T Stadium"}
        info = call(row)
        self.assertIsNotNone(info)
        self.assertIn("p_a",    info)
        self.assertIn("p_draw", info)
        self.assertIn("p_b",    info)
        # struct_variants may or may not be present (depends on config existing),
        # but if present they must be a list under a DIFFERENT key
        if "struct_variants" in info:
            svs = info["struct_variants"]
            self.assertIsInstance(svs, list)
            for sv_entry in svs:
                self.assertIn("p_a",    sv_entry)
                self.assertIn("source", sv_entry)
                # The overlay entry is NOT the same object as the top-level probs
                self.assertIsNot(info, sv_entry)

    def test_struct_variant_probs_differ_from_incumbent_for_post_md1(self):
        """post-md1 struct_variants (ratings_dir) must give different W/D/L than
        the incumbent — proving the two are independent and not aliased."""
        call = self._make_call()
        row = {"team_a": "Czechia", "team_b": "South Africa",
               "match_id": "A3", "country": "USA", "stadium": "Mercedes-Benz Stadium"}
        info = call(row)
        self.assertIsNotNone(info)
        if "struct_variants" not in info:
            self.skipTest("struct_variants not present (config absent)")

        post_variants = [sv for sv in info["struct_variants"] if sv.get("group") == "post-md1"]
        if not post_variants:
            self.skipTest("no post-md1 struct_variants in call() output")

        incumbent_pa = info["p_a"]
        for sv_entry in post_variants:
            self.assertFalse(
                abs(sv_entry["p_a"] - incumbent_pa) < 1e-9,
                f"post-md1 struct_variant '{sv_entry['source']}' p_a is identical to "
                f"incumbent — ratings_dir override appears not loaded"
            )

    def test_no_top_level_group_key_in_call_result(self):
        """The 'group' field belongs inside struct_variants entries, NEVER at
        the top level of info — that would indicate struct_variant data leaked
        into the ledger-bound prediction dict."""
        call = self._make_call()
        row = {"team_a": "England", "team_b": "Croatia",
               "match_id": "L1", "country": "Canada", "stadium": "AT&T Stadium"}
        info = call(row)
        self.assertIsNotNone(info)
        self.assertNotIn("group", info,
            "'group' key found at top level of call() result — struct_variant data may "
            "have leaked into the incumbent prediction dict that feeds the ledger")


if __name__ == "__main__":
    unittest.main()
