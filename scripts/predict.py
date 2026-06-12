#!/usr/bin/env python3
"""WC26 match predictor — aggregates team ratings into W/D/L, scoreline, totals.

Backbone (decided with the user, June 12; see data/Ratings/DATA_QUALITY.md):
  * Match strength = consensus of **verified Elo + Futi**, blended in z-space and
    anchored to the Elo scale. Sets favouritism (who wins, by how much).
  * Goals = a Poisson model whose total is matchup-specific, driven by **Futi
    Attack/Defense** (so Over/Under and BTTS are meaningful, not constant).
  * Opta tournament numbers and the **real outright market**
    (Market_Outrights_VERIFIED.csv, de-vigged) are **context overlays only**
    (advance %, ranks, disagreement flags) — tournament probabilities are
    bracket-confounded, not match strength. The Zeileis file is not used at all:
    its squad values are corrupted AND its "bookmaker consensus" column proved
    to be algebraically derived from its own model (uniform 1.30 multiplier),
    not market data.

Calibration notes (June 12 audit):
  * θ = 190 is FIT, not hand-picked: it minimises squared error between the
    model's win-plus-half-draw expectancy and the canonical Elo expectancy curve
    1/(1+10^(-gap/400)) over gaps 0–650 (the curve the rating system itself is
    trained on). The earlier draft value 290 understated favourites by ~6-8pp at
    typical gaps. A regression test locks this calibration.
  * Draw rates at θ=190: ~26% even game, ~21% at +200, ~13% at +400 — consistent
    with the historical World Cup draw share (~20-25% overall).
  * Known v1 limits (deliberate): independent Poisson slightly underweights
    draws in low-scoring games (Dixon-Coles correction deferred until we have
    logged results to fit ρ against, rather than inventing another constant);
    the total does not grow with the strength gap (blowout inflation is partly
    captured by the att/def texture term).

Model, per match (team_a = listed-first side; hfa_team gets the host bonus):
    sup   = (S[a] + bonus_a − S[b] − bonus_b) / θ          # consensus supremacy
    T     = ((zAtt_a − zDef_b) + (zAtt_b − zDef_a)) / 2     # matchup offensiveness
    μ     = μ0 · exp(α · T)                                  # expected total goals
    λ_a   = μ · σ(sup),  λ_b = μ · (1 − σ(sup))             # split by supremacy
    P(i,j)= Poisson(i; λ_a) · Poisson(j; λ_b), 0 ≤ i,j ≤ N  # score matrix
W/D/L, modal/expected score, Over/Under and BTTS all come from the (normalised)
matrix, so probabilities sum to 1.

Constants live in Config and are tunable; the defaults are the proposed values
pending the user's final sign-off before any prediction is published.

Importable API:
    model = load_ratings()                       # RatingModel
    pred  = predict_match(model, "Mexico", "South Korea", hfa_team="Mexico")
    md    = render_prediction(model, pred)

CLI:
    python scripts/predict.py A4                  # by match_id (HFA auto)
    python scripts/predict.py "Mexico" "South Korea" [--home "Mexico"]
    python scripts/predict.py --build-ratings     # write data/ratings.csv + team_strength.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics as stats
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st  # noqa: E402  (canon validation reuses the fixtures loader)

REPO_ROOT = Path(__file__).resolve().parents[1]
RATINGS_DIR = REPO_ROOT / "data" / "Ratings"
FIXTURES = REPO_ROOT / "data" / "fixtures.csv"

# rating-file team names -> CLAUDE.md canon (Futi uses a different convention)
ALIAS = {
    "Congo DR": "DR Congo", "Ivory Coast": "Côte d'Ivoire", "USA": "United States",
    "IR Iran": "Iran", "Korea Republic": "South Korea", "Turkey": "Türkiye",
    "Czech Republic": "Czechia", "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Cape Verde Islands": "Cape Verde",
}
# host nation (by fixtures `country`) that receives home advantage at home venues
HOST_BY_COUNTRY = {"Mexico": "Mexico", "USA": "United States", "Canada": "Canada"}

ELO_FILE = "Elo_Ratings_World_Cup_2026_VERIFIED.csv"   # NOT the corrupted original
FUTI_FILE = "World_Cup_2026_Futi_Final_Fixed_Futi_Detailed_Profiles_Final.csv"
OPTA_FILE = "Opta_Predictions_World_Cup_2026.csv"
MARKET_FILE = "Market_Outrights_VERIFIED.csv"          # real de-vigged outright market
OPTA_MATCH_FILE = "Opta_Match_Predictions.csv"         # per-match W/D/L overlay
# CLAUDE.md preferred match-level schema: match_id,p_home,p_draw,p_away,source,asof
# with p_home = the fixtures row's team_a. Aggregation rule per CLAUDE.md: simple
# average of probabilities across sources (the model counts as one source).


def _canon(name: str) -> str:
    return ALIAS.get(name.strip(), name.strip())


# ---------------------------------------------------------------- config / types

@dataclass
class Config:
    mu0: float = 2.6        # baseline total goals at even strength / even att-def
    theta: float = 190.0    # FIT to the Elo expectancy curve (see module docstring)
    alpha: float = 0.20     # Futi att/def (z) -> log expected-total goals
    hfa: float = 60.0       # host home-field advantage, in Elo-equivalent points
                            # (eloratings.net convention is +100; we discount for
                            # split crowds at neutral-ish NFL venues — revisit vs Brier)
    max_goals: int = 8      # score-matrix truncation (captures >99.99%)
    w_elo: float = 1.0      # consensus weights (equal by default)
    w_futi: float = 1.0


@dataclass
class TeamRating:
    team: str
    elo: float
    futi: float
    attack: float
    defense: float
    strength: float          # consensus, Elo-scaled
    z_att: float
    z_def: float
    elo_rank: int
    futi_rank: int
    consensus_rank: int
    opta_advance: float | None
    opta_wincup: float | None
    opta_rank: int | None
    market_odds: float | None      # real outright decimal odds (de-vig source)
    market_implied: float | None   # de-vigged outright win probability, %
    market_rank: int | None        # competition rank by market odds (ties share)
    formation: str = ""
    top_player: str = ""
    coach: str = ""


@dataclass
class RatingModel:
    teams: dict          # canon name -> TeamRating
    config: Config
    asof: str


@dataclass
class Prediction:
    team_a: str
    team_b: str
    hfa_team: str | None
    p_a: float
    p_draw: float
    p_b: float
    lambda_a: float
    lambda_b: float
    total: float
    modal_score: tuple
    over: dict            # line -> P(total > line)
    btts: float
    dnb_a: float = 0.0    # draw-no-bet: P(team_a wins | not a draw) — Phase 5 market
    top_scores: list = field(default_factory=list)   # [((i,j), prob), ...]


# ---------------------------------------------------------------- loading

def _read(path: Path) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {_canon(r["Team"]): r for r in csv.DictReader(f)}


def _zscores(values: dict) -> dict:
    mean, sd = stats.mean(values.values()), stats.pstdev(values.values())
    sd = sd or 1.0
    return {k: (v - mean) / sd for k, v in values.items()}


def _ranks(values: dict) -> dict:
    order = sorted(values, key=lambda t: -values[t])
    return {t: i for i, t in enumerate(order, 1)}


def load_ratings(ratings_dir: str | Path = RATINGS_DIR,
                 fixtures: str | Path = FIXTURES,
                 config: Config | None = None) -> RatingModel:
    """Build the consensus rating model from the verified Elo + Futi files, with
    Opta/Zeileis as context. Validates that every fixtures team resolves to a
    rating (stop-and-report on any canon mismatch, per the data contract)."""
    config = config or Config()
    ratings_dir = Path(ratings_dir)
    elo = _read(ratings_dir / ELO_FILE)
    futi = _read(ratings_dir / FUTI_FILE)
    opta = _read(ratings_dir / OPTA_FILE) if (ratings_dir / OPTA_FILE).exists() else {}
    market = _read(ratings_dir / MARKET_FILE) if (ratings_dir / MARKET_FILE).exists() else {}

    canon = set()
    for m in st.load_fixtures(fixtures):
        canon |= {m.team_a, m.team_b}
    for label, src in (("Elo", elo), ("Futi", futi)):
        missing = sorted(canon - set(src))
        if missing:
            raise ValueError(
                f"{label} ratings missing team(s) after canon normalization: "
                f"{', '.join(missing)} — fix the source or the alias map before joining")

    teams = sorted(canon)
    E = {t: float(elo[t]["Elo_Rating"]) for t in teams}
    FR = {t: float(futi[t]["Futi_Rating"]) for t in teams}
    FA = {t: float(futi[t]["Attack"]) for t in teams}
    FD = {t: float(futi[t]["Defense"]) for t in teams}

    # consensus strength: blend Elo with Futi expressed on the Elo scale
    e_mean, e_sd = stats.mean(E.values()), stats.pstdev(E.values())
    zFR = _zscores(FR)
    futi_as_elo = {t: e_mean + zFR[t] * e_sd for t in teams}
    wsum = config.w_elo + config.w_futi
    strength = {t: (config.w_elo * E[t] + config.w_futi * futi_as_elo[t]) / wsum for t in teams}

    zA, zD = _zscores(FA), _zscores(FD)
    rE, rF, rS = _ranks(E), _ranks(FR), _ranks(strength)
    rO = _ranks({t: float(opta[t]["Win_Tournament_%"]) for t in teams}) if opta else {}
    # market ranks: competition ranking so the big tie blocks (five at 66-1,
    # ten at 1000-1) share a rank instead of getting arbitrary order
    rM = {}
    if market:
        m_odds = {t: float(market[t]["Decimal_Odds"]) for t in teams if t in market}
        ordered = sorted(m_odds, key=lambda t: m_odds[t])
        for i, t in enumerate(ordered, 1):
            rM[t] = rM[ordered[i - 2]] if i > 1 and m_odds[t] == m_odds[ordered[i - 2]] else i

    table = {}
    for t in teams:
        table[t] = TeamRating(
            team=t, elo=E[t], futi=FR[t], attack=FA[t], defense=FD[t],
            strength=strength[t], z_att=zA[t], z_def=zD[t],
            elo_rank=rE[t], futi_rank=rF[t], consensus_rank=rS[t],
            opta_advance=float(opta[t]["Advance_From_Group_%"]) if opta else None,
            opta_wincup=float(opta[t]["Win_Tournament_%"]) if opta else None,
            opta_rank=rO.get(t),
            market_odds=float(market[t]["Decimal_Odds"]) if t in market else None,
            market_implied=float(market[t]["Implied_Devig_%"]) if t in market else None,
            market_rank=rM.get(t),
            formation=(futi[t].get("Formation") or "").strip(),
            top_player=(futi[t].get("Top_Player") or "").strip(),
            coach=(futi[t].get("Coach") or "").strip(),
        )
    asof = (elo[teams[0]].get("asof") or "").strip() or "unknown"
    return RatingModel(table, config, asof)


# ---------------------------------------------------------------- prediction

def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def predict_match(model: RatingModel, team_a: str, team_b: str,
                  hfa_team: str | None = None) -> Prediction:
    """Predict team_a vs team_b. ``hfa_team`` (one of the two, or None) receives
    the host home-field bonus."""
    cfg = model.config
    for t in (team_a, team_b):
        if t not in model.teams:
            raise ValueError(f"unknown team {t!r} (not in the rating model)")
    a, b = model.teams[team_a], model.teams[team_b]
    bonus_a = cfg.hfa if hfa_team == team_a else 0.0
    bonus_b = cfg.hfa if hfa_team == team_b else 0.0

    sup = (a.strength + bonus_a - b.strength - bonus_b) / cfg.theta
    texture = ((a.z_att - b.z_def) + (b.z_att - a.z_def)) / 2
    total = cfg.mu0 * math.exp(cfg.alpha * texture)
    share = 1.0 / (1.0 + math.exp(-sup))
    lam_a, lam_b = total * share, total * (1 - share)

    N = cfg.max_goals
    pa = [_poisson(i, lam_a) for i in range(N + 1)]
    pb = [_poisson(j, lam_b) for j in range(N + 1)]
    matrix = {(i, j): pa[i] * pb[j] for i in range(N + 1) for j in range(N + 1)}
    z = sum(matrix.values())
    matrix = {k: v / z for k, v in matrix.items()}      # normalise: sums to 1

    p_a = sum(p for (i, j), p in matrix.items() if i > j)
    p_draw = sum(p for (i, j), p in matrix.items() if i == j)
    p_b = sum(p for (i, j), p in matrix.items() if i < j)
    over = {line: sum(p for (i, j), p in matrix.items() if i + j > line)
            for line in (1.5, 2.5, 3.5)}
    btts = sum(p for (i, j), p in matrix.items() if i >= 1 and j >= 1)
    top = sorted(matrix.items(), key=lambda kv: -kv[1])[:3]

    dnb_a = p_a / (p_a + p_b) if (p_a + p_b) > 0 else 0.5
    return Prediction(team_a, team_b, hfa_team, p_a, p_draw, p_b, lam_a, lam_b,
                      total, top[0][0], over, btts, dnb_a, top)


# ---------------------------------------------------------------- match-level overlay

def load_match_overlay(path: str | Path = RATINGS_DIR / OPTA_MATCH_FILE) -> dict:
    """Per-match W/D/L probabilities from an external source (CLAUDE.md preferred
    schema). Accepts percentages or fractions; validates each row sums to 1 ±
    0.001 after normalisation of units, per the data contract. Returns
    {match_id: {"p_home","p_draw","p_away","source","asof"}} (fractions)."""
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            mid = (row.get("match_id") or "").strip()
            ps = [float(row[k]) for k in ("p_home", "p_draw", "p_away")]
            total = sum(ps)
            if 97.0 <= total <= 103.0:          # given as percentages
                ps = [p / 100 for p in ps]
                total = sum(ps)
            if abs(total - 1.0) > 0.001:
                raise ValueError(
                    f"{path.name}: {mid} probabilities sum to {total:.4f}, "
                    "must be 1.0 ± 0.001 (CLAUDE.md contract)")
            out[mid] = {"p_home": ps[0], "p_draw": ps[1], "p_away": ps[2],
                        "source": (row.get("source") or "").strip(),
                        "asof": (row.get("asof") or "").strip()}
    return out


def blend_wdl(pred: Prediction, overlay_row: Mapping) -> tuple:
    """Consensus W/D/L: simple average of the model and the overlay source
    (equal weights per CLAUDE.md), renormalised to sum exactly to 1."""
    pa = (pred.p_a + overlay_row["p_home"]) / 2
    pd = (pred.p_draw + overlay_row["p_draw"]) / 2
    pb = (pred.p_b + overlay_row["p_away"]) / 2
    z = pa + pd + pb
    return pa / z, pd / z, pb / z


# ---------------------------------------------------------------- rendering

def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _disagreement(model: RatingModel, t: TeamRating) -> str | None:
    """Flag when the model (Elo+Futi) and real public sentiment (de-vigged
    outright market, with Opta as secondary) rank a team very differently — a
    cue for the write-up, not a model input."""
    if t.market_rank is None or abs(t.consensus_rank - t.market_rank) < 10:
        return None   # trigger on the real market only; Opta is context, not a trigger
    bits = [f"model #{t.consensus_rank}", f"market #{t.market_rank}"]
    if t.opta_rank is not None:
        bits.append(f"Opta #{t.opta_rank}")
    return f"{t.team}: " + " vs ".join(bits)


def render_prediction(model: RatingModel, p: Prediction,
                      overlay_row: Mapping | None = None) -> str:
    a, b = model.teams[p.team_a], model.teams[p.team_b]
    # headline probabilities: consensus when a second source is present
    if overlay_row:
        hp_a, hp_d, hp_b = blend_wdl(p, overlay_row)
    else:
        hp_a, hp_d, hp_b = p.p_a, p.p_draw, p.p_b
    fav = p.team_a if hp_a >= hp_b else p.team_b
    fav_p = max(hp_a, hp_b)
    if hp_d >= max(hp_a, hp_b):
        lean = "too close to call — draw is the single most likely result"
    elif fav_p >= 0.65:
        lean = f"{fav} clear favourites"
    elif fav_p >= 0.45:
        lean = f"{fav} edge"
    else:
        lean = f"lean {fav}, but live for all three results"
    hfa = f" (🏠 {p.hfa_team} home)" if p.hfa_team else ""
    mi, mj = p.modal_score

    lines = [f"**The Call — {p.team_a} vs {p.team_b}{hfa}**", ""]
    if overlay_row:
        src = overlay_row["source"] or "external source"
        lines += [
            f"- **Consensus:** {p.team_a} {_pct(hp_a)} · Draw {_pct(hp_d)} · "
            f"{p.team_b} {_pct(hp_b)} _(simple average of the two sources below)_",
            f"  - our model: {_pct(p.p_a)} / {_pct(p.p_draw)} / {_pct(p.p_b)}",
            f"  - {src}: {_pct(overlay_row['p_home'])} / {_pct(overlay_row['p_draw'])} / "
            f"{_pct(overlay_row['p_away'])} (asof {overlay_row['asof']})",
        ]
    lines += [
        f"- **Model:** {p.team_a} {_pct(p.p_a)} · Draw {_pct(p.p_draw)} · {p.team_b} {_pct(p.p_b)}"
        if not overlay_row else
        f"- **Score model:** expected goals below are from our model (the overlay is W/D/L only)",
        f"- **Expected goals:** {p.team_a} {p.lambda_a:.2f} – {p.lambda_b:.2f} {p.team_b} "
        f"(most likely {mi}–{mj})",
        f"- **Total:** {p.total:.2f} · Over 2.5 {_pct(p.over[2.5])} · BTTS {_pct(p.btts)} "
        f"· DNB {p.team_a} {_pct(p.dnb_a)}",
        f"- **Lean:** {lean}",
        "",
        f"_Strength: {p.team_a} {a.strength:.0f} (Elo {a.elo:.0f}, Futi {a.futi:.0f}) vs "
        f"{p.team_b} {b.strength:.0f} (Elo {b.elo:.0f}, Futi {b.futi:.0f})._",
    ]
    if a.opta_advance is not None and b.opta_advance is not None:
        lines.append(f"_Opta advance-from-group: {p.team_a} {a.opta_advance:.0f}% · "
                     f"{p.team_b} {b.opta_advance:.0f}%._")
    flags = [f for f in (_disagreement(model, a), _disagreement(model, b)) if f]
    if flags:
        lines.append("_Sources diverge — " + "; ".join(flags) + "._")
    return "\n".join(lines)


# ---------------------------------------------------------------- data outputs

def write_ratings_csv(model: RatingModel, path: str | Path) -> None:
    """Consensus ratings in the CLAUDE.md team-level schema (team, rating, source, asof)."""
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["team", "rating", "source", "asof"])
        for t in sorted(model.teams, key=lambda t: -model.teams[t].strength):
            w.writerow([t, f"{model.teams[t].strength:.1f}", "consensus:elo+futi", model.asof])


def write_team_strength_csv(model: RatingModel, path: str | Path) -> None:
    """Rich per-team table (all components + tournament context + divergence flag)
    for the write-ups and odds work."""
    path = Path(path)
    cols = ["team", "consensus_rank", "strength", "elo", "elo_rank", "futi", "futi_rank",
            "attack", "defense", "opta_advance_pct", "opta_wincup_pct", "opta_rank",
            "market_odds_decimal", "market_implied_pct", "market_rank",
            "formation", "top_player", "coach", "sources_diverge"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in sorted(model.teams, key=lambda t: model.teams[t].consensus_rank):
            r = model.teams[t]
            w.writerow([
                r.team, r.consensus_rank, f"{r.strength:.1f}", f"{r.elo:.0f}", r.elo_rank,
                f"{r.futi:.0f}", r.futi_rank, f"{r.attack:.0f}", f"{r.defense:.0f}",
                "" if r.opta_advance is None else f"{r.opta_advance:.1f}",
                "" if r.opta_wincup is None else f"{r.opta_wincup:.2f}",
                r.opta_rank or "",
                "" if r.market_odds is None else f"{r.market_odds:.0f}",
                "" if r.market_implied is None else f"{r.market_implied:.3f}",
                r.market_rank or "",
                r.formation, r.top_player, r.coach,
                "yes" if _disagreement(model, r) else "",
            ])


# ---------------------------------------------------------------- CLI helpers

def _fixture_lookup(match_id: str, fixtures: str | Path = FIXTURES):
    """Return (team_a, team_b, hfa_team) for a match_id, with HFA assigned to a
    host nation playing in its own country."""
    with Path(fixtures).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("match_id") or "").strip() == match_id:
                a, b = row["team_a"].strip(), row["team_b"].strip()
                host = HOST_BY_COUNTRY.get((row.get("country") or "").strip())
                hfa = host if host in (a, b) else None
                return a, b, hfa
    raise ValueError(f"match_id {match_id!r} not found in {fixtures}")


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Predict a WC26 match from aggregated ratings.")
    ap.add_argument("teams", nargs="*", help="a match_id (e.g. A4), or two team names")
    ap.add_argument("--home", help="team name to receive home advantage (by-name mode)")
    ap.add_argument("--build-ratings", action="store_true",
                    help="write data/ratings.csv and data/team_strength.csv, then exit")
    ap.add_argument("--ratings-dir", type=Path, default=RATINGS_DIR)
    ap.add_argument("--fixtures", type=Path, default=FIXTURES)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        model = load_ratings(args.ratings_dir, args.fixtures)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.build_ratings:
        out = REPO_ROOT / "data"
        write_ratings_csv(model, out / "ratings.csv")
        write_team_strength_csv(model, out / "team_strength.csv")
        print(f"wrote {out/'ratings.csv'} and {out/'team_strength.csv'} "
              f"({len(model.teams)} teams, asof {model.asof})")
        return 0

    overlay_row = None
    if len(args.teams) == 1:
        mid = args.teams[0].strip().upper()
        try:
            a, b, hfa = _fixture_lookup(mid, args.fixtures)
            overlay_row = load_match_overlay(args.ratings_dir / OPTA_MATCH_FILE).get(mid)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    elif len(args.teams) == 2:
        a, b = args.teams
        hfa = args.home
    else:
        print("error: give a match_id, two team names, or --build-ratings", file=sys.stderr)
        return 2

    try:
        pred = predict_match(model, a, b, hfa_team=hfa)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(render_prediction(model, pred, overlay_row=overlay_row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
