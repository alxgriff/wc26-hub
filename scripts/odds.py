#!/usr/bin/env python3
"""WC26 odds & best-bet engine (PLAN.md Phase 5 / CLAUDE.md "Phase 3").

Decisions locked with the user (June 12): odds come from a free odds API
(key supplied by the user) with a manual-entry CLI fallback; the edge is
computed against the PUBLISHED consensus probability (the ledger's accountable
call); defaults confirmed: 3pp edge threshold, flat 1u stakes, 1X2 + totals
markets, >15pp sanity flag; paper units only for now.

Methodology (per CLAUDE.md, with reviewed extensions):
  * Snapshot odds at publish time to ``data/odds_log.csv``. We fetch the whole US
    region and PREFER one book per selection (DraftKings, ``--bookmaker``, the book
    actually bet at): if DK quotes the line we log only DK (so the de-vigged implied
    is the line you can really take); if it doesn't (DK supplies h2h but not totals/
    spreads via the API), we fall back to the median/best of the books that do — so
    totals/spreads stay covered. Same API quota as a single-book request (one region).
    ``--bookmaker all`` = no preference (best of all books). Each selection logs a
    MEDIAN row (fair-value reference) and the BEST price (what a bet settles at).
  * De-vig multiplicatively: implied_i = (1/odds_i) / Σ(1/odds_j). (Shin is
    the documented upgrade for longshot bias — later.)
  * Edge_i = our_p_i − implied_i, where our_p is the ledger's consensus W/D/L
    for 1X2, and the score-matrix model probability for totals (the overlay is
    W/D/L-only, so totals are model-priced — documented, not hidden).
  * Best bet = largest positive edge ≥ threshold (default 3pp), priced at the
    best book. Edges > 15pp are FLAGGED, not auto-picked ("verify odds
    freshness / team news") — today's data-corruption audits earned that rule.
    Otherwise the output is "No bet", a normal and expected result.
  * Every pick goes to ``data/picks_log.csv`` (flat 1u). ``settle`` grades
    picks once results land (units = odds−1 or −1; .5 lines cannot push) and
    computes CLV = closing implied − snapshot implied (de-vigged the same way)
    when a closing snapshot exists; missing closings stay blank, never invented.
  * Pick integrity mirrors the prediction ledger: no new/revised picks after
    kickoff; settled picks are immutable.

Schemas (CLAUDE.md base + documented extensions ``line``/``phase``/pick fields):
  odds_log.csv:  match_id, market, selection, line, odds, source, phase, timestamp
                 (market: h2h|totals; selection: home|draw|away|over|under,
                  home = the fixtures row's team_a; phase: snapshot|closing)
  picks_log.csv: match_id, market, selection, line, odds, book, edge_pp,
                 our_p, implied_p, stake, timestamp, status, units, clv_pp

CLI:
    python scripts/odds.py fetch [--phase snapshot|closing] [--bookmaker draftkings]
                                                              # odds API (key req.)
    python scripts/odds.py enter D3 h2h 2.45,3.20,3.10 --source betmgm
    python scripts/odds.py enter D3 totals 2.5 1.95,1.87 --source betmgm
    python scripts/odds.py evaluate 2026-06-13                # edges + best bets
    python scripts/odds.py settle                              # grade picks + CLV
    python scripts/odds.py report                              # units/CLV summary

API key: env ODDS_API_KEY, or the git-ignored file data/.odds_api_key.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st      # noqa: E402
import build_edition as be  # noqa: E402  (editorial slate)
import ledger as lg         # noqa: E402  (kickoff guard, consensus probabilities)
import predict as pr        # noqa: E402  (totals model, name canon)

REPO_ROOT = Path(__file__).resolve().parents[1]
ODDS_LOG = REPO_ROOT / "data" / "odds_log.csv"
PICKS_LOG = REPO_ROOT / "data" / "picks_log.csv"
SHADOW_PICKS_LOG = REPO_ROOT / "data" / "shadow_picks_log.csv"   # RISKY calls — a
                            # SEVERE model<->market disagreement (edge above the sanity
                            # ceiling). Tracked + settled like real picks at stake 0
                            # (paper, too risky to bet) to learn whether the model's
                            # strong convictions land. Kept strictly OUT of the
                            # accountable units/CLV record (units_summary).
KEY_FILE = REPO_ROOT / "data" / ".odds_api_key"
API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "soccer_fifa_world_cup"

ODDS_COLUMNS = ["match_id", "market", "selection", "line", "odds",
                "source", "phase", "timestamp"]
PICK_COLUMNS = ["match_id", "market", "selection", "line", "odds", "book",
                "edge_pp", "our_p", "implied_p", "stake", "timestamp",
                "status", "units", "clv_pp"]

EDGE_THRESHOLD = 0.03      # user-tunable via --threshold
SANITY_EDGE = 0.15         # corroborated 1X2 (vs consensus): flag above this
# Model-priced markets (totals/spreads/BTTS) are priced from the SAME score matrix
# that generates the edge — there is no independent consensus cross-check (1X2 has
# one; see the module docstring) — so a large edge is far more likely model error
# than market error. They clear a STRICTER sanity ceiling (user-set June 14):
# flagged above MODEL_PRICED_SANITY, recordable only in [RECORD_THRESHOLD, this).
MODEL_PRICED_MARKETS = ("totals", "spreads", "btts", "advance")
MODEL_PRICED_SANITY = 0.08
H2H_SELECTIONS = ("home", "draw", "away")
KO_ADVANCE_MARKET = "advance"   # knockout 2-way: home = team_a advances, away = team_b
                                # advances. Model-priced (predict.resolve_knockout); the
                                # ONLY knockout market we record, because it settles
                                # cleanly + penalty-aware from knockout.csv's winner side.

# Prefer-book sourcing (June 14 single-book → June 16 prefer-else-fallback): fetch the
# whole US region and PREFER this book per selection (the one the user bets) so the
# de-vigged implied is the line they can take; fall back to the other US books where it
# doesn't quote (DK supplies h2h but not totals/spreads via the API). --bookmaker
# overrides; --bookmaker all = no preference (best of all books).
DEFAULT_BOOKMAKER = "draftkings"
BOOK_DISPLAY = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM",
                "caesars": "Caesars", "pointsbetus": "PointsBet",
                "betrivers": "BetRivers"}


def _book_display(key: str) -> str:
    """Pretty book name for provenance notes; unknown keys pass through as-is."""
    return BOOK_DISPLAY.get(key, key)


def american_odds(decimal: float) -> str:
    """Decimal odds -> American moneyline string (the intuitive US form):
    2.50 -> '+150', 1.50 -> '-200', 2.00 -> '+100'. Both sides always carry a
    sign so '+120' / '-200' read unambiguously. Decimal <= 1.0 cannot occur
    (devig rejects it) but degrades to 'n/a' rather than dividing by zero.
    Display only — odds_log/picks_log stay decimal, the canonical math form."""
    if decimal <= 1.0:
        return "n/a"
    if decimal >= 2.0:
        return f"+{round((decimal - 1) * 100)}"
    return f"-{round(100 / (decimal - 1))}"


class OddsError(ValueError):
    pass


# ---------------------------------------------------------------- core math

def devig(odds: list) -> list:
    """Multiplicative de-vig: implied_i = (1/odds_i) / Σ(1/odds_j)."""
    if any(o <= 1.0 for o in odds):
        raise OddsError(f"decimal odds must be > 1.0, got {odds}")
    raw = [1.0 / o for o in odds]
    z = sum(raw)
    return [r / z for r in raw]


def totals_probs(lambda_a: float, lambda_b: float, line: float,
                 n: int = 12) -> tuple:
    """(P(over), P(push), P(under)) for any totals line from the independent-
    Poisson score matrix. P(push) is nonzero only on integer lines, where the
    total can land exactly on the line and the stake is refunded."""
    pa = [math.exp(-lambda_a) * lambda_a ** i / math.factorial(i) for i in range(n + 1)]
    pb = [math.exp(-lambda_b) * lambda_b ** j / math.factorial(j) for j in range(n + 1)]
    z = sum(pa) * sum(pb)
    under = sum(pa[i] * pb[j] for i in range(n + 1) for j in range(n + 1)
                if i + j < line) / z
    push = sum(pa[i] * pb[j] for i in range(n + 1) for j in range(n + 1)
               if abs(i + j - line) < 1e-9) / z
    return (1.0 - under - push, push, under)


def prob_over(lambda_a: float, lambda_b: float, line: float, n: int = 12) -> float:
    """P(total goals > line) for half lines (kept for callers that cannot
    handle a push; integer lines must go through totals_probs)."""
    if abs(line - round(line)) < 1e-9:
        raise OddsError(f"integer totals line {line} can push — use totals_probs")
    over, _push, _under = totals_probs(lambda_a, lambda_b, line, n)
    return over


def margin_dist(lambda_a: float, lambda_b: float, n: int = 12) -> dict:
    """{goal margin (a − b): probability} from the independent-Poisson matrix."""
    pa = [math.exp(-lambda_a) * lambda_a ** i / math.factorial(i) for i in range(n + 1)]
    pb = [math.exp(-lambda_b) * lambda_b ** j / math.factorial(j) for j in range(n + 1)]
    z = sum(pa) * sum(pb)
    out: dict = {}
    for i in range(n + 1):
        for j in range(n + 1):
            out[i - j] = out.get(i - j, 0.0) + pa[i] * pb[j] / z
    return out


def _ah_components(handicap: float) -> list:
    """A quarter line splits the stake across its two neighbours; other lines
    are a single component."""
    q = round(handicap * 4)
    if q % 2 == 1:                      # ±0.25, ±0.75, ±1.25, ...
        return [(q - 1) / 4, (q + 1) / 4]
    return [handicap]


def ah_effective(margins: dict, handicap: float) -> tuple:
    """(W_eff, L_eff) for an Asian-handicap side whose perspective-margin
    distribution is ``margins`` and line is ``handicap`` (e.g. home −0.75 with
    margins from home's perspective). Pushes are excluded from both; quarter
    lines weight each half-stake component 50/50. Fair odds = (W+L)/W, so the
    de-vig-comparable probability is W_eff / (W_eff + L_eff)."""
    comps = _ah_components(handicap)
    w = l = 0.0
    for h in comps:
        w += sum(p for m, p in margins.items() if m + h > 1e-9)
        l += sum(p for m, p in margins.items() if m + h < -1e-9)
    return w / len(comps), l / len(comps)


def ah_prob(margins: dict, handicap: float) -> float:
    """Push-adjusted win probability comparable to a de-vigged two-way price."""
    w, l = ah_effective(margins, handicap)
    if w + l <= 0:
        raise OddsError(f"degenerate handicap {handicap}")
    return w / (w + l)


def ah_settle_units(margin_sel: int, handicap: float, odds: float) -> tuple:
    """(units, status) for a settled AH pick: ``margin_sel`` is the final goal
    margin from the selection's perspective, stake 1u split across quarter-line
    components (half-win +/− (odds−1)/2, push component 0)."""
    comps = _ah_components(handicap)
    units = 0.0
    results = []
    for h in comps:
        adj = margin_sel + h
        if adj > 1e-9:
            units += (odds - 1) / len(comps)
            results.append("w")
        elif adj < -1e-9:
            units -= 1.0 / len(comps)
            results.append("l")
        else:
            results.append("p")
    status = {"w": "won", "l": "lost", "p": "push"}[results[0]] if len(set(results)) == 1 \
        else ("half-won" if "w" in results else "half-lost")
    return units, status


# ---------------------------------------------------------------- log I/O

def _load(path: Path, columns: list) -> list:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _save(rows: list, path: Path, columns: list) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in columns} for r in rows)


def load_odds(path: Path = ODDS_LOG) -> list:
    return _load(path, ODDS_COLUMNS)


def _dedupe_picks(rows: list) -> list:
    """Heal a union-merge that duplicated a pick row on a rebase race (the picks
    log is merge=union per .gitattributes). For one (match_id, market, selection,
    line): prefer the SETTLED copy over an open one so units aren't double-counted;
    two settled copies that DISAGREE are a real corruption — stop and report."""
    settled = lambda r: (r.get("status") or "") not in ("", "open")
    by_key: dict = {}
    order: list = []
    for r in rows:
        k = (r["match_id"], r["market"], r["selection"], _fmt_line(r.get("line", "")))
        if k not in by_key:
            by_key[k] = r
            order.append(k)
        elif settled(by_key[k]) and settled(r):
            if (by_key[k].get("status"), by_key[k].get("units")) != (r.get("status"), r.get("units")):
                raise OddsError(f"picks log has contradictory settled rows for {k} "
                                "— resolve the duplicate by hand")
        elif settled(r):
            by_key[k] = r       # prefer the settled copy over the open one
    return [by_key[k] for k in order]


def load_picks(path: Path = PICKS_LOG) -> list:
    return _dedupe_picks(_load(path, PICK_COLUMNS))


def append_odds(rows: list, path: Path = ODDS_LOG) -> None:
    existing = load_odds(path)
    _save(existing + rows, path, ODDS_COLUMNS)


def _fmt_line(line) -> str:
    """Canonical string form of a market line so writes and look-ups always
    agree: '3.0'/'3'/3 -> '3', '-1.0' -> '-1', '2.5' -> '2.5', ''/None -> ''.
    The single formatter for every line write and key — whole-number handicaps
    and totals were silently dropped when written '3.0' but keyed '3'."""
    if line is None or line == "":
        return ""
    try:
        return f"{float(line):g}"
    except (TypeError, ValueError):
        return str(line)


def latest_market(odds_rows: list, match_id: str, market: str,
                  phase: str = "snapshot", source_prefix: str = "median") -> dict:
    """The most recent {(selection, line): (odds, n_books)} for a
    match/market/phase from rows whose source starts with ``source_prefix``.
    Line-aware because different books quote different main lines — pairing an
    Over at one line with an Under at another would be an invalid de-vig."""
    rows = [r for r in odds_rows
            if r["match_id"] == match_id and r["market"] == market
            and r["phase"] == phase and r["source"].startswith(source_prefix)]
    if not rows:
        return {}
    last_ts = max(r["timestamp"] for r in rows)
    out = {}
    for r in rows:
        if r["timestamp"] != last_ts:
            continue
        m = re.search(r"/(\d+)books", r["source"])
        out[(r["selection"], _fmt_line(r["line"]))] = (float(r["odds"]),
                                                       int(m.group(1)) if m else 1)
    return out


def snapshot_source_label(odds_rows: list, match_id: str,
                          phase: str = "snapshot") -> str:
    """Human provenance for a match's snapshot, for the edition/site note. Reflects
    the prefer-book-else-fall-back sourcing: pure DraftKings reads "DraftKings line";
    pure multi-book reads "median across N books"; the common MIX (DK h2h + multi-book
    totals/spreads) reads "DraftKings where quoted, else best of N US books". "" when
    there is no snapshot. The label must never describe a sourcing the log lacks."""
    rows = [r for r in odds_rows
            if r["match_id"] == match_id and r["phase"] == phase]
    if not rows:
        return ""
    last_ts = max(r["timestamp"] for r in rows)
    latest = [r for r in rows if r["timestamp"] == last_ts]
    has_dk = any(r["source"] == "best:draftkings" for r in latest)
    multi_n = max((int(m.group(1)) for r in latest
                   if (m := re.search(r"/(\d+)books", r["source"])) and int(m.group(1)) > 1),
                  default=0)
    if has_dk and multi_n:
        return f"DraftKings where quoted, else best of {multi_n} US books"
    if has_dk:
        return "DraftKings line"
    if multi_n:
        return f"median across {multi_n} books"
    books = {r["source"].split("best:", 1)[1] for r in latest
             if r["source"].startswith("best:")}
    if len(books) == 1:
        return f"{_book_display(next(iter(books)))} line"
    return "single book"


def _latest_phase_ts(odds_rows: list, match_id: str, market: str, phase: str,
                     source_prefix: str = "median") -> str | None:
    """Timestamp string of the most recent rows latest_market() would use for a
    match/market/phase, or None — lets the caller judge how close a 'closing'
    snapshot actually is to kickoff."""
    ts = [r["timestamp"] for r in odds_rows
          if r["match_id"] == match_id and r["market"] == market
          and r["phase"] == phase and r["source"].startswith(source_prefix)]
    return max(ts) if ts else None


def all_paired_lines(market_data: dict, sel_a: str, sel_b: str,
                     negate_b: bool = False) -> list:
    """Every line quoted on BOTH sides of a two-way market, as
    [(line_a, odds_a, odds_b), ...] sorted by the line value. For spreads the
    b-side line is the negation of the a-side line (``negate_b``)."""
    out = []
    for (sel, line), (odds_a, n_a) in market_data.items():
        if sel != sel_a:
            continue
        b_line = line
        if negate_b and line:
            b_line = _fmt_line(-float(line))
        match_b = market_data.get((sel_b, b_line))
        if match_b:
            out.append((line, odds_a, match_b[0], n_a + match_b[1]))
    out.sort(key=lambda c: float(c[0]) if c[0] else 0.0)
    return [(line, oa, ob) for line, oa, ob, _n in out]


def paired_lines(market_data: dict, sel_a: str, sel_b: str,
                 negate_b: bool = False) -> tuple | None:
    """The best-supported line quoted on BOTH sides of a two-way market.
    Returns (line_a, odds_a, odds_b). None if no complete pair."""
    candidates = []
    for (sel, line), (odds_a, n_a) in market_data.items():
        if sel != sel_a:
            continue
        b_line = line
        if negate_b and line:
            b_line = _fmt_line(-float(line))
        match_b = market_data.get((sel_b, b_line))
        if match_b:
            candidates.append((n_a + match_b[1], line, odds_a, match_b[0]))
    if not candidates:
        return None
    _, line, odds_a, odds_b = max(candidates, key=lambda c: c[0])
    return line, odds_a, odds_b


# ---------------------------------------------------------------- evaluation

def consensus_probs(match_id: str, ledger_rows: list) -> tuple | None:
    """The PUBLISHED consensus W/D/L from the prediction ledger (the accountable
    number we bet against). None if not logged, OR if the logged probabilities
    fail the 1.0±0.001 contract — a corrupt row must never silently drive an
    (immutable) recorded bet. Same gate the site uses to suppress the call."""
    row = next((r for r in ledger_rows
                if r["match_id"] == match_id and r["source"] == lg.PUBLISHED_SOURCE), None)
    if row is None:
        return None
    if not lg.probs_valid((row.get("p_home"), row.get("p_draw"), row.get("p_away"))):
        return None
    return float(row["p_home"]), float(row["p_draw"]), float(row["p_away"])


def evaluate_match(match_id: str, odds_rows: list, ledger_rows: list,
                   pred: "pr.Prediction | None") -> dict:
    """Edge table for one match. Returns
    {"h2h": [(selection, line, median_odds, implied, our_p, edge)], "totals": [...],
     "missing": [...notes...]}."""
    out = {"h2h": [], "totals": [], "spreads": [], "btts": [], "missing": []}

    h2h = latest_market(odds_rows, match_id, "h2h")
    probs = consensus_probs(match_id, ledger_rows)
    if h2h and all((s, "") in h2h for s in H2H_SELECTIONS):
        if probs is None:
            published = any(r["match_id"] == match_id
                            and r["source"] == lg.PUBLISHED_SOURCE for r in ledger_rows)
            out["missing"].append(
                "logged consensus fails the 1.0±0.001 probability contract — "
                "1X2 edge not computed, pick suppressed" if published
                else "no logged consensus prediction — 1X2 edge not computed")
        else:
            odds3 = [h2h[(s, "")][0] for s in H2H_SELECTIONS]
            implied = devig(odds3)
            for i, sel in enumerate(H2H_SELECTIONS):
                out["h2h"].append((sel, "", odds3[i], implied[i], probs[i],
                                   probs[i] - implied[i]))
    elif h2h:
        out["missing"].append("incomplete 1X2 snapshot (need home/draw/away)")
    else:
        out["missing"].append("no 1X2 snapshot")

    totals = latest_market(odds_rows, match_id, "totals")
    pairs = all_paired_lines(totals, "over", "under") if totals else []
    if pairs and pred is not None:
        for line, o_over, o_under in pairs:
            try:
                p_over, p_push, p_under = totals_probs(
                    pred.lambda_a, pred.lambda_b, float(line))
            except (ValueError, OddsError) as e:
                out["missing"].append(f"O/U {line}: {e}")
                continue
            action = p_over + p_under
            if action <= 0:
                continue
            # books refund pushes, so quoted odds (and their de-vig) are
            # conditional on action — our probabilities must match that basis
            implied = devig([o_over, o_under])
            for sel, o, imp, p in (("over", o_over, implied[0], p_over / action),
                                   ("under", o_under, implied[1], p_under / action)):
                out["totals"].append((sel, line, o, imp, p, p - imp))
            if p_push > 0.005:
                out["missing"].append(
                    f"O/U {line} can push (P {p_push:.0%}) — probabilities and "
                    "edge are per unit at risk; a push refunds the stake")
    elif totals:
        out["missing"].append("totals quoted but no line has both over and under")
    else:
        out["missing"].append("no totals snapshot")

    # Asian handicap and BTTS are optional markets: evaluated when present,
    # silent when absent. Both are model-priced (the overlay is W/D/L-only).
    spreads = latest_market(odds_rows, match_id, "spreads")
    pair = paired_lines(spreads, "home", "away", negate_b=True) if spreads else None
    if pair and pred is not None:
        h_line, o_home, o_away = pair
        try:
            margins = margin_dist(pred.lambda_a, pred.lambda_b)
            p_home = ah_prob(margins, float(h_line))
            implied = devig([o_home, o_away])
            a_line = _fmt_line(-float(h_line))
            for sel, line, o, imp, p in (
                    ("home", h_line, o_home, implied[0], p_home),
                    ("away", a_line, o_away, implied[1], 1 - p_home)):
                out["spreads"].append((sel, line, o, imp, p, p - imp))
        except OddsError as e:
            out["missing"].append(f"spreads: {e}")
    elif spreads:
        out["missing"].append("spreads quoted but no line has both sides")

    btts = latest_market(odds_rows, match_id, "btts")
    if btts and ("yes", "") in btts and ("no", "") in btts and pred is not None:
        odds2 = [btts[("yes", "")][0], btts[("no", "")][0]]
        implied = devig(odds2)
        for i, (sel, p) in enumerate((("yes", pred.btts), ("no", 1 - pred.btts))):
            out["btts"].append((sel, "", odds2[i], implied[i], p, p - implied[i]))
    elif btts:
        out["missing"].append("incomplete BTTS snapshot (need yes + no)")
    return out


def evaluate_ko_match(match_no: int, team_a: str, team_b: str,
                      odds_rows: list, model, country: str = "") -> dict:
    """Edge table for a resolved knockout tie. The bettable market is ADVANCE (to
    qualify): model-priced from predict.resolve_knockout (the 90' consensus routed
    through extra time + a coin-flip shootout), de-vigged against a 2-way 'advance'
    market when one is quoted. Returns the evaluate_match shape (all market keys present
    so render_market stays safe), with edges only in 'advance'.

    Advance-only by design: the standard odds-API soccer market is the 90-MINUTE 3-way
    result, which (a) has no consensus ledger for knockouts and (b) cannot be settled
    from the post-extra-time score in knockout.csv. Advance settles cleanly + penalty-
    aware from the winner side, so it is the only knockout market we record (a 90' market,
    if present, is the caller's to display — never recorded here)."""
    out = {"h2h": [], "totals": [], "spreads": [], "btts": [], "advance": [], "missing": []}
    if not (team_a and team_b):
        out["missing"].append("matchup not resolved yet — no advance call")
        return out
    mid = f"M{match_no}"
    kp = pr.resolve_knockout(model, team_a, team_b,
                             hfa_team=pr.host_hfa(country, team_a, team_b))  # host at home in KO
    adv = latest_market(odds_rows, mid, KO_ADVANCE_MARKET)
    if adv and ("home", "") in adv and ("away", "") in adv:
        # a REAL, quoted 2-way to-qualify line (rare — via manual `enter advance`): recordable
        o = [adv[("home", "")][0], adv[("away", "")][0]]
        implied = devig(o)                       # 2-way multiplicative de-vig (Σ implied = 1)
        for i, (sel, p) in enumerate((("home", kp.p_advance_a), ("away", kp.p_advance_b))):
            out["advance"].append((sel, "", o[i], implied[i], p, p - implied[i]))
        return out
    if adv:
        out["missing"].append("incomplete advance market (need both sides)")
        return out
    # No quoted to-qualify market (the norm — The Odds API carries none). DERIVE the market's
    # advance probability from the fetched 90' 3-way h2h, routing its draw mass through the SAME
    # extra-time + coin-flip-shootout layer the model uses. This is a model-vs-market READ
    # (vig-free fair probability, not a price you can take) — shown, but NEVER recorded: the ET
    # layer is ours on both sides, so the edge is just the 90' h2h disagreement on the advance
    # axis (a self-priced quantity, hence the model-priced 8pp framing — and no real line/CLV).
    h = latest_market(odds_rows, mid, "h2h")
    if h and all((sel, "") in h for sel in ("home", "draw", "away")):
        q = devig([h[("home", "")][0], h[("draw", "")][0], h[("away", "")][0]])  # 90' h/d/a
        et_a, et_d, _et_b = kp.et_wdl                       # model's ET conditional W/D/L
        madv_a = q[0] + q[1] * (et_a + et_d * 0.5)          # route draw mass; 0.5 = flat shootout
        madv = (madv_a, 1.0 - madv_a)
        for i, (sel, p) in enumerate((("home", kp.p_advance_a), ("away", kp.p_advance_b))):
            out["advance"].append((sel, "", None, madv[i], p, p - madv[i]))  # None odds = no quoted price
        out["advance_derived"] = True
        out["missing"].append(
            "advance edge derived from the 90' market (no quoted 'to qualify' line) — a "
            "model-vs-market read, not a recorded bet")
    else:
        out["missing"].append("no market snapshot for this tie yet — advance call shown, nothing priced")
    return out


RECORD_THRESHOLD = 0.04    # picks must clear a higher bar than table display.
                           # Lowered 0.05->0.04 (user, June 14) to gather more model-
                           # performance data once the Maher fix left the model well-
                           # calibrated — a measured experiment: watch the [4,5)pp band's
                           # CLV and revert if it's negative. Still ~4x the de-vig noise.
MAX_PICKS_PER_MATCH = 3    # top edges across DISTINCT markets


def _market_sanity(market: str, sanity: float, model_sanity: float) -> float:
    """Flag ceiling for a market. Model-priced markets (no independent
    corroboration) clear a stricter bar than consensus-checked 1X2."""
    return model_sanity if market in MODEL_PRICED_MARKETS else sanity


def best_bets(evaluation: dict, threshold: float = RECORD_THRESHOLD,
              sanity: float = SANITY_EDGE, model_sanity: float = MODEL_PRICED_SANITY,
              limit: int = MAX_PICKS_PER_MATCH) -> tuple:
    """(picks, flags): the best selection per market with edge ≥ threshold
    and < the market's sanity ceiling, ranked by edge, capped at ``limit``. One
    pick per market by construction — ladder lines within a market are mutually
    exclusive alternatives, and same-match picks are correlated enough already
    (they tend to win and lose together; the units record swings accordingly).
    Edges at/above the ceiling become flags, never picks. The ceiling is market-
    aware: consensus-checked 1X2 uses ``sanity`` (15pp); model-priced totals/
    spreads/BTTS use the stricter ``model_sanity`` (8pp) because a large edge on a
    self-priced market is far more likely our miscalibration than market error."""
    flags = []
    per_market: dict = {}
    for market in ("h2h", "totals", "spreads", "btts", "advance"):
        # a knockout advance edge DERIVED from the 90' line is a display-only model-vs-market
        # read (no quoted to-qualify price, no CLV) — shown, never recorded.
        if market == "advance" and evaluation.get("advance_derived"):
            continue
        ceiling = _market_sanity(market, sanity, model_sanity)
        for sel, line, odds, implied, our_p, edge in evaluation.get(market, []):
            if edge >= ceiling:   # inclusive: an edge AT the ceiling is the case the rule targets
                tag = f"{market} {sel}{f' {line}' if line else ''}"
                if market in MODEL_PRICED_MARKETS:
                    flags.append(f"{tag}: model-priced edge {edge:+.1%} exceeds the "
                                 f"{ceiling:.0%} cap for uncorroborated markets (priced "
                                 "from our own score matrix, no consensus cross-check) "
                                 "— not recorded")
                else:
                    flags.append(f"{tag}: edge {edge:+.1%} implausibly large — verify "
                                 "odds freshness and team news before trusting")
            elif edge >= threshold:
                cand = {"market": market, "selection": sel, "line": line,
                        "odds": odds, "implied_p": implied,
                        "our_p": our_p, "edge": edge}
                cur = per_market.get(market)
                if cur is None or cand["edge"] > cur["edge"]:
                    per_market[market] = cand
    picks = sorted(per_market.values(), key=lambda c: -c["edge"])[:limit]
    return picks, flags


def best_bet(evaluation: dict, threshold: float = EDGE_THRESHOLD,
             sanity: float = SANITY_EDGE,
             model_sanity: float = MODEL_PRICED_SANITY) -> tuple:
    """(pick | None, flags): the single largest qualifying edge at the
    display threshold. Kept for compatibility; recording uses best_bets."""
    picks, flags = best_bets(evaluation, threshold=threshold, sanity=sanity,
                             model_sanity=model_sanity, limit=1)
    return (picks[0] if picks else None), flags


def flagged_bets(evaluation: dict, sanity: float = SANITY_EDGE,
                 model_sanity: float = MODEL_PRICED_SANITY,
                 limit: int = MAX_PICKS_PER_MATCH) -> list:
    """The model's extreme convictions the sanity ceiling SUPPRESSES — the best
    selection per market whose edge is AT/ABOVE the ceiling (the ones best_bets turns
    into flags, never picks). Returned as pick dicts for the SHADOW ledger: tracked and
    settled like real picks but never bet, so we can learn whether the model's
    over-confident calls actually win without staking the blind spot."""
    per_market: dict = {}
    for market in ("h2h", "totals", "spreads", "btts"):
        ceiling = _market_sanity(market, sanity, model_sanity)
        for sel, line, odds, implied, our_p, edge in evaluation.get(market, []):
            if edge >= ceiling:
                cand = {"market": market, "selection": sel, "line": line,
                        "odds": odds, "implied_p": implied, "our_p": our_p, "edge": edge}
                cur = per_market.get(market)
                if cur is None or cand["edge"] > cur["edge"]:
                    per_market[market] = cand
    return sorted(per_market.values(), key=lambda c: -c["edge"])[:limit]


# ---------------------------------------------------------------- picks ledger

def record_pick(match_id: str, pick: dict, best_price: tuple, now: datetime,
                kickoff_passed: bool, picks_path: Path = PICKS_LOG,
                allow_revise: bool = False, stake: str = "1") -> str:
    """Append/refresh the pick for (match_id, market). Best_price = (odds, book)
    actually loggable; refuses post-kickoff and never touches settled rows.

    A pick, once recorded, is a published commitment: re-recording the exact
    same selection/line/odds is a no-op that preserves the ORIGINAL row and
    timestamp; anything that would change it raises unless ``allow_revise``
    is passed explicitly (CLI --revise). No silent re-pricing."""
    picks = load_picks(picks_path)
    existing = next((p for p in picks if p["match_id"] == match_id
                     and p["market"] == pick["market"]), None)
    if existing and existing["status"] != "open":
        raise OddsError(f"{match_id} {pick['market']}: pick already settled — immutable")
    if kickoff_passed:
        raise OddsError(f"{match_id}: kickoff has passed — no new or revised picks")
    odds, book = best_price
    row = {"match_id": match_id, "market": pick["market"], "selection": pick["selection"],
           "line": _fmt_line(pick["line"]), "odds": f"{odds:.2f}", "book": book,
           "edge_pp": f"{pick['edge'] * 100:.1f}", "our_p": f"{pick['our_p']:.4f}",
           "implied_p": f"{pick['implied_p']:.4f}", "stake": stake,
           "timestamp": now.isoformat(timespec="seconds"),
           "status": "open", "units": "", "clv_pp": ""}
    if existing:
        identical = (str(existing["selection"]) == str(row["selection"])
                     and _fmt_line(existing["line"]) == _fmt_line(row["line"])
                     and float(existing["odds"]) == float(row["odds"]))
        if identical:
            return (f"{match_id}: {pick['market']} pick unchanged — already "
                    f"recorded at {existing['timestamp']}")
        if not allow_revise:
            raise OddsError(
                f"{match_id} {pick['market']}: pick already recorded "
                f"({existing['selection']} {existing['line'] or ''} @ "
                f"{existing['odds']}, {existing['timestamp']}) and the new values "
                f"differ ({row['selection']} {row['line'] or ''} @ {row['odds']}) "
                "— pass --revise to supersede explicitly")
        existing.update(row)
    else:
        picks.append(row)
    _save(picks, picks_path, PICK_COLUMNS)
    return (f"{match_id}: {pick['market']} {pick['selection']}"
            f"{f' {pick['line']}' if pick['line'] else ''} @ {odds:.2f} ({book}), "
            f"edge {pick['edge']:+.1%}"
            + (" [REVISED]" if existing and allow_revise else ""))


def _market_keys(market: str, selection: str, line: str) -> list:
    """Ordered (selection, line) keys spanning the full market a pick belongs
    to, with spreads lines negated for the opposite side."""
    if market == "h2h":
        return [(s, "") for s in H2H_SELECTIONS]
    if market == "btts":
        return [("yes", ""), ("no", "")]
    if market == KO_ADVANCE_MARKET:
        return [("home", ""), ("away", "")]
    line = _fmt_line(line)
    if market == "totals":
        return [("over", line), ("under", line)]
    other = _fmt_line(-float(line)) if line else ""
    return ([("home", line), ("away", other)] if selection == "home"
            else [("home", other), ("away", line)])


CLOSING_WINDOW = timedelta(hours=6)   # a "closing" snapshot must sit within this
                                      # of kickoff (before it) to count for CLV


def _closing_is_timely(odds_rows: list, pick: dict, kickoff) -> bool:
    """A closing snapshot counts for CLV only when it was taken within
    CLOSING_WINDOW before kickoff. An unknown kickoff, or a timestamp that is far
    from / after kickoff, means it is not a real close (the bulk June-12 snapshot
    is tagged 'closing' but covers matches out to June 27) — so CLV stays blank
    rather than being computed against a non-closing line (never invented)."""
    if kickoff is None:
        return False
    ts = _latest_phase_ts(odds_rows, pick["match_id"], pick["market"], "closing")
    if not ts:
        return False
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return timedelta(0) <= (kickoff - t) <= CLOSING_WINDOW


def _ko_fixture_row(km) -> dict:
    """A fixtures-row-shaped dict for a knockout match so ledger.kickoff_dt can derive
    its kickoff for the CLV timeliness window."""
    return {"match_id": f"M{km.match_no}", "date_et": km.date_et,
            "kickoff_et_24h": km.kickoff_et_24h, "kickoff_et": km.kickoff_et,
            "team_a": km.team_a, "team_b": km.team_b}


def _settle_clv_line(p: dict, odds_rows: list, kickoff_by_mid: dict) -> str:
    """Compute CLV for a freshly-graded pick — mutating p['clv_pp'] only when a TRUE
    closing line exists (within CLOSING_WINDOW of kickoff) — and return its status line.
    Shared by the group and knockout settle paths so they de-vig CLV identically."""
    closing = latest_market(odds_rows, p["match_id"], p["market"], phase="closing")
    keys = _market_keys(p["market"], p["selection"], p["line"])
    if (closing and all(k in closing for k in keys)
            and _closing_is_timely(odds_rows, p, kickoff_by_mid.get(p["match_id"]))):
        implied = devig([closing[k][0] for k in keys])
        idx = [k[0] for k in keys].index(p["selection"])
        clv = implied[idx] - float(p["implied_p"])
        p["clv_pp"] = f"{clv * 100:+.1f}"
    return (f"{p['match_id']} {p['market']} {p['selection']}: "
            f"{p['status']} {p['units']}u"
            + (f", CLV {p['clv_pp']}pp" if p["clv_pp"] else ", CLV n/a"))


def settle_picks(matches: list, odds_rows: list,
                 picks_path: Path = PICKS_LOG,
                 fixtures_rows: list | None = None,
                 knockout: list | None = None) -> list:
    """Grade open picks whose match is played: units (odds−1 won, −1 lost) and
    CLV vs the closing snapshot when a TRUE one exists. Group picks settle from
    ``matches`` (fixtures); knockout ADVANCE picks settle from ``knockout``
    (data/knockout.csv) by the winner side — penalty-aware, since the winner is
    authoritative even when the score is level. ``fixtures_rows`` supply kickoff times so
    a row tagged 'closing' but logged far from kickoff is rejected for CLV; knockout
    kickoffs are derived from the knockout rows."""
    picks = load_picks(picks_path)
    by_mid = {m.match_id: m for m in matches if m.is_played}
    ko_by_mid = {f"M{km.match_no}": km for km in (knockout or []) if km.is_played}
    kickoff_by_mid = {}
    for row in (fixtures_rows or []):
        try:
            kickoff_by_mid[row["match_id"]] = lg.kickoff_dt(row)
        except (KeyError, ValueError):
            continue
    for km in (knockout or []):
        try:
            kickoff_by_mid[f"M{km.match_no}"] = lg.kickoff_dt(_ko_fixture_row(km))
        except Exception:
            pass                                 # CLV stays blank if the kickoff is unknown
    lines = []
    for p in picks:
        if p["status"] != "open":
            continue
        if p["market"] == KO_ADVANCE_MARKET:
            km = ko_by_mid.get(p["match_id"])
            if km is None:
                continue
            won = ((p["selection"] == "home" and km.winner == "A")
                   or (p["selection"] == "away" and km.winner == "B"))
            p["status"] = "won" if won else "lost"
            p["units"] = f"{float(p['odds']) - 1:+.2f}" if won else "-1.00"
            lines.append(_settle_clv_line(p, odds_rows, kickoff_by_mid))
            continue
        if p["match_id"] not in by_mid:
            continue
        m = by_mid[p["match_id"]]
        if p["market"] == "h2h":
            outcome = ("home", "draw", "away")[lg.outcome_index(m.score_a, m.score_b)]
            won = p["selection"] == outcome
            p["status"] = "won" if won else "lost"
            p["units"] = f"{float(p['odds']) - 1:+.2f}" if won else "-1.00"
        elif p["market"] == "totals":
            total = m.score_a + m.score_b
            line = float(p["line"])
            if abs(total - line) < 1e-9:        # integer line landed exactly
                p["status"] = "push"
                p["units"] = "+0.00"
            else:
                won = (total > line) == (p["selection"] == "over")
                p["status"] = "won" if won else "lost"
                p["units"] = f"{float(p['odds']) - 1:+.2f}" if won else "-1.00"
        elif p["market"] == "spreads":
            margin = m.score_a - m.score_b
            margin_sel = margin if p["selection"] == "home" else -margin
            units, status = ah_settle_units(margin_sel, float(p["line"]),
                                            float(p["odds"]))
            p["status"] = status
            p["units"] = f"{units:+.2f}"
        else:  # btts
            both = m.score_a >= 1 and m.score_b >= 1
            won = both == (p["selection"] == "yes")
            p["status"] = "won" if won else "lost"
            p["units"] = f"{float(p['odds']) - 1:+.2f}" if won else "-1.00"

        lines.append(_settle_clv_line(p, odds_rows, kickoff_by_mid))
    _save(picks, picks_path, PICK_COLUMNS)
    return lines


def units_summary(picks: list) -> str | None:
    settled = [p for p in picks if p["status"] not in ("", "open")]
    if not settled:
        return None
    units = sum(float(p["units"]) for p in settled)
    wins = sum(1 for p in settled if p["status"] in ("won", "half-won"))
    losses = sum(1 for p in settled if p["status"] in ("lost", "half-lost"))
    pushes = len(settled) - wins - losses
    rec = f"{wins}W-{losses}L" + (f"-{pushes}P" if pushes else "")
    clvs = [float(p["clv_pp"]) for p in settled if p["clv_pp"]]
    out = (f"Picks: {len(settled)} settled ({rec}), "
           f"{units:+.2f}u at flat 1u stakes")
    if clvs:
        out += f"; avg CLV {statistics.mean(clvs):+.1f}pp over {len(clvs)} closing line(s)"
    return out


def load_ko_odds_engine(now=None):
    """Defensive adapter for the knockout ADVANCE market: returns (callable, None) or
    (None, reason). The callable maps a resolved KnockoutMatch -> the render_market
    odds_info dict (same shape as build_site.load_odds_engine produces), or None when
    there's no advance snapshot for that tie (placeholder state, per contract). Neutral
    venue — host HFA is not applied in the knockout (DECISIONS.md). Mirrors the group
    load_odds_engine so build_site can render KO odds through the existing render_market."""
    try:
        odds_rows = load_odds()
        if not odds_rows:
            return None, "odds_log.csv is empty — no snapshots yet"
        picks = load_picks()
        model = pr.load_ratings()
        _now = now or lg.now_et()
    except Exception as e:                       # broad on purpose: never break the build
        return None, f"knockout odds engine unavailable ({e.__class__.__name__}: {e})"

    def call(km) -> dict | None:
        try:
            if not (km.team_a and km.team_b):
                return None
            mid = f"M{km.match_no}"
            match_rows = [r for r in odds_rows
                          if r["match_id"] == mid and r["phase"] == "snapshot"]
            if not match_rows:
                return None
            ev = evaluate_ko_match(km.match_no, km.team_a, km.team_b, odds_rows, model, km.country)
            if not ev.get("advance"):
                return None                      # snapshot exists but no h2h to derive advance from
            match_picks, flags = best_bets(ev)
            stale = {m for m in {p["market"] for p in match_picks}
                     if (age := _snapshot_age_hours(odds_rows, mid, _now, m)) is None
                     or age > MAX_SNAPSHOT_AGE_HOURS}
            return {
                "evaluation": ev, "picks": match_picks,
                "pick": match_picks[0] if match_picks else None,   # back-compat
                "flags": flags, "stale_markets": stale,
                "max_age_h": MAX_SNAPSHOT_AGE_HOURS,
                "best_prices": _best_prices(odds_rows, mid),
                "recorded": [p for p in picks if p["match_id"] == mid],
                "threshold": EDGE_THRESHOLD, "record_threshold": RECORD_THRESHOLD,
                "model_sanity": MODEL_PRICED_SANITY, "sanity": SANITY_EDGE,
                "source_label": snapshot_source_label(odds_rows, mid),
                "snapshot_ts": max(r["timestamp"] for r in match_rows),
            }
        except Exception:
            return None

    return call, None


def load_shadow_picks(path: Path = SHADOW_PICKS_LOG) -> list:
    return load_picks(path)


def _wlu(ps: list) -> tuple:
    """(n, wins, losses, hypothetical units) for a settled-pick subset."""
    w = sum(1 for p in ps if p["status"] in ("won", "half-won"))
    l = sum(1 for p in ps if p["status"] in ("lost", "half-lost"))
    u = sum(float(p["units"]) for p in ps if p["units"])
    return len(ps), w, l, u


def shadow_summary(picks: list) -> str | None:
    """One line on the SHADOW track — the RISKY calls (severe model<->market
    disagreement): hit rate + HYPOTHETICAL flat-1u units, sliced 1X2 (ratings calls) vs
    model-priced. On paper, not staked — the figure says only whether these convictions
    land (would they have won), never that we bet them."""
    settled = [p for p in picks if p["status"] not in ("", "open")]
    if not settled:
        return None
    n, w, l, u = _wlu(settled)
    parts = [f"{n} settled, {w}W-{l}L, {u:+.2f}u paper (flat 1u, not staked)"]
    for label, ms in (("1X2", ("h2h",)), ("model-priced", ("totals", "spreads", "btts"))):
        sub = [p for p in settled if p["market"] in ms]
        if sub:
            sn, sw, sl, su = _wlu(sub)
            parts.append(f"{label} {sw}W-{sl}L {su:+.2f}u")
    return "; ".join(parts)


# ---------------------------------------------------------------- rendering

def render_odds_section(match_id: str, evaluation: dict, pick,
                        flags: list, best_prices: dict,
                        threshold: float = EDGE_THRESHOLD,
                        source_label: str = "") -> str:
    """Markdown body for a card's Odds & Best Bet slot. ``pick`` accepts a
    single pick dict (legacy) or the ranked list from best_bets. ``source_label``
    (from snapshot_source_label) names where the odds came from — provenance the
    note must state honestly rather than assuming a market median."""
    picks = pick if isinstance(pick, list) else ([pick] if pick else [])
    lines = []
    labelled = ([("1X2", r) for r in evaluation["h2h"]]
                + [(f"O/U {r[1]}", r) for r in evaluation["totals"]]
                + [(f"AH {float(r[1]):+g}", r) for r in evaluation.get("spreads", [])]
                + [("BTTS", r) for r in evaluation.get("btts", [])]
                + [("Advance", r) for r in evaluation.get("advance", [])])
    rows = [r for _, r in labelled]
    if rows:
        lines += ["| Market | Sel | Odds | Implied | Ours | Edge |",
                  "|:--|:--|--:|--:|--:|--:|"]
        actionable = 0
        for mk, (sel, line, odds, implied, our_p, edge) in labelled:
            clears = edge >= threshold
            actionable += int(clears)
            odds_str = american_odds(odds) if odds is not None else "—"   # derived line: no price
            lines.append(f"| {mk} | {sel} | {odds_str} | {implied:.0%} | "
                         f"{our_p:.0%} | {edge:+.1%}{' ✓' if clears else ''} |")
        if actionable:
            lines.append("")
            lines.append(f"_✓ clears the {threshold:.0%} display threshold; a pick "
                         f"must clear the {RECORD_THRESHOLD:.0%} recording bar._")
        if evaluation.get("spreads") or evaluation.get("btts") or evaluation["totals"]:
            lines.append("")
            lines.append("_Totals/AH/BTTS are model-priced from the score matrix "
                         "(the Opta overlay covers W/D/L only) — no independent "
                         f"consensus check, so they clear a stricter {MODEL_PRICED_SANITY:.0%} "
                         f"edge ceiling vs {SANITY_EDGE:.0%} for 1X2._")
        if source_label:
            lines.append("")
            lines.append(f"_Odds source: {source_label}, de-vigged multiplicatively._")
        for n in evaluation.get("missing", []):
            lines.append("")
            lines.append(f"_{n}._")
        lines.append("")
    if picks:
        for i, pk in enumerate(picks, 1):
            bp = best_prices.get((pk["market"], pk["selection"], str(pk["line"])))
            price = f" — best price {american_odds(bp[0])} ({bp[1]})" if bp else ""
            ln = f" {pk['line']}" if pk["line"] else ""
            label = "Best bet" if len(picks) == 1 else f"Pick {i}"
            lines.append(f"**{label}: {pk['selection']}{ln} ({pk['market']}) "
                         f"@ {american_odds(pk['odds'])}, edge {pk['edge']:+.1%}**{price}. "
                         "Flat 1u (paper).")
        if len(picks) > 1:
            lines.append("_Same-match picks are correlated — they tend to win "
                         "and lose together._")
    elif rows:
        lines.append(f"**No bet** — no edge clears the {RECORD_THRESHOLD:.0%} "
                     "recording bar (a normal, expected result).")
    for fl in flags:
        lines.append(f"> ⚠️ {fl}")
    for ms in evaluation["missing"]:
        lines.append(f"_{ms}._")
    return "\n".join(lines)


# ---------------------------------------------------------------- odds API fetch

def _read_key() -> str | None:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    return None


def _api_get(path: str, key: str, **params) -> tuple:
    params = {"apiKey": key, **params}
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "wc26-hub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            remaining = resp.headers.get("x-requests-remaining", "?")
            return json.loads(resp.read().decode("utf-8")), remaining
    except urllib.error.HTTPError as e:
        # the request URL carries the apiKey query param — scrub it so the key
        # can never leak through e.url (callers print e.code/body, not the URL)
        try:
            if getattr(e, "url", None):
                e.url = e.url.replace(key, "***")
        except Exception:
            pass
        raise
    except (urllib.error.URLError, TimeoutError) as e:
        # DNS / refused / timeout: a clean, key-free OddsError so a best-effort
        # snapshot degrades instead of crashing the step with a traceback
        raise OddsError(f"odds API unreachable: {getattr(e, 'reason', e)}") from None


def _match_event(event: dict, fixture_rows: list) -> dict | None:
    """Map an API event to a fixtures row by canon team names (either order),
    bounded to ±1 day of the fixture's ET date so a knockout-stage rematch of
    a group pairing can never be logged under the group match_id."""
    h = pr._canon(event.get("home_team", ""))
    a = pr._canon(event.get("away_team", ""))
    ev_date = None
    ct = (event.get("commence_time") or "").replace("Z", "+00:00")
    if ct:
        try:
            # compare in ET, the fixtures' calendar — a UTC date is off by one
            # for every evening kickoff and would widen the guard window
            ev_date = datetime.fromisoformat(ct).astimezone(lg.ET).date()
        except ValueError:
            pass
    for r in fixture_rows:
        if {r["team_a"], r["team_b"]} == {h, a}:
            if ev_date is not None:
                try:
                    f_date = date.fromisoformat((r.get("date_et") or "").strip())
                except ValueError:
                    f_date = None
                if f_date and abs((ev_date - f_date).days) > 1:
                    continue  # same pairing, different stage — not this fixture
            return r
    return None


def knockout_fixture_rows(fixtures_path: Path) -> list[dict]:
    """Resolved knockout ties as fixtures-shaped rows {match_id 'M{no}', team_a, team_b,
    date_et} so the SAME US-region fetch can snapshot a knockout game's 90' h2h (the API
    carries no 'to qualify' market — see evaluate_ko_match, which derives advance from this
    h2h). Teams are canon (from standings) so _match_event matches the API event by team-set;
    the ±1-day guard keeps a knockout rematch off the weeks-earlier group match_id. The KO
    match_id 'M73' never collides with a group id 'A1'. Missing/blank knockout.csv -> []."""
    import knockout as ko
    import bracket as bk
    ko_path = fixtures_path.parent / "knockout.csv"
    matches = ko.load_knockout(ko_path)
    if not matches:
        return []
    try:                                  # materialize so a lagging on-disk file still resolves
        fixtures = st.load_fixtures(fixtures_path)
        standings = st.compute_standings(fixtures, fair_play=st.load_discipline())
        matches = ko.materialize_teams(bk.project(standings), matches)
    except (FileNotFoundError, ValueError):
        pass                              # fall back to the teams already written on disk
    return [{"match_id": f"M{km.match_no}", "team_a": km.team_a, "team_b": km.team_b,
             "date_et": km.date_et}
            for km in matches if km.participants_known and not km.is_played]


def snapshot_from_api(events: list, fixture_rows: list, phase: str,
                      now: datetime, prefer_book: str | None = None) -> tuple:
    """Convert API events to odds_log rows: per-selection median + best price.
    Returns (rows, status_lines). Unmatched team names are REPORTED, not guessed.
    ``prefer_book`` (e.g. "draftkings") implements GET-THE-PREFERRED-BOOK-ELSE-FALL-BACK:
    for each selection, if the preferred book quoted it we log ONLY that book's price
    (so you bet the line you can take); otherwise we fall back to the median/best across
    all the books that did quote it. The fetch pulls the whole US region (same quota as
    a single-book request), so DraftKings drives h2h while totals/spreads — which DK
    doesn't supply via the API — fall back to the other US books."""
    rows, lines = [], []
    stamp = now.isoformat(timespec="seconds")
    for ev in events:
        ct = (ev.get("commence_time") or "").replace("Z", "+00:00")
        kickoff = None
        if ct:
            try:
                kickoff = datetime.fromisoformat(ct)
            except ValueError:
                kickoff = None
        # fail CLOSED: only log a snapshot for an event with a valid, FUTURE
        # kickoff — a missing/unparseable/past commence_time is skipped and
        # reported so in-play (or unknown-time) prices never pollute the log.
        if kickoff is None or kickoff <= now:
            why = ("already kicked off — in-play odds not logged" if kickoff
                   else "no valid commence_time — skipped (fail-closed)")
            lines.append(f"{ev.get('home_team')} vs {ev.get('away_team')}: {why}")
            continue
        fr = _match_event(ev, fixture_rows)
        if fr is None:
            lines.append(f"UNMATCHED event: {ev.get('home_team')!r} vs "
                         f"{ev.get('away_team')!r} — not in fixtures after canon "
                         "normalization; skipped (report, never fuzzy-match)")
            continue
        mid = fr["match_id"]
        home_is_a = pr._canon(ev["home_team"]) == fr["team_a"]

        books = ev.get("bookmakers", [])     # ALL US books; prefer_book chosen per selection
        if not books:
            lines.append(f"{mid}: no odds in this event — skipped")
            continue

        prices = {}   # (market, selection, line) -> [(odds, book), ...]
        for bk in books:
            for mkt in bk.get("markets", []):
                if mkt["key"] == "h2h":
                    for oc in mkt.get("outcomes", []):
                        nm = pr._canon(oc["name"]) if oc["name"] != "Draw" else "Draw"
                        if nm == "Draw":
                            sel = "draw"
                        elif nm == fr["team_a"]:
                            sel = "home"
                        elif nm == fr["team_b"]:
                            sel = "away"
                        else:
                            continue
                        prices.setdefault(("h2h", sel, ""), []).append(
                            (float(oc["price"]), bk["key"]))
                elif mkt["key"] == "totals":
                    for oc in mkt.get("outcomes", []):
                        sel = oc["name"].strip().lower()
                        if sel in ("over", "under"):
                            prices.setdefault(("totals", sel, _fmt_line(oc.get("point", "")))
                                              , []).append((float(oc["price"]), bk["key"]))
                elif mkt["key"] == "spreads":
                    for oc in mkt.get("outcomes", []):
                        nm = pr._canon(oc["name"])
                        if nm == fr["team_a"]:
                            sel = "home"
                        elif nm == fr["team_b"]:
                            sel = "away"
                        else:
                            continue
                        prices.setdefault(("spreads", sel, _fmt_line(oc.get("point", "")))
                                          , []).append((float(oc["price"]), bk["key"]))
        n_books = len(books)
        pref_hits = 0
        for (market, sel, line), plist in prices.items():
            # prefer the chosen book's own price when it quoted this selection;
            # otherwise fall back to the median/best across all books that did
            use = [(p, b) for p, b in plist if prefer_book and b == prefer_book] or plist
            if use is not plist:
                pref_hits += 1
            med = statistics.median(p for p, _ in use)
            best_odds, best_book = max(use, key=lambda x: x[0])
            rows.append({"match_id": mid, "market": market, "selection": sel,
                         "line": line, "odds": f"{med:.3f}",
                         "source": f"median/{len(use)}books", "phase": phase,
                         "timestamp": stamp})
            rows.append({"match_id": mid, "market": market, "selection": sel,
                         "line": line, "odds": f"{best_odds:.3f}",
                         "source": f"best:{best_book}", "phase": phase,
                         "timestamp": stamp})
        pref_note = (f", {prefer_book} on {pref_hits}/{len(prices)} selections"
                     if prefer_book else "")
        lines.append(f"{mid}: snapshot from {n_books} books "
                     f"({len(prices)} selections{pref_note})")
    return rows, lines


# ---------------------------------------------------------------- CLI

def _best_prices(odds_rows: list, match_id: str) -> dict:
    """{(market, selection, line): (odds, book)} from the latest best:* rows."""
    out = {}
    for market in ("h2h", "totals", "spreads", "btts"):
        rows = [r for r in odds_rows if r["match_id"] == match_id
                and r["market"] == market and r["phase"] == "snapshot"
                and r["source"].startswith("best:")]
        if not rows:
            continue
        last_ts = max(r["timestamp"] for r in rows)
        for r in rows:
            if r["timestamp"] == last_ts:
                out[(market, r["selection"], r["line"])] = (
                    float(r["odds"]), r["source"].split(":", 1)[1])
    return out


MAX_SNAPSHOT_AGE_HOURS = 12   # picks are only recorded against fresh prices


def _snapshot_age_hours(odds_rows: list, match_id: str, now: datetime,
                        market: str | None = None) -> float | None:
    """Hours since the newest snapshot row for this match (optionally for one
    market, so a fresh totals row can't vouch for a stale h2h price); None if
    no snapshot or the timestamps are unusable."""
    stamps = [r["timestamp"] for r in odds_rows
              if r["match_id"] == match_id and r["phase"] == "snapshot"
              and (market is None or r["market"] == market)]
    if not stamps:
        return None
    try:
        latest = datetime.fromisoformat(max(stamps))
        return (now - latest).total_seconds() / 3600
    except (ValueError, TypeError):   # malformed or tz-naive stamp: treat as stale
        return None


def cmd_evaluate(target: date, fixtures: Path, threshold: float,
                 record: bool = False, revise: bool = False,
                 record_any_date: bool = False,
                 max_snapshot_age: float = MAX_SNAPSHOT_AGE_HOURS) -> int:
    rows = be.read_rows(fixtures)
    slate = be.select_matches(rows, target)
    if not slate:
        print(f"no matches on editorial date {target}")
        return 0
    odds_rows = load_odds()
    ledger_rows = lg.load_ledger()
    # Recording is a publishing act: day-of and fresh prices only, unless
    # explicitly overridden. Evaluation/display is always allowed.
    if record and target != lg.now_et().date() and not record_any_date:
        print(f"--record refused: {target} is not today's editorial date "
              f"({lg.now_et().date()}) — picks are recorded day-of against fresh "
              "prices (pass --record-any-date to override deliberately)")
        record = False
    try:
        model = pr.load_ratings(fixtures=fixtures)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    now = lg.now_et()
    day_best = None
    for fr in slate:
        mid = fr["match_id"]
        host = pr.HOST_BY_COUNTRY.get((fr.get("country") or "").strip())
        hfa = host if host in (fr["team_a"], fr["team_b"]) else None
        pred = pr.predict_match(model, fr["team_a"], fr["team_b"], hfa_team=hfa)
        ev = evaluate_match(mid, odds_rows, ledger_rows, pred)
        picks, flags = best_bets(ev)
        bp = _best_prices(odds_rows, mid)
        src = snapshot_source_label(odds_rows, mid)
        print(f"\n### {mid} {fr['team_a']} vs {fr['team_b']}\n")
        print(render_odds_section(mid, ev, picks, flags, bp, threshold,
                                  source_label=src))
        for pick in picks if record else []:
            passed = now >= lg.kickoff_dt(fr)
            age = _snapshot_age_hours(odds_rows, mid, now, market=pick["market"])
            if age is None or age > max_snapshot_age:
                print(f"→ NOT recorded ({pick['market']}): snapshot is "
                      f"{'missing' if age is None else f'{age:.1f}h old'} "
                      f"(max {max_snapshot_age:g}h) — refresh odds first")
            else:
                price = bp.get((pick["market"], pick["selection"], str(pick["line"])),
                               (pick["odds"], "median"))
                try:
                    print("→ " + record_pick(mid, pick, price, now, passed,
                                             allow_revise=revise))
                except OddsError as e:
                    print(f"→ NOT recorded: {e}")
        # SHADOW track: the risky calls (severe model-vs-market gap), logged on paper
        # (too risky to bet) so we can later learn whether they land. Same freshness gate.
        for fp in flagged_bets(ev) if record else []:
            passed = now >= lg.kickoff_dt(fr)
            age = _snapshot_age_hours(odds_rows, mid, now, market=fp["market"])
            if age is None or age > max_snapshot_age:
                continue
            price = bp.get((fp["market"], fp["selection"], str(fp["line"])),
                           (fp["odds"], "median"))
            try:
                print("→ [shadow] " + record_pick(mid, fp, price, now, passed,
                                                  picks_path=SHADOW_PICKS_LOG,
                                                  allow_revise=revise, stake="0"))
            except OddsError:
                pass   # already logged / post-kickoff: the first conviction stands
        if picks and (day_best is None or picks[0]["edge"] > day_best[1]["edge"]):
            day_best = (mid, picks[0])
    if day_best:
        mid, pk = day_best
        print(f"\n**Day's best bet: {mid} {pk['selection']} "
              f"{pk['line'] or ''} ({pk['market']}) edge {pk['edge']:+.1%}**")
    else:
        print("\n**No bet today** — nothing clears the threshold.")
    return 0


def _load_resolved_knockout(matches: list) -> list:
    """Load + materialize the knockout schedule for settling/evaluation; [] (fail-soft)
    if there's no schedule or the bracket can't be projected yet."""
    try:
        import knockout as ko
        import bracket as bk
        kos = ko.load_knockout()
        if not kos:
            return []
        standings = st.compute_standings(matches, fair_play=st.load_discipline())
        return ko.materialize_teams(bk.project(standings), kos)
    except Exception:
        return []


def cmd_evaluate_ko(target: date, fixtures: Path, knockout_path: Path, threshold: float,
                    record: bool = False, revise: bool = False,
                    record_any_date: bool = False,
                    max_snapshot_age: float = MAX_SNAPSHOT_AGE_HOURS) -> int:
    """Edges + (optional) recording for the knockout ADVANCE market on a date. Only
    resolved, not-yet-played ties are considered; recording obeys the same day-of +
    fresh-snapshot gate as the group evaluate."""
    import knockout as ko
    import bracket as bk
    kos = ko.load_knockout(knockout_path)
    if not kos:
        print("no knockout schedule — nothing to evaluate")
        return 0
    matches = st.load_fixtures(fixtures)
    standings = st.compute_standings(matches, fair_play=st.load_discipline())
    try:
        kos = ko.materialize_teams(bk.project(standings), kos)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: cannot resolve knockout matchups: {e}", file=sys.stderr)
        return 1
    slate = [km for km in kos if km.date_et == target.isoformat()
             and km.participants_known and not km.is_played]
    if not slate:
        print(f"no resolved, unplayed knockout ties on {target}")
        return 0
    odds_rows = load_odds()
    if record and target != lg.now_et().date() and not record_any_date:
        print(f"--record refused: {target} is not today's date "
              f"({lg.now_et().date()}) — pass --record-any-date to override")
        record = False
    try:
        model = pr.load_ratings(fixtures=fixtures)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    now = lg.now_et()
    for km in slate:
        mid = f"M{km.match_no}"
        ev = evaluate_ko_match(km.match_no, km.team_a, km.team_b, odds_rows, model, km.country)
        picks, flags = best_bets(ev)
        bp = _best_prices(odds_rows, mid)
        print(f"\n### {mid} {km.team_a} vs {km.team_b} ({km.round})\n")
        print(render_odds_section(mid, ev, picks, flags, bp, threshold,
                                  source_label=snapshot_source_label(odds_rows, mid)))
        for pick in picks if record else []:
            passed = now >= lg.kickoff_dt(_ko_fixture_row(km))
            age = _snapshot_age_hours(odds_rows, mid, now, market=pick["market"])
            if age is None or age > max_snapshot_age:
                print(f"→ NOT recorded ({pick['market']}): snapshot "
                      f"{'missing' if age is None else f'{age:.1f}h old'}")
            else:
                price = bp.get((pick["market"], pick["selection"], str(pick["line"])),
                               (pick["odds"], "median"))
                try:
                    print("→ " + record_pick(mid, pick, price, now, passed,
                                             allow_revise=revise))
                except OddsError as e:
                    print(f"→ NOT recorded: {e}")
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Odds snapshots, edges, best bets, CLV.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="snapshot odds from the odds API")
    p_fetch.add_argument("--phase", choices=["snapshot", "closing", "both"],
                         default="snapshot",
                         help="'both' writes the fetched odds under snapshot AND closing "
                              "tags from ONE API call — so an intra-day run feeds both "
                              "recording (snapshot) and CLV (closing) without a 2nd call")
    p_fetch.add_argument("--sport", default=SPORT_KEY)
    p_fetch.add_argument("--bookmaker", default=DEFAULT_BOOKMAKER,
                         help="PREFERRED book (default "
                              f"{DEFAULT_BOOKMAKER!r}): used per-selection when it quotes "
                              "the line, else fall back to the best of the US region. "
                              "Pass 'all' for no preference (best of all books).")

    p_enter = sub.add_parser("enter", help="manually enter odds")
    p_enter.add_argument("match_id")
    p_enter.add_argument("market", choices=["h2h", "totals", "spreads", "btts", "advance"])
    p_enter.add_argument("values", nargs="+",
                         help="h2h: H,D,A — totals: LINE OVER,UNDER — "
                              "spreads: HOME_LINE HOME,AWAY — btts: YES,NO — "
                              "advance: HOME,AWAY (knockout to-qualify, 2-way)")
    p_enter.add_argument("--source", default="manual")
    p_enter.add_argument("--phase", choices=["snapshot", "closing"], default="snapshot")

    p_eval = sub.add_parser("evaluate", help="edges + best bets for a date")
    p_eval.add_argument("date")
    p_eval.add_argument("--threshold", type=float, default=EDGE_THRESHOLD)
    p_eval.add_argument("--record", action="store_true",
                        help="record qualifying picks to picks_log.csv "
                             "(day-of + fresh snapshot only)")
    p_eval.add_argument("--revise", action="store_true",
                        help="allow an existing open pick to be superseded")
    p_eval.add_argument("--record-any-date", action="store_true",
                        help="override the day-of recording gate (deliberate "
                             "early positions only)")
    p_eval.add_argument("--max-snapshot-age", type=float,
                        default=MAX_SNAPSHOT_AGE_HOURS,
                        help="oldest snapshot (hours) picks may be recorded against")

    p_eval_ko = sub.add_parser("evaluate-ko",
                               help="knockout advance edges + record for a date")
    p_eval_ko.add_argument("date")
    p_eval_ko.add_argument("--threshold", type=float, default=EDGE_THRESHOLD)
    p_eval_ko.add_argument("--record", action="store_true",
                           help="record qualifying advance picks (day-of + fresh only)")
    p_eval_ko.add_argument("--revise", action="store_true")
    p_eval_ko.add_argument("--record-any-date", action="store_true")
    p_eval_ko.add_argument("--max-snapshot-age", type=float,
                           default=MAX_SNAPSHOT_AGE_HOURS)
    p_eval_ko.add_argument("--knockout", type=Path,
                           default=REPO_ROOT / "data" / "knockout.csv")

    sub.add_parser("settle", help="grade open picks + CLV")
    sub.add_parser("report", help="units/CLV summary")

    for p in (p_fetch, p_enter, p_eval, p_eval_ko):
        p.add_argument("--fixtures", type=Path,
                       default=REPO_ROOT / "data" / "fixtures.csv")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if args.command == "fetch":
        key = _read_key()
        if not key:
            print("error: no API key. Set ODDS_API_KEY or write data/.odds_api_key "
                  "(git-ignored). Sign up free at the-odds-api.com.", file=sys.stderr)
            return 1
        # Pull the WHOLE US region (same quota as a single book), then prefer the
        # chosen book per selection and fall back to the rest — so DraftKings drives
        # h2h while totals/spreads (which DK doesn't supply via the API) use other books.
        prefer = (None if (args.bookmaker or "").strip().lower() in ("", "all")
                  else args.bookmaker.strip().lower())
        try:
            events, remaining = _api_get(f"/sports/{args.sport}/odds", key,
                                         regions="us", markets="h2h,spreads,totals",
                                         oddsFormat="decimal", dateFormat="iso")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"error: sport key {args.sport!r} not found — run with "
                      "--sport after checking /v4/sports for the current key",
                      file=sys.stderr)
            else:
                print(f"error: odds API HTTP {e.code}: {e.read().decode()[:200]}",
                      file=sys.stderr)
            return 1
        except OddsError as e:                 # URLError/timeout from _api_get
            print(f"error: {e}", file=sys.stderr)
            return 1
        rows = be.read_rows(args.fixtures)
        rows += knockout_fixture_rows(args.fixtures)   # snapshot resolved KO ties' 90' h2h too
        now = lg.now_et()
        phases = ["snapshot", "closing"] if args.phase == "both" else [args.phase]
        odds_rows, lines = [], []
        for ph in phases:                       # one API call, tagged under each phase
            rws, lns = snapshot_from_api(events, rows, ph, now, prefer_book=prefer)
            odds_rows += rws
            lines = lines or lns                # status lines are identical per phase
        append_odds(odds_rows)
        for line in lines:
            print(line)
        src_desc = (f"prefer {_book_display(prefer)}, else best US book"
                    if prefer else "US region (best of all books)")
        print(f"logged {len(odds_rows)} odds rows ({args.phase}, {src_desc}); "
              f"API requests remaining this month: {remaining}")
        return 0

    if args.command == "enter":
        now = lg.now_et().isoformat(timespec="seconds")
        try:
            if args.market == "h2h":
                odds3 = [float(x) for x in args.values[0].split(",")]
                if len(odds3) != 3:
                    raise OddsError("h2h needs three odds: home,draw,away")
                devig(odds3)   # validates > 1.0
                new = [{"match_id": args.match_id, "market": "h2h", "selection": s,
                        "line": "", "odds": f"{o:.3f}", "source": args.source,
                        "phase": args.phase, "timestamp": now}
                       for s, o in zip(H2H_SELECTIONS, odds3)]
            elif args.market == "totals":
                if len(args.values) != 2:
                    raise OddsError("totals needs: LINE OVER,UNDER")
                line = float(args.values[0])
                ou = [float(x) for x in args.values[1].split(",")]
                if len(ou) != 2:
                    raise OddsError("totals needs two odds: over,under")
                devig(ou)
                new = [{"match_id": args.match_id, "market": "totals", "selection": s,
                        "line": _fmt_line(line), "odds": f"{o:.3f}", "source": args.source,
                        "phase": args.phase, "timestamp": now}
                       for s, o in zip(("over", "under"), ou)]
            elif args.market == "spreads":
                if len(args.values) != 2:
                    raise OddsError("spreads needs: HOME_LINE HOME,AWAY")
                h_line = float(args.values[0])
                ha = [float(x) for x in args.values[1].split(",")]
                if len(ha) != 2:
                    raise OddsError("spreads needs two odds: home,away")
                devig(ha)
                new = [{"match_id": args.match_id, "market": "spreads", "selection": s,
                        "line": _fmt_line(l), "odds": f"{o:.3f}", "source": args.source,
                        "phase": args.phase, "timestamp": now}
                       for s, l, o in (("home", h_line, ha[0]), ("away", -h_line, ha[1]))]
            elif args.market == "advance":
                ha = [float(x) for x in args.values[0].split(",")]
                if len(ha) != 2:
                    raise OddsError("advance needs two odds: home,away (team_a,team_b to qualify)")
                devig(ha)
                new = [{"match_id": args.match_id, "market": "advance", "selection": s,
                        "line": "", "odds": f"{o:.3f}", "source": args.source,
                        "phase": args.phase, "timestamp": now}
                       for s, o in zip(("home", "away"), ha)]
            else:  # btts
                yn = [float(x) for x in args.values[0].split(",")]
                if len(yn) != 2:
                    raise OddsError("btts needs two odds: yes,no")
                devig(yn)
                new = [{"match_id": args.match_id, "market": "btts", "selection": s,
                        "line": "", "odds": f"{o:.3f}", "source": args.source,
                        "phase": args.phase, "timestamp": now}
                       for s, o in zip(("yes", "no"), yn)]
        except (OddsError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        # manual entries serve as both fair-value and best-price reference
        for r in list(new):
            new.append({**r, "source": f"best:{args.source}"})
        for r in new[:len(new) // 2]:
            r["source"] = f"median/{args.source}"
        append_odds(new)
        print(f"logged {len(new)} odds rows for {args.match_id} ({args.phase})")
        return 0

    if args.command == "evaluate":
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"error: bad date {args.date!r}", file=sys.stderr)
            return 2
        return cmd_evaluate(target, args.fixtures, args.threshold, args.record,
                            revise=args.revise,
                            record_any_date=args.record_any_date,
                            max_snapshot_age=args.max_snapshot_age)

    if args.command == "evaluate-ko":
        try:
            target = date.fromisoformat(args.date)
        except ValueError:
            print(f"error: bad date {args.date!r}", file=sys.stderr)
            return 2
        return cmd_evaluate_ko(target, args.fixtures, args.knockout, args.threshold,
                               record=args.record, revise=args.revise,
                               record_any_date=args.record_any_date,
                               max_snapshot_age=args.max_snapshot_age)

    matches = st.load_fixtures(REPO_ROOT / "data" / "fixtures.csv")
    if args.command == "settle":
        fixtures_rows = be.read_rows(REPO_ROOT / "data" / "fixtures.csv")
        knockout = _load_resolved_knockout(matches)
        for line in settle_picks(matches, load_odds(), fixtures_rows=fixtures_rows,
                                 knockout=knockout):
            print(line)
        # the shadow ledger settles the same way but stays OUT of the units record
        for line in settle_picks(matches, load_odds(), picks_path=SHADOW_PICKS_LOG,
                                 fixtures_rows=fixtures_rows, knockout=knockout):
            print(f"[shadow] {line}")
        print(units_summary(load_picks()) or "no settled picks yet")
        sh = shadow_summary(load_shadow_picks())
        if sh:
            print("SHADOW (risky calls — model vs market, paper-only) — " + sh)
        return 0

    print(units_summary(load_picks()) or "no settled picks yet")
    sh = shadow_summary(load_shadow_picks())
    if sh:
        print("SHADOW (tracked, never bet) — " + sh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
