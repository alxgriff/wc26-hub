#!/usr/bin/env python3
"""Reference-model overlay — separate W/D/L triple from the external
world_cup_predictions repo (Brian's "first real ML project" XGBoost model
with rolling Elo + form lags + rest + H2H). Lives ALONGSIDE structural and
our own hybrid; never replaces either.

The three-way comparison in experiments/THREE_WAY.md showed the reference
model beats both structural and our hybrid on the first 16 played wc26
matches by an aggregated RPS margin. The instinct that motivated the
hybrid (an ML overlay can help) was right; our v1 just had too thin a
feature set. The reference IS our v2 — same author, richer features,
already trained — so we wire it in via sys.path injection rather than
vendoring its source.

Inertness contract (same shape as hybrid.py):
  * No data/calibration/reference.ubj → reference_predict() returns None.
  * Reference repo not at REFERENCE_REPO env-or-default path → returns None.
  * xgboost / pandas / sklearn not importable → returns None.
  * --reference CLI flag is opt-in; absent ⇒ baseline output unchanged.

API:
    pred = reference_predict(model, team_a, team_b, hfa_team=..., match_date="2026-06-18")

The structural pipeline (predict_match, score matrix, totals, BTTS, etc.) is
not touched. Reference gets ONE row alongside the existing Model bullet in
render_prediction.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "data" / "calibration" / "reference.ubj"
META = REPO_ROOT / "data" / "calibration" / "reference.meta.json"

# Where the reference repo lives. Override with the WC26_REFERENCE_REPO env var
# if you move it. Default matches Brian's dev tree on this machine.
REFERENCE_REPO = Path(os.environ.get(
    "WC26_REFERENCE_REPO", "/Users/bnowak/dev/world_cup_predictions"))

# CLAUDE.md canon → reference repo's internal canonical names. Anything not in
# the map passes through unchanged. The mapping is intentionally MINIMAL — it
# encodes only the genuine spelling differences between the two projects.
CANON_TO_REF = {
    "Türkiye": "Turkey",
    "Côte d'Ivoire": "Ivory Coast",
    "Czechia": "Czech Republic",
    "Curaçao": "Curacao",
    "Cape Verde": "Cabo Verde",
}


def to_ref_name(canon: str) -> str:
    """Translate one wc26-hub canon name to the reference repo's internal canon."""
    return CANON_TO_REF.get(canon.strip(), canon.strip())


@dataclass
class ReferencePrediction:
    team_a: str
    team_b: str
    p_a: float
    p_draw: float
    p_b: float
    source: str       # e.g. "ref-xgb fit 2026-06-11 (Elo+form+H2H)"
    asof: str         # what the booster was trained through


# ---------------------------------------------------------------- loader

# Process-local cache of the rebuilt reference state. The reference's
# predict_symmetric needs `long` (per-team match history) and `final_elo` at
# call time; rebuilding both from results.csv costs ~100ms — cheap once, but
# cumulative across a 72-fixture build_edition run. Cache it.
_REF_CACHE = None


def _import_reference_module():
    """Inject the reference repo onto sys.path and import predict_today.
    Returns the module, or None if anything's missing. Quiet on failure —
    every callsite treats None as "overlay unavailable, stay structural-only"."""
    if not REFERENCE_REPO.exists():
        return None
    sys.path.insert(0, str(REFERENCE_REPO))
    try:
        import predict_today  # noqa: F401
        return predict_today
    except ImportError:
        return None
    finally:
        # Don't pollute sys.path beyond the import — once predict_today is
        # in sys.modules, subsequent accesses hit the cache.
        if str(REFERENCE_REPO) in sys.path:
            sys.path.remove(str(REFERENCE_REPO))


@dataclass
class _ReferenceState:
    """Everything the reference's predict_symmetric needs at call time, plus the
    meta. Held by _REF_CACHE; rebuilt at most once per process."""
    booster: object       # xgb.XGBClassifier (loaded via .load_model)
    long: object          # pandas.DataFrame: per-team match-by-match history
    final_elo: dict       # team -> Elo as of latest training match
    meta: dict            # source / asof / trained_through / ref_repo_path


def _load_reference():
    """Build (or return cached) _ReferenceState, or None when any gate fails.
    The cache is invalidated when ARTIFACT changes mtime — that way refitting
    in a long-lived process picks up the new model on next call."""
    global _REF_CACHE
    if not ARTIFACT.exists() or not META.exists():
        return None
    # Cache invalidation: re-derive when the artifact has been re-fit.
    art_mtime = ARTIFACT.stat().st_mtime
    if _REF_CACHE is not None and _REF_CACHE.meta.get("_mtime") == art_mtime:
        return _REF_CACHE
    try:
        import xgboost as xgb
        import pandas  # noqa: F401
        import sklearn  # noqa: F401  — reference repo imports it at module top
    except ImportError:
        return None
    ref = _import_reference_module()
    if ref is None:
        return None
    try:
        meta = json.loads(META.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    booster = xgb.XGBClassifier()
    booster.load_model(str(ARTIFACT))
    # Rebuild as-of state. Reference's own functions, no duplication.
    results = ref.load_results()
    _, final_elo = ref.build_dataset(results)
    long = ref.per_team_long(results)
    meta["_mtime"] = art_mtime
    _REF_CACHE = _ReferenceState(booster=booster, long=long,
                                 final_elo=final_elo, meta=meta)
    return _REF_CACHE


def reference_predict(model, team_a: str, team_b: str, *,
                      hfa_team=None, match_date: str | None = None,
                      neutral: bool | None = None,
                      tournament_weight: int = 4):
    """Reference-model overlay W/D/L for one match, or None when inert.

    Args:
        model: the structural RatingModel — accepted for API symmetry with
            hybrid_predict (not actually used; the reference has its own Elo).
        team_a, team_b: canon names per CLAUDE.md.
        hfa_team: which side has home advantage (or None). The reference repo's
            convention is MATCH_NEUTRAL=True for wc26 — they strip HFA in their
            published validation. We default to that to keep the comparison
            apples-to-apples; pass ``neutral=False`` to override.
        match_date: kickoff date as "YYYY-MM-DD". Drives the as-of features
            (form/rest/H2H/elo). When None, uses the booster's trained_through
            date — i.e., the freshest data the model was trained on.
        tournament_weight: passed straight through. Default 4 = FIFA World Cup,
            matching the reference repo's own convention.

    Returns:
        ReferencePrediction or None (inert).
    """
    state = _load_reference()
    if state is None:
        return None
    # Default to the reference repo's published convention so our overlay numbers
    # match `python predict_today.py "Team A" "Team B"` byte-for-byte.
    if neutral is None:
        neutral = True
    asof = match_date or state.meta.get("trained_through") or "2026-06-11"
    ref = _import_reference_module()
    if ref is None:
        return None
    ra, rb = to_ref_name(team_a), to_ref_name(team_b)
    p_a, p_draw, p_b = ref.predict_symmetric(
        state.booster, state.long, state.final_elo,
        ra, rb, asof, neutral, tournament_weight)
    return ReferencePrediction(
        team_a=team_a, team_b=team_b,
        p_a=float(p_a), p_draw=float(p_draw), p_b=float(p_b),
        source=str(state.meta.get("source", "ref-xgb")),
        asof=str(state.meta.get("asof", "")))
