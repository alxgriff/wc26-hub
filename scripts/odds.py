#!/usr/bin/env python3
"""WC26 odds & best-bet engine (PLAN.md Phase 5 / CLAUDE.md "Phase 3").

Decisions locked with the user (June 12): odds come from a free odds API
(key supplied by the user) with a manual-entry CLI fallback; the edge is
computed against the PUBLISHED consensus probability (the ledger's accountable
call); defaults confirmed: 3pp edge threshold, flat 1u stakes, 1X2 + totals
markets, >15pp sanity flag; paper units only for now.

Methodology (per CLAUDE.md, with reviewed extensions):
  * Snapshot the market at publish time to ``data/odds_log.csv``. We log the
    per-selection MEDIAN across books (fair-value reference, robust to one
    stale book) and the BEST available price (what a bet would settle at).
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
    python scripts/odds.py fetch [--phase snapshot|closing]   # odds API (key req.)
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
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st      # noqa: E402
import build_edition as be  # noqa: E402  (editorial slate)
import ledger as lg         # noqa: E402  (kickoff guard, consensus probabilities)
import predict as pr        # noqa: E402  (totals model, name canon)

REPO_ROOT = Path(__file__).resolve().parents[1]
ODDS_LOG = REPO_ROOT / "data" / "odds_log.csv"
PICKS_LOG = REPO_ROOT / "data" / "picks_log.csv"
KEY_FILE = REPO_ROOT / "data" / ".odds_api_key"
API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "soccer_fifa_world_cup"

ODDS_COLUMNS = ["match_id", "market", "selection", "line", "odds",
                "source", "phase", "timestamp"]
PICK_COLUMNS = ["match_id", "market", "selection", "line", "odds", "book",
                "edge_pp", "our_p", "implied_p", "stake", "timestamp",
                "status", "units", "clv_pp"]

EDGE_THRESHOLD = 0.03      # user-tunable via --threshold
SANITY_EDGE = 0.15         # above this: flag, never auto-pick
H2H_SELECTIONS = ("home", "draw", "away")


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


def prob_over(lambda_a: float, lambda_b: float, line: float, n: int = 12) -> float:
    """P(total goals > line) from independent Poisson margins (the same
    assumptions as predict.py's score matrix)."""
    if abs(line - round(line)) < 1e-9:
        raise OddsError(f"integer totals line {line} can push — only .5 lines supported")
    pa = [math.exp(-lambda_a) * lambda_a ** i / math.factorial(i) for i in range(n + 1)]
    pb = [math.exp(-lambda_b) * lambda_b ** j / math.factorial(j) for j in range(n + 1)]
    z = sum(pa) * sum(pb)
    under = sum(pa[i] * pb[j] for i in range(n + 1) for j in range(n + 1)
                if i + j < line)
    return 1.0 - under / z


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


def load_picks(path: Path = PICKS_LOG) -> list:
    return _load(path, PICK_COLUMNS)


def append_odds(rows: list, path: Path = ODDS_LOG) -> None:
    existing = load_odds(path)
    _save(existing + rows, path, ODDS_COLUMNS)


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
        out[(r["selection"], r["line"])] = (float(r["odds"]),
                                            int(m.group(1)) if m else 1)
    return out


def paired_lines(market_data: dict, sel_a: str, sel_b: str,
                 negate_b: bool = False) -> tuple | None:
    """The best-supported line quoted on BOTH sides of a two-way market.
    Returns (line_a, odds_a, odds_b) — for spreads the away line is the
    negation of the home line (``negate_b``). None if no complete pair."""
    candidates = []
    for (sel, line), (odds_a, n_a) in market_data.items():
        if sel != sel_a:
            continue
        b_line = line
        if negate_b and line:
            b_line = f"{-float(line):g}"
        match_b = market_data.get((sel_b, b_line))
        if match_b:
            candidates.append((n_a + match_b[1], line, odds_a, match_b[0]))
    if not candidates:
        return None
    _, line, odds_a, odds_b = max(candidates, key=lambda c: c[0])
    return line, odds_a, odds_b


# ---------------------------------------------------------------- evaluation

def consensus_probs(match_id: str, ledger_rows: list) -> tuple | None:
    """The PUBLISHED consensus W/D/L from the prediction ledger (the
    accountable number we bet against). None if not logged."""
    row = next((r for r in ledger_rows
                if r["match_id"] == match_id and r["source"] == lg.PUBLISHED_SOURCE), None)
    if row is None:
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
            out["missing"].append("no logged consensus prediction — 1X2 edge not computed")
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
    pair = paired_lines(totals, "over", "under") if totals else None
    if pair and pred is not None:
        line, o_over, o_under = pair
        try:
            p_over = prob_over(pred.lambda_a, pred.lambda_b, float(line))
        except OddsError as e:
            out["missing"].append(str(e))
        else:
            implied = devig([o_over, o_under])
            for i, (sel, p) in enumerate((("over", p_over), ("under", 1 - p_over))):
                out["totals"].append((sel, line, [o_over, o_under][i],
                                      implied[i], p, p - implied[i]))
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
            a_line = f"{-float(h_line):g}"
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


def best_bet(evaluation: dict, threshold: float = EDGE_THRESHOLD,
             sanity: float = SANITY_EDGE) -> tuple:
    """(pick | None, flags). Pick = dict for the largest positive edge ≥
    threshold and ≤ sanity; edges above sanity become flags instead."""
    candidates, flags = [], []
    for market in ("h2h", "totals", "spreads", "btts"):
        for sel, line, odds, implied, our_p, edge in evaluation.get(market, []):
            if edge > sanity:
                flags.append(f"{market} {sel}{f' {line}' if line else ''}: edge "
                             f"{edge:+.1%} implausibly large — verify odds freshness "
                             "and team news before trusting")
            elif edge >= threshold:
                candidates.append({"market": market, "selection": sel, "line": line,
                                   "odds": odds, "implied_p": implied,
                                   "our_p": our_p, "edge": edge})
    if not candidates:
        return None, flags
    return max(candidates, key=lambda c: c["edge"]), flags


# ---------------------------------------------------------------- picks ledger

def record_pick(match_id: str, pick: dict, best_price: tuple, now: datetime,
                kickoff_passed: bool, picks_path: Path = PICKS_LOG) -> str:
    """Append/refresh the pick for (match_id, market). Best_price = (odds, book)
    actually loggable; refuses post-kickoff and never touches settled rows."""
    picks = load_picks(picks_path)
    existing = next((p for p in picks if p["match_id"] == match_id
                     and p["market"] == pick["market"]), None)
    if existing and existing["status"] != "open":
        raise OddsError(f"{match_id} {pick['market']}: pick already settled — immutable")
    if kickoff_passed:
        raise OddsError(f"{match_id}: kickoff has passed — no new or revised picks")
    odds, book = best_price
    row = {"match_id": match_id, "market": pick["market"], "selection": pick["selection"],
           "line": pick["line"], "odds": f"{odds:.2f}", "book": book,
           "edge_pp": f"{pick['edge'] * 100:.1f}", "our_p": f"{pick['our_p']:.4f}",
           "implied_p": f"{pick['implied_p']:.4f}", "stake": "1",
           "timestamp": now.isoformat(timespec="seconds"),
           "status": "open", "units": "", "clv_pp": ""}
    if existing:
        existing.update(row)
    else:
        picks.append(row)
    _save(picks, picks_path, PICK_COLUMNS)
    return (f"{match_id}: {pick['market']} {pick['selection']}"
            f"{f' {pick['line']}' if pick['line'] else ''} @ {odds:.2f} ({book}), "
            f"edge {pick['edge']:+.1%}")


def _market_keys(market: str, selection: str, line: str) -> list:
    """Ordered (selection, line) keys spanning the full market a pick belongs
    to, with spreads lines negated for the opposite side."""
    if market == "h2h":
        return [(s, "") for s in H2H_SELECTIONS]
    if market == "btts":
        return [("yes", ""), ("no", "")]
    if market == "totals":
        return [("over", line), ("under", line)]
    other = f"{-float(line):g}" if line else ""
    return ([("home", line), ("away", other)] if selection == "home"
            else [("home", other), ("away", line)])


def settle_picks(matches: list, odds_rows: list,
                 picks_path: Path = PICKS_LOG) -> list:
    """Grade open picks whose match is played: units (odds−1 won, −1 lost) and
    CLV vs the closing snapshot when one exists."""
    picks = load_picks(picks_path)
    by_mid = {m.match_id: m for m in matches if m.is_played}
    lines = []
    for p in picks:
        if p["status"] != "open" or p["match_id"] not in by_mid:
            continue
        m = by_mid[p["match_id"]]
        if p["market"] == "h2h":
            outcome = ("home", "draw", "away")[lg.outcome_index(m.score_a, m.score_b)]
            won = p["selection"] == outcome
            p["status"] = "won" if won else "lost"
            p["units"] = f"{float(p['odds']) - 1:+.2f}" if won else "-1.00"
        elif p["market"] == "totals":
            total = m.score_a + m.score_b
            won = (total > float(p["line"])) == (p["selection"] == "over")
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

        closing = latest_market(odds_rows, p["match_id"], p["market"], phase="closing")
        keys = _market_keys(p["market"], p["selection"], p["line"])
        if closing and all(k in closing for k in keys):
            implied = devig([closing[k][0] for k in keys])
            idx = [k[0] for k in keys].index(p["selection"])
            clv = implied[idx] - float(p["implied_p"])
            p["clv_pp"] = f"{clv * 100:+.1f}"
        lines.append(f"{p['match_id']} {p['market']} {p['selection']}: "
                     f"{p['status']} {p['units']}u"
                     + (f", CLV {p['clv_pp']}pp" if p["clv_pp"] else ", CLV n/a"))
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


# ---------------------------------------------------------------- rendering

def render_odds_section(match_id: str, evaluation: dict, pick: dict | None,
                        flags: list, best_prices: dict,
                        threshold: float = EDGE_THRESHOLD) -> str:
    """Markdown body for a card's Odds & Best Bet slot."""
    lines = []
    labelled = ([("1X2", r) for r in evaluation["h2h"]]
                + [(f"O/U {r[1]}", r) for r in evaluation["totals"]]
                + [(f"AH {float(r[1]):+g}", r) for r in evaluation.get("spreads", [])]
                + [("BTTS", r) for r in evaluation.get("btts", [])])
    rows = [r for _, r in labelled]
    if rows:
        lines += ["| Market | Sel | Odds (median) | Implied | Ours | Edge |",
                  "|:--|:--|--:|--:|--:|--:|"]
        for mk, (sel, line, odds, implied, our_p, edge) in labelled:
            lines.append(f"| {mk} | {sel} | {odds:.2f} | {implied:.0%} | "
                         f"{our_p:.0%} | {edge:+.1%} |")
        if evaluation.get("spreads") or evaluation.get("btts") or evaluation["totals"]:
            lines.append("")
            lines.append("_Totals/AH/BTTS are model-priced from the score matrix "
                         "(the Opta overlay covers W/D/L only)._")
        lines.append("")
    if pick:
        bp = best_prices.get((pick["market"], pick["selection"], str(pick["line"])))
        price = f" — best price {bp[0]:.2f} ({bp[1]})" if bp else ""
        ln = f" {pick['line']}" if pick["line"] else ""
        lines.append(f"**Best bet: {pick['selection']}{ln} ({pick['market']}) "
                     f"@ {pick['odds']:.2f}, edge {pick['edge']:+.1%}**{price}. "
                     "Flat 1u (paper).")
    elif rows:
        lines.append(f"**No bet** — no edge clears the {threshold:.0%} threshold "
                     "(a normal, expected result).")
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        remaining = resp.headers.get("x-requests-remaining", "?")
        return json.loads(resp.read().decode("utf-8")), remaining


def _match_event(event: dict, fixture_rows: list) -> dict | None:
    """Map an API event to a fixtures row by canon team names (either order)."""
    h = pr._canon(event.get("home_team", ""))
    a = pr._canon(event.get("away_team", ""))
    for r in fixture_rows:
        if {r["team_a"], r["team_b"]} == {h, a}:
            return r
    return None


def snapshot_from_api(events: list, fixture_rows: list, phase: str,
                      now: datetime) -> tuple:
    """Convert API events to odds_log rows: per-selection median + best price.
    Returns (rows, status_lines). Unmatched team names are REPORTED, not guessed."""
    rows, lines = [], []
    stamp = now.isoformat(timespec="seconds")
    for ev in events:
        ct = (ev.get("commence_time") or "").replace("Z", "+00:00")
        if ct:
            try:
                if datetime.fromisoformat(ct) <= now:
                    lines.append(f"{ev.get('home_team')} vs {ev.get('away_team')}: "
                                 "already kicked off — in-play odds not logged")
                    continue
            except ValueError:
                pass
        fr = _match_event(ev, fixture_rows)
        if fr is None:
            lines.append(f"UNMATCHED event: {ev.get('home_team')!r} vs "
                         f"{ev.get('away_team')!r} — not in fixtures after canon "
                         "normalization; skipped (report, never fuzzy-match)")
            continue
        mid = fr["match_id"]
        home_is_a = pr._canon(ev["home_team"]) == fr["team_a"]

        prices = {}   # (market, selection, line) -> [(odds, book), ...]
        for bk in ev.get("bookmakers", []):
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
                            prices.setdefault(("totals", sel, str(oc.get("point", "")))
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
                        prices.setdefault(("spreads", sel, str(oc.get("point", "")))
                                          , []).append((float(oc["price"]), bk["key"]))
        n_books = len(ev.get("bookmakers", []))
        for (market, sel, line), plist in prices.items():
            med = statistics.median(p for p, _ in plist)
            best_odds, best_book = max(plist, key=lambda x: x[0])
            rows.append({"match_id": mid, "market": market, "selection": sel,
                         "line": line, "odds": f"{med:.3f}",
                         "source": f"median/{len(plist)}books", "phase": phase,
                         "timestamp": stamp})
            rows.append({"match_id": mid, "market": market, "selection": sel,
                         "line": line, "odds": f"{best_odds:.3f}",
                         "source": f"best:{best_book}", "phase": phase,
                         "timestamp": stamp})
        lines.append(f"{mid}: snapshot from {n_books} books "
                     f"({len(prices)} selections)")
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


def cmd_evaluate(target: date, fixtures: Path, threshold: float,
                 record: bool = False) -> int:
    rows = be.read_rows(fixtures)
    slate = be.select_matches(rows, target)
    if not slate:
        print(f"no matches on editorial date {target}")
        return 0
    odds_rows = load_odds()
    ledger_rows = lg.load_ledger()
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
        pick, flags = best_bet(ev, threshold)
        bp = _best_prices(odds_rows, mid)
        print(f"\n### {mid} {fr['team_a']} vs {fr['team_b']}\n")
        print(render_odds_section(mid, ev, pick, flags, bp, threshold))
        if pick and record:
            passed = now >= lg.kickoff_dt(fr)
            price = bp.get((pick["market"], pick["selection"], str(pick["line"])),
                           (pick["odds"], "median"))
            try:
                print("→ " + record_pick(mid, pick, price, now, passed))
            except OddsError as e:
                print(f"→ NOT recorded: {e}")
        if pick and (day_best is None or pick["edge"] > day_best[1]["edge"]):
            day_best = (mid, pick)
    if day_best:
        mid, pk = day_best
        print(f"\n**Day's best bet: {mid} {pk['selection']} "
              f"{pk['line'] or ''} ({pk['market']}) edge {pk['edge']:+.1%}**")
    else:
        print("\n**No bet today** — nothing clears the threshold.")
    return 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Odds snapshots, edges, best bets, CLV.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="snapshot odds from the odds API")
    p_fetch.add_argument("--phase", choices=["snapshot", "closing"], default="snapshot")
    p_fetch.add_argument("--sport", default=SPORT_KEY)

    p_enter = sub.add_parser("enter", help="manually enter odds")
    p_enter.add_argument("match_id")
    p_enter.add_argument("market", choices=["h2h", "totals", "spreads", "btts"])
    p_enter.add_argument("values", nargs="+",
                         help="h2h: H,D,A — totals: LINE OVER,UNDER — "
                              "spreads: HOME_LINE HOME,AWAY — btts: YES,NO")
    p_enter.add_argument("--source", default="manual")
    p_enter.add_argument("--phase", choices=["snapshot", "closing"], default="snapshot")

    p_eval = sub.add_parser("evaluate", help="edges + best bets for a date")
    p_eval.add_argument("date")
    p_eval.add_argument("--threshold", type=float, default=EDGE_THRESHOLD)
    p_eval.add_argument("--record", action="store_true",
                        help="record qualifying picks to picks_log.csv")

    sub.add_parser("settle", help="grade open picks + CLV")
    sub.add_parser("report", help="units/CLV summary")

    for p in (p_fetch, p_enter, p_eval):
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
        rows = be.read_rows(args.fixtures)
        odds_rows, lines = snapshot_from_api(events, rows, args.phase, lg.now_et())
        append_odds(odds_rows)
        for line in lines:
            print(line)
        print(f"logged {len(odds_rows)} odds rows ({args.phase}); "
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
                        "line": str(line), "odds": f"{o:.3f}", "source": args.source,
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
                        "line": str(l), "odds": f"{o:.3f}", "source": args.source,
                        "phase": args.phase, "timestamp": now}
                       for s, l, o in (("home", h_line, ha[0]), ("away", -h_line, ha[1]))]
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
        return cmd_evaluate(target, args.fixtures, args.threshold, args.record)

    matches = st.load_fixtures(REPO_ROOT / "data" / "fixtures.csv")
    if args.command == "settle":
        for line in settle_picks(matches, load_odds()):
            print(line)
        summary = units_summary(load_picks())
        print(summary or "no settled picks yet")
        return 0

    summary = units_summary(load_picks())
    print(summary or "no settled picks yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
