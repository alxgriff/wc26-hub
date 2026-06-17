#!/usr/bin/env python3
"""ML overlay (Groll-style hybrid) — separate W/D/L triple, shown side-by-side.

This module is a PURE ADD-ON. The structural pipeline (predict.predict_match,
score matrix, totals/BTTS/DNB, resolve_knockout) is not touched. The booster
reads structural outputs as features and emits its own W/D/L triple — display
only, never fed back into the structural model.

Inert by design — any of these states ⇒ hybrid_predict() returns None ⇒
predict.render_prediction shows exactly today's output:
  * No data/calibration/hybrid.ubj
  * No sibling hybrid.meta.json
  * xgboost (or numpy) not importable
The CLI flag is opt-in (`predict.py ... --hybrid`); without it nothing changes.

Importable API:
    feats   = extract_features_live(model, struct, a, b, hfa_team=...)
    booster = _load_booster()                # None if absent or deps missing
    hybrid  = hybrid_predict(model, a, b, hfa_team=...)   # HybridPrediction | None
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as pr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "data" / "calibration" / "hybrid.ubj"
META = REPO_ROOT / "data" / "calibration" / "hybrid.meta.json"

# Feature-name catalog. A trained artifact's meta.json names exactly which of
# these were used and in what order — that names list is the contract, not any
# specific tuple here. Adding a new computable feature means extending
# _ALL_FEATURE_NAMES and the _features helper; an artifact fit before the
# extension still loads (its meta names a subset).
FEATURES_FULL = (
    # structural outputs (also synthesized at train-time from rolling Elo)
    "p_a", "p_draw", "p_b",
    "lam_a", "lam_b", "total",
    "sup", "texture",
    # raw ratings (rolling Elo historically; live Elo at predict time)
    "elo_a", "elo_b", "elo_gap",
    # context
    "tournament_weight", "is_neutral", "home_advantage_side",
)

# v2 "lean" feature set — strips the structural triple/lambdas/total/texture,
# leaving only raw inputs + context. Forces the booster to learn from raw
# rather than digested signals, addressing the v1 over-redundancy hypothesis.
FEATURES_LEAN = (
    "sup", "elo_a", "elo_b", "elo_gap",
    "tournament_weight", "is_neutral", "home_advantage_side",
)

# v3 "minimal" feature set — only the supremacy term + context. No raw Elo
# values, no Elo gap (sup already encodes it scaled by theta). Tests whether
# the booster benefits from absolute Elo levels at all, or whether sup alone
# suffices.
FEATURES_MINIMAL = (
    "sup", "tournament_weight", "is_neutral", "home_advantage_side",
)

FEATURES = FEATURES_FULL   # default for callers that don't specify
_ALL_FEATURE_NAMES = frozenset(FEATURES_FULL)   # everything _features knows how to emit

# Tournament weight as a FEATURE only (the booster decides what to do with it).
# fit_hybrid.py uses the same table — keep in one place.
TOURNAMENT_WEIGHTS = {
    "FIFA World Cup": 1.0,
    "FIFA World Cup qualification": 0.7,
    "UEFA Euro": 1.0,
    "UEFA Euro qualification": 0.7,
    "Copa América": 1.0,
    "Copa America": 1.0,
    "Africa Cup of Nations": 0.9,
    "AFC Asian Cup": 0.9,
    "CONCACAF Gold Cup": 0.8,
    "UEFA Nations League": 0.7,
    "CONCACAF Nations League": 0.5,
}
DEFAULT_TOURNAMENT_WEIGHT = 0.5  # any other competitive match; friendlies are excluded upstream

# Deployment-slice: major tournaments only. Used by fit_hybrid for the verdict.
MAJOR_TOURNAMENTS = frozenset([
    "FIFA World Cup", "UEFA Euro", "Copa América", "Copa America",
])


@dataclass
class HybridPrediction:
    team_a: str
    team_b: str
    p_a: float
    p_draw: float
    p_b: float
    source: str   # e.g. "xgb-v1 fit 2026-06-15"
    asof: str


# ---------------------------------------------------------------- calibration

def apply_isotonic(curve: list, x: float) -> float:
    """Piecewise-linear interpolation along a sorted, non-decreasing curve of
    (x_breakpoint, y_calibrated) pairs. Clamps at the edges — extrapolation past
    the training range would be guess-work, not calibration. Matches sklearn's
    IsotonicRegression(out_of_bounds='clip') behaviour."""
    if not curve:
        return x
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    lo, hi = 0, len(curve) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if curve[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    x0, y0 = curve[lo]
    x1, y1 = curve[hi]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def apply_platt(params: tuple, x: float) -> float:
    """Two-parameter sigmoid: σ(A·x + B). A>0 ⇒ monotone-increasing in x."""
    A, B = params
    z = A * x + B
    if z > 500:           # overflow guard for tail tails; σ saturates to 1/0 well before
        return 1.0
    if z < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


@dataclass
class _Calibrator:
    """Per-class one-vs-rest calibrator. ``method`` is 'isotonic' or 'platt';
    ``per_class`` is a 3-element list (one entry per W/D/L class) of the
    calibrator's parameters in that method's serialization. At predict time:
    apply per-class transform to the booster's raw probabilities, then renormalise
    so the triple sums to 1. The renormalisation is what makes one-vs-rest
    + per-class calibration coherent in a multi-class setting."""
    method: str
    per_class: list

    def apply(self, p_a: float, p_draw: float, p_b: float) -> tuple:
        if self.method == "isotonic":
            ca = apply_isotonic(self.per_class[0], p_a)
            cd = apply_isotonic(self.per_class[1], p_draw)
            cb = apply_isotonic(self.per_class[2], p_b)
        elif self.method == "platt":
            ca = apply_platt(tuple(self.per_class[0]), p_a)
            cd = apply_platt(tuple(self.per_class[1]), p_draw)
            cb = apply_platt(tuple(self.per_class[2]), p_b)
        else:
            return p_a, p_draw, p_b
        # Guard against numerical edge cases where all three round to zero
        # (shouldn't happen in practice — isotonic clamps to training range and
        # Platt has overflow guards — but a renormalise-by-zero would be silent miscalibration).
        z = ca + cd + cb
        if z <= 0:
            return p_a, p_draw, p_b
        return ca / z, cd / z, cb / z


@dataclass
class _Booster:
    """Thin wrapper over the xgboost Booster + the loaded meta. The only place
    that knows the prediction-time tensor shape; tests stub this directly.

    If ``calibrator`` is non-None, the booster's raw probabilities are routed
    through it before being returned. That preserves the W/D/L ordering (each
    calibrator is monotone-increasing) but rescales the magnitudes — exactly
    what an over-confident tree booster needs."""
    booster: object
    meta: dict
    calibrator: object = None       # _Calibrator or None — None ⇒ raw booster output

    def predict_proba(self, feats: list) -> tuple:
        import numpy as np
        import xgboost as xgb
        names = list(self.meta["feature_names"])
        assert len(feats) == len(names), (
            f"booster expected {len(names)} features, got {len(feats)}")
        dm = xgb.DMatrix(np.asarray([feats], dtype=float), feature_names=names)
        p = self.booster.predict(dm)[0]
        p_a, p_draw, p_b = float(p[0]), float(p[1]), float(p[2])
        if self.calibrator is not None:
            return self.calibrator.apply(p_a, p_draw, p_b)
        return p_a, p_draw, p_b


def tournament_weight_for(tournament: str) -> float:
    return TOURNAMENT_WEIGHTS.get((tournament or "").strip(), DEFAULT_TOURNAMENT_WEIGHT)


def _home_advantage_side(team_a: str, hfa_team) -> int:
    """+1 if team_a is the host, -1 if team_b is, 0 otherwise (neutral). Symmetric
    so a swap of team_a/team_b at the booster sees the negated context."""
    if hfa_team is None:
        return 0
    return +1 if hfa_team == team_a else -1


def _features(*, p_a: float, p_draw: float, p_b: float,
              lam_a: float, lam_b: float, total: float,
              sup: float, texture: float,
              elo_a: float, elo_b: float,
              tournament_weight: float,
              is_neutral: int, home_advantage_side: int,
              feature_names=FEATURES) -> list:
    """Return values in ``feature_names`` order. Pure function — both train and
    predict paths route through this so the column order has ONE definition (the
    artifact's meta.json `feature_names`)."""
    vals = {
        "p_a": p_a, "p_draw": p_draw, "p_b": p_b,
        "lam_a": lam_a, "lam_b": lam_b, "total": total,
        "sup": sup, "texture": texture,
        "elo_a": elo_a, "elo_b": elo_b, "elo_gap": elo_a - elo_b,
        "tournament_weight": float(tournament_weight),
        "is_neutral": int(is_neutral),
        "home_advantage_side": int(home_advantage_side),
    }
    unknown = [f for f in feature_names if f not in vals]
    if unknown:
        raise ValueError(f"unknown feature name(s) in feature_names: {unknown}")
    return [float(vals[f]) for f in feature_names]


def extract_features_live(model: pr.RatingModel, struct: pr.Prediction,
                          team_a: str, team_b: str, *,
                          hfa_team=None,
                          tournament_weight: float = 1.0,
                          feature_names=FEATURES) -> list:
    """Predict-time feature extraction. ``sup`` and ``texture`` are recovered
    from the Prediction analytically so the formulas live in ONE place
    (predict.predict_match):

        lam_a = total * σ(sup) and lam_b = total * (1-σ(sup))  ⇒  sup = ln(lam_a/lam_b)
        total = mu0 · exp(α · texture)                           ⇒  texture = ln(total/mu0) / α

    Default ``tournament_weight=1.0`` because every wc26 group/knockout game is
    "FIFA World Cup". For non-WC contexts (e.g. backtesting) pass it explicitly.
    """
    cfg = model.config
    a, b = model.teams[team_a], model.teams[team_b]
    sup = math.log(struct.lambda_a / struct.lambda_b)
    texture = math.log(struct.total / cfg.mu0) / cfg.alpha
    return _features(
        p_a=struct.p_a, p_draw=struct.p_draw, p_b=struct.p_b,
        lam_a=struct.lambda_a, lam_b=struct.lambda_b, total=struct.total,
        sup=sup, texture=texture,
        elo_a=a.elo, elo_b=b.elo,
        tournament_weight=tournament_weight,
        is_neutral=(1 if hfa_team is None else 0),
        home_advantage_side=_home_advantage_side(team_a, hfa_team),
        feature_names=feature_names,
    )


def _load_booster(artifact_path=None, meta_path=None):
    """Load the booster + meta, or return None if either is missing or xgboost
    is not installed. Single inertness gate — every behaviour change routes
    through here. Catches the feature_names drift case explicitly: if the meta's
    feature_names don't match this module's FEATURES tuple, treat as missing
    (loud stderr warning) — the artifact was fit against a different schema."""
    artifact_path = Path(artifact_path) if artifact_path else ARTIFACT
    meta_path = Path(meta_path) if meta_path else META
    if not artifact_path.exists() or not meta_path.exists():
        return None
    try:
        import numpy  # noqa: F401  (transitive dep of xgboost; surface failure here)
        import xgboost as xgb
    except ImportError:
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    names = tuple(meta.get("feature_names", ()))
    if not names:
        print(f"warning: {meta_path.name} has no feature_names — treating overlay as inert.",
              file=sys.stderr)
        return None
    unknown = [n for n in names if n not in _ALL_FEATURE_NAMES]
    if unknown:
        print(f"warning: {meta_path.name} names features this hybrid.py doesn't know how to "
              f"compute ({unknown}) — refit or upgrade hybrid.py; treating overlay as inert.",
              file=sys.stderr)
        return None
    booster = xgb.Booster()
    booster.load_model(str(artifact_path))
    cal = None
    cal_blob = meta.get("calibration")
    if cal_blob:
        method = cal_blob.get("method")
        per_class = cal_blob.get("per_class")
        if method in ("isotonic", "platt") and per_class and len(per_class) == 3:
            cal = _Calibrator(method=method, per_class=per_class)
        else:
            print(f"warning: {meta_path.name} has malformed 'calibration' block — "
                  "loading uncalibrated booster.", file=sys.stderr)
    return _Booster(booster=booster, meta=meta, calibrator=cal)


def hybrid_predict(model: pr.RatingModel, team_a: str, team_b: str, *,
                   hfa_team=None, stage: str = "group"):
    """ML overlay — separate W/D/L triple. Returns None when the layer is inert
    (artifact absent, deps missing, or feature schema mismatch). The structural
    pipeline is unaffected either way.

    ``stage`` is accepted (per the plan doc signature) but unused in v1; no
    stage feature is fittable from the corpus today (results.csv has no round/stage
    column). Kept in the signature so callers don't break when v2 adds it."""
    booster = _load_booster()
    if booster is None:
        return None
    feature_names = tuple(booster.meta["feature_names"])    # the artifact's own contract
    struct = pr.predict_match(model, team_a, team_b, hfa_team=hfa_team)
    feats = extract_features_live(model, struct, team_a, team_b, hfa_team=hfa_team,
                                  feature_names=feature_names)
    assert len(feats) == len(feature_names), (
        f"feature vector length {len(feats)} != feature_names len {len(feature_names)} "
        "— train/predict drift bug")
    p_a, p_draw, p_b = booster.predict_proba(feats)
    z = p_a + p_draw + p_b
    return HybridPrediction(team_a=team_a, team_b=team_b,
                            p_a=p_a / z, p_draw=p_draw / z, p_b=p_b / z,
                            source=str(booster.meta.get("source", "xgb-v?")),
                            asof=str(booster.meta.get("asof", "")))
