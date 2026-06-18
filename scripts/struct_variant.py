#!/usr/bin/env python3
"""Structural-variant overlay — a confederation-tuned (and/or Futi-tilted) copy of
the structural model, shown side-by-side. Display-only, no betting calcs.

The PRODUCTION structural model (predict.predict_match) is never touched. This
recomputes a *tuned* team strength at runtime from the same frozen inputs the
production model already carries (each TeamRating holds .elo / .futi /
.market_implied), then returns a separate W/D/L triple. So there is no new data
source and no leakage — only a reweight + per-confederation offset of the
existing June-11 ratings.

Inert unless data/calibration/struct_variant.json exists (activation + tunable
params, same discipline as the rho/reference artifacts). Absent / malformed /
deps-free ⇒ tuned_predict() returns None and nothing renders. The config is the
one place to retune after more matches land — no code change needed.

Config schema (all keys optional except they should sum sensibly):
    {
      "label": "Structural (conf-tuned)",
      "w_futi": 1.0, "w_elo": 1.0, "w_market": 0.0,
      "conf_offset": {"CAF": 40, "CONMEBOL": -40, "AFC": 20}
    }
"""
from __future__ import annotations

import dataclasses
import json
import math
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as pr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "data" / "calibration" / "struct_variant.json"

# 48-team WC26 confederation map (kept local; the offset keys reference these).
CAF = {"Morocco", "Senegal", "Côte d'Ivoire", "Ghana", "Egypt", "Tunisia",
       "Algeria", "Cape Verde", "South Africa", "DR Congo"}
CONMEBOL = {"Brazil", "Argentina", "Uruguay", "Colombia", "Ecuador", "Paraguay"}
UEFA = {"Spain", "France", "England", "Germany", "Portugal", "Netherlands",
        "Belgium", "Croatia", "Switzerland", "Austria", "Norway", "Sweden",
        "Scotland", "Czechia", "Türkiye", "Bosnia and Herzegovina"}
AFC = {"Japan", "South Korea", "Iran", "Saudi Arabia", "Australia", "Qatar",
       "Uzbekistan", "Jordan", "Iraq"}
CONCACAF = {"Mexico", "United States", "Canada", "Panama", "Haiti", "Curaçao"}
OFC = {"New Zealand"}


def _conf(team: str) -> str:
    for name, s in (("CAF", CAF), ("CONMEBOL", CONMEBOL), ("UEFA", UEFA),
                    ("AFC", AFC), ("CONCACAF", CONCACAF), ("OFC", OFC)):
        if team in s:
            return name
    return "?"


@dataclasses.dataclass
class TunedPrediction:
    team_a: str
    team_b: str
    p_a: float
    p_draw: float
    p_b: float
    source: str


def _load_config(path: "Path | None" = None) -> "list[dict] | None":
    """Returns list of variant dicts, or None when inert (file absent/malformed).
    Accepts both the old single-dict format (wrapped to a list) and a JSON array."""
    path = Path(path) if path else CONFIG
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if isinstance(raw, dict):
        return [raw]   # backwards-compat: single-variant file
    if isinstance(raw, list) and raw and all(isinstance(x, dict) for x in raw):
        return raw
    return None


# Process-local cache: building the tuned model touches every team, so do it
# once per (production-model identity, config mtime) and reuse across matches.
# Maps (model_id, mtime) -> list of (cfg_dict, tuned_RatingModel).
_CACHE: dict = {}


def _build_tuned(model: pr.RatingModel, cfg: dict) -> pr.RatingModel:
    """Derive a tuned RatingModel from the production one — reweight Elo/Futi
    (optionally market) in z-space on the Elo scale, then apply per-confederation
    strength offsets. Mirrors load_ratings' own consensus math so w_futi=w_elo,
    no offset reproduces the production strengths exactly."""
    teams = list(model.teams)
    E = {t: model.teams[t].elo for t in teams}
    F = {t: model.teams[t].futi for t in teams}
    e_mean, e_sd = st.mean(E.values()), st.pstdev(E.values()) or 1.0
    f_mean, f_sd = st.mean(F.values()), st.pstdev(F.values()) or 1.0

    w_futi = float(cfg.get("w_futi", 1.0))
    w_elo = float(cfg.get("w_elo", 1.0))
    w_mkt = float(cfg.get("w_market", 0.0))
    offsets = cfg.get("conf_offset", {}) or {}

    M = {t: model.teams[t].market_implied for t in teams}
    have = {t: math.log(M[t]) for t in teams if M[t]}
    use_mkt = bool(w_mkt) and bool(have)
    if use_mkt:
        m_mean, m_sd = st.mean(have.values()), st.pstdev(have.values()) or 1.0

    tuned = {}
    for t in teams:
        zF = (F[t] - f_mean) / f_sd
        futi_as_elo = e_mean + zF * e_sd
        num = w_elo * E[t] + w_futi * futi_as_elo
        den = w_elo + w_futi
        if use_mkt:
            zM = ((math.log(M[t]) - m_mean) / m_sd) if M[t] else zF
            num += w_mkt * (e_mean + zM * e_sd)
            den += w_mkt
        strength = num / den + float(offsets.get(_conf(t), 0))
        tuned[t] = dataclasses.replace(model.teams[t], strength=strength)
    return dataclasses.replace(model, teams=tuned)


def _get_tuned_list(model: pr.RatingModel, variants: "list[dict]",
                    cfg_path: "Path | None" = None) -> "list[tuple[dict, pr.RatingModel]]":
    """Returns list of (cfg, tuned_model) for all variants; cached per mtime."""
    path = Path(cfg_path) if cfg_path else CONFIG
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (id(model), mtime)
    cached = _CACHE.get(key)
    if cached is None:
        cached = [(cfg, _build_tuned(model, cfg)) for cfg in variants]
        _CACHE.clear()              # only ever need the current build's tuned copies
        _CACHE[key] = cached
    return cached


def tuned_predict(model: pr.RatingModel, team_a: str, team_b: str, *,
                  hfa_team=None, config_path: "Path | None" = None
                  ) -> "list[TunedPrediction] | None":
    """Returns list[TunedPrediction] (one per configured variant), or None when
    inert (config absent/malformed). Each entry is an independent W/D/L read from
    a separately-built model — only the input strengths differ from production."""
    variants = _load_config(config_path)
    if variants is None:
        return None
    results = []
    for cfg, tuned in _get_tuned_list(model, variants, config_path):
        p = pr.predict_match(tuned, team_a, team_b, hfa_team=hfa_team)
        results.append(TunedPrediction(team_a=team_a, team_b=team_b,
                                       p_a=p.p_a, p_draw=p.p_draw, p_b=p.p_b,
                                       source=str(cfg.get("label", "Structural (tuned)"))))
    return results
