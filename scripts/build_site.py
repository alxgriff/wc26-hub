#!/usr/bin/env python3
"""WC26 static site builder — standings hub + team cards + matchup previews.

Outputs (all self-contained HTML, inline CSS from templates/site.css, zero JS):

    docs/index.html          the standings front page
    docs/teams/{slug}.html   48 team cards (kb profile + live standing + fixtures)
    docs/matches/{id}.html   72 matchup previews (card prose + live Call/Stakes)
    docs/data.json           machine-readable standings (schema 1)

Presentation layer only. Ranking lives in standings.py; the 🌙 editorial-date
convention in build_edition.py; kb/card parsing in site_content.py; the
prediction model in predict.py. predict.py is consumed through a defensive
adapter: any failure to load it (Phase 4 still landing, missing ratings)
degrades The Call to its placeholder state with a warning — never a broken
build, and never an invented number.

Importable API (for tests):

    forms = form_by_team(matches)
    html_page, data = build_page(matches, rows, target_date, generated_at)
    warnings = build_site(out_dir, target_date, generated_at, predictor=...)

CLI:
    python scripts/build_site.py [--date 2026-06-12] [--fixtures ...]
                                 [--out-dir docs] [--template-dir templates]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from string import Template
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st            # noqa: E402
import build_edition as be        # noqa: E402
import site_content as sc         # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "templates"
KB_GUIDE = REPO_ROOT / "kb" / "2026_fifa_world_cup_guide.md"
DISCIPLINE = st.DISCIPLINE
BLURBS_DIR = REPO_ROOT / "data" / "blurbs"
FAIR_PLAY_POINTS = st.FAIR_PLAY_POINTS
REPO_URL = "https://github.com/alxgriff/wc26-hub"
DAGGERS = ["†", "‡", "§", "¶"]
GAMES_PER_TEAM = st.GAMES_PER_TEAM

_PLACEHOLDER_BRACKETS = re.compile(r"^\*?\[([^\]]*)\]\*?$", re.DOTALL)
_MACHINE_PREFIX = re.compile(
    r"^(?:Model pending|Phase \d|Edition day)\s*[—\-–]*\s*(?:lean|markets to watch)?:?\s*",
    re.IGNORECASE)


# ---------------------------------------------------------------- data shaping

def form_by_team(matches: "list[st.Match]") -> dict[str, list[str | None]]:
    """Per-team result letters indexed by matchday (0-based): 'W'/'D'/'L' for
    played matches, None for unplayed slots."""
    forms: dict[str, list[str | None]] = {}
    for m in matches:
        for t in (m.team_a, m.team_b):
            forms.setdefault(t, [None] * GAMES_PER_TEAM)
        if not m.is_played or m.score_a is None or m.score_b is None:
            continue
        if not 1 <= m.matchday <= GAMES_PER_TEAM:
            raise ValueError(f"{m.match_id}: matchday {m.matchday} outside 1..{GAMES_PER_TEAM}")
        i = m.matchday - 1
        if m.score_a > m.score_b:
            a, b = "W", "L"
        elif m.score_a < m.score_b:
            a, b = "L", "W"
        else:
            a = b = "D"
        for team, letter in ((m.team_a, a), (m.team_b, b)):
            if forms[team][i] is not None:
                raise ValueError(
                    f"{m.match_id}: {team} already has a matchday-{m.matchday} result")
            forms[team][i] = letter
    return forms


def load_discipline(path: Path = DISCIPLINE) -> dict[str, int]:
    """Delegates to standings.load_discipline — the single fair-play source
    shared with editions, scenarios, and the blurb."""
    return st.load_discipline(path)


def _note_daggers(rows: "list[st.TeamRow]", notes: list[str]
                  ) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Assign footnote symbols to notes and to the teams each note names.
    Returns ({team: symbols}, [(symbol, note_text), ...]). Past four notes the
    symbols double up (††, ‡‡, ...) per print convention. Team matching is
    word-bounded so a team name contained in another's never misfires."""
    team_syms: dict[str, str] = {}
    sym_notes: list[tuple[str, str]] = []
    for i, note in enumerate(notes):
        sym = DAGGERS[i % len(DAGGERS)] * (i // len(DAGGERS) + 1)
        sym_notes.append((sym, note))
        for r in rows:
            if re.search(rf"(?<!\w){re.escape(r.team)}(?!\w)", note):
                team_syms[r.team] = team_syms.get(r.team, "") + sym
    return team_syms, sym_notes


def _display_note(note: str, *prefixes: str) -> str:
    out = note.replace("⚠️", "").strip()
    for p in prefixes:
        if out.startswith(p):
            out = out[len(p):].lstrip()
    return out


# ---------------------------------------------------------------- html pieces

_FORM_WORDS = {"W": "won", "D": "drew", "L": "lost", None: "not yet played"}


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _pips_html(form: list[str | None]) -> str:
    pips = []
    for letter in form:
        if letter is None:
            pips.append('<span class="pip" aria-hidden="true">·</span>')
        else:
            pips.append(f'<span class="pip pip-{letter.lower()}" aria-hidden="true">{letter}</span>')
    words = ", ".join(_FORM_WORDS[x] for x in form)
    return "".join(pips) + f'<span class="sr-only">{words}</span>'


def _notes_html(sym_notes: list[tuple[str, str]], *strip_prefixes: str) -> str:
    if not sym_notes:
        return ""
    items = []
    for sym, note in sym_notes:
        warn = "⚠️" in note or "lots" in note or "provisional" in note
        cls = ' class="warn"' if warn else ""
        items.append(f'    <li{cls}><span class="dag">{sym}</span> '
                     f'{_esc(_display_note(note, *strip_prefixes))}</li>')
    return '  <ul class="notes">\n' + "\n".join(items) + "\n  </ul>\n"


def _team_link(team: str, root: str) -> str:
    return f'{root}teams/{sc.slugify(team)}.html'


def _team_cell(r: "st.TeamRow", syms: dict[str, str], root: str) -> str:
    dag = syms.get(r.team, "")
    dag_html = f'<span class="dag">{dag}</span>' if dag else ""
    return (f'<th class="team" scope="row" title="{_esc(r.team)}">'
            f'<a href="{_team_link(r.team, root)}">{_esc(r.team)}</a>{dag_html}</th>')


def render_group_card(gt: "st.GroupTable", forms: dict[str, list[str | None]],
                      index: int, root: str = "") -> str:
    g = gt.group
    played = sum(r.played for r in gt.rows) // 2
    syms, sym_notes = _note_daggers(gt.rows, gt.notes)
    rows_html = []
    for pos, r in enumerate(gt.rows, 1):
        zone = "zone-top" if pos <= 2 else ("zone-third" if pos == 3 else "zone-out")
        form = forms.get(r.team, [None] * GAMES_PER_TEAM)
        rows_html.append(
            f'      <tr class="{zone}">\n'
            f'        <td class="pos">{pos}</td>\n'
            f'        {_team_cell(r, syms, root)}\n'
            f'        <td class="form">{_pips_html(form)}</td>\n'
            f'        <td>{r.gf}</td>\n'
            f'        <td>{st._fmt_gd(r.gd)}</td>\n'
            f'        <td class="pts">{r.points}</td>\n'
            f'      </tr>')
    gid = g.lower()
    return (
        f'<article class="card" id="group-{gid}" style="--i:{index}" aria-labelledby="gh-{gid}">\n'
        f'  <h3 id="gh-{gid}"><span class="stamp" aria-hidden="true">{g}</span>'
        f'Group {g}<span class="played-tag">{played}/{st.MATCHES_PER_GROUP} played</span></h3>\n'
        f'  <table>\n'
        f'    <caption class="sr-only">Group {g} standings after {played} of '
        f'{st.MATCHES_PER_GROUP} matches</caption>\n'
        f'    <thead><tr><th class="pos" scope="col"><span aria-hidden="true">#</span>'
        f'<span class="sr-only">Position</span></th>'
        f'<th class="team" scope="col">Team</th><th class="form" scope="col">Form</th>'
        f'<th class="num" scope="col">GF</th><th class="num" scope="col">GD</th>'
        f'<th class="num" scope="col">Pts</th></tr></thead>\n'
        f'    <tbody>\n' + "\n".join(rows_html) + "\n    </tbody>\n  </table>\n"
        + _notes_html(sym_notes, f"Group {g}:")
        + "</article>"
    )


def render_thirds(s: "st.Standings", forms: dict[str, list[str | None]],
                  root: str = "") -> str:
    rows = s.third_place
    if not rows:
        return '<p class="standfirst">No third-place table yet — no completed group rows.</p>'
    syms, sym_notes = _note_daggers(rows, s.third_place_notes)
    body = []
    for pos, r in enumerate(rows, 1):
        qualifying = pos <= st.QUALIFYING_THIRDS
        cls = "q" if qualifying else "below"
        in_cell = ('<td class="in">✓<span class="sr-only"> qualifying as it stands</span></td>'
                   if qualifying else '<td class="in"><span class="sr-only">out as it stands</span></td>')
        body.append(
            f'      <tr class="{cls}">\n'
            f'        <td class="pos">{pos}</td>\n'
            f'        {_team_cell(r, syms, root)}\n'
            f'        <td class="grp">{_esc(r.group)}</td>\n'
            f'        <td>{r.won}-{r.drawn}-{r.lost}</td>\n'
            f'        <td>{r.gf}</td>\n'
            f'        <td>{st._fmt_gd(r.gd)}</td>\n'
            f'        <td class="pts">{r.points}</td>\n'
            f'        {in_cell}\n'
            f'      </tr>')
        if qualifying and pos == st.QUALIFYING_THIRDS and len(rows) > st.QUALIFYING_THIRDS:
            body.append(
                '      <tr class="cut"><td colspan="8">'
                '<span class="scissors" aria-hidden="true">✂</span>&nbsp; '
                f'the cutline — top {st.QUALIFYING_THIRDS} advance</td></tr>')
    return (
        '<table>\n'
        '    <caption class="sr-only">Third-place ranking across all groups; '
        f'the best {st.QUALIFYING_THIRDS} advance</caption>\n'
        '    <thead><tr><th class="pos" scope="col"><span aria-hidden="true">#</span>'
        '<span class="sr-only">Rank</span></th>'
        '<th class="team" scope="col">Team</th><th scope="col">Grp</th>'
        '<th scope="col">W-D-L</th><th scope="col">GF</th><th scope="col">GD</th>'
        '<th scope="col">Pts</th><th scope="col"><span aria-hidden="true">In</span>'
        '<span class="sr-only">Qualifying</span></th></tr></thead>\n'
        '    <tbody>\n' + "\n".join(body) + "\n    </tbody>\n  </table>\n"
        + _notes_html(sym_notes, "Third-place ranking:")
    )


def render_slate(today: list[dict], root: str = "",
                 picks: dict[str, str] | None = None) -> str:
    if not today:
        return '    <li class="empty">No matches on this editorial date.</li>'
    picks = picks or {}
    chips = []
    for r in today:
        mid = r["match_id"]
        moon = '<span class="moon" aria-label="midnight kickoff, this slate"> ☾</span>' \
            if r.get("_late_cap") else ""
        tv = (r.get("tv_us") or "").strip() or "TV TBD"
        venue = ", ".join(p for p in [(r.get("stadium") or "").strip(),
                                      (r.get("city") or "").strip()] if p)
        href = f'{root}matches/{_esc(mid)}.html'
        played = (r.get("status") or "").strip().lower() == "played"
        if played:
            label = (f'{_esc(r["team_a"])} {_esc(str(r["score_a"]))}–'
                     f'{_esc(str(r["score_b"]))} {_esc(r["team_b"])}')
            time_bit = "FT"
        else:
            label = f'{_esc(r["team_a"])} v {_esc(r["team_b"])}'
            time_bit = _esc((r.get("kickoff_et") or "").strip()) + " ET"
        pick_html = ""
        if mid in picks:
            pick_html = f'<span class="pickline">▸ best bet: {_esc(picks[mid])}</span>'
        chips.append(
            f'    <li><span class="t">{time_bit}{moon}</span>'
            f'<span class="teams"><a href="{href}">{label}</a></span>'
            f'<span class="meta">{_esc(mid)} · {_esc(tv)} · {_esc(venue)} · preview →</span>'
            f'{pick_html}</li>')
    return "\n".join(chips)


def _group_nav(groups: "dict[str, st.GroupTable]") -> str:
    return "\n".join(
        f'    <a href="#group-{g.lower()}">{g}</a>' for g in sorted(groups))


def _archive(editions_dir: Path) -> str:
    files = sorted(editions_dir.glob("*.md")) if editions_dir.exists() else []
    if not files:
        return "      <li>No editions published yet.</li>"
    return "\n".join(
        f'      <li><a href="{REPO_URL}/blob/main/editions/{quote(f.name)}">'
        f'{_esc(f.stem)}</a></li>'
        for f in files)


def _site_css(template_dir: Path = TEMPLATE_DIR) -> str:
    css = (template_dir / "site.css").read_text(encoding="utf-8")
    if "$" in css:
        raise ValueError("templates/site.css must not contain '$' (template clash)")
    return css


# ---------------------------------------------------------------- the call

def load_predictor(fixtures: Path | None = None):
    """Defensive adapter around predict.py (Phase 4, owned by another stream).
    Returns (callable, None) or (None, reason). The callable maps a fixtures
    row -> dict for render_call(); any per-match failure returns None."""
    try:
        import predict as pr
        model = pr.load_ratings()
        overlay = pr.load_match_overlay()
    except Exception as e:                      # broad on purpose: never break the build
        return None, f"prediction model unavailable ({e.__class__.__name__}: {e})"

    def call(row: dict) -> dict | None:
        try:
            a, b = row["team_a"].strip(), row["team_b"].strip()
            host = pr.HOST_BY_COUNTRY.get((row.get("country") or "").strip())
            hfa = host if host in (a, b) else None
            p = pr.predict_match(model, a, b, hfa_team=hfa)
            o = overlay.get(row["match_id"])
            pa, pd_, pb = pr.blend_wdl(p, o) if o else (p.p_a, p.p_draw, p.p_b)
            return {
                "p_a": pa, "p_draw": pd_, "p_b": pb,
                "modal_score": p.modal_score, "total": p.total,
                "over25": p.over.get(2.5), "btts": p.btts,
                "hfa": hfa, "consensus": bool(o),
                "source": (o or {}).get("source", ""),
            }
        except Exception:
            return None

    return call, None


def render_call(info: dict | None, team_a: str, team_b: str,
                prebaked_lean: str | None,
                result: tuple[int, int] | None = None) -> str:
    """The Call block. Scheduled matches show the live model read; played
    matches show ONLY the pre-kickoff logged consensus (info["logged"]) graded
    against the result — never a retroactive recomputation. A played match
    with no logged call says so plainly instead of inventing a grade."""
    parts = []
    if info is None:
        if result is not None:
            parts.append(
                '<div class="placeholder-slot">No prediction was logged before '
                'kickoff — nothing to grade. Calls are never graded '
                'retroactively.</div>')
        else:
            parts.append(
                '<div class="placeholder-slot">Model pending — the ratings layer '
                '(Phase 2 in CLAUDE.md terms) has not produced a prediction for this '
                'match yet. This slot fills automatically once data/Ratings is '
                'complete; no numbers are invented in the meantime.</div>')
    else:
        pa, pd_, pb = info["p_a"], info["p_draw"], info["p_b"]
        wa, wd, wb = (max(round(x * 100), 1) for x in (pa, pd_, pb))
        logged = bool(info.get("logged"))
        facts = []
        if logged:
            if info.get("predicted_score"):
                facts.append(f'predicted score <b>{_esc(info["predicted_score"])}</b>')
            srcline = ("published consensus — logged "
                       f"{_esc(_fmt_snapshot_ts(info.get('logged_ts', '')))}, pre-kickoff")
            hfa = ""
        else:
            ms = info["modal_score"]
            facts = [f'most likely score <b>{ms[0]}–{ms[1]}</b>',
                     f'expected goals <b>{info["total"]:.2f}</b>']
            if info.get("over25") is not None:
                facts.append(f'over 2.5 <b>{round(info["over25"] * 100)}%</b>')
            if info.get("btts") is not None:
                facts.append(f'both score <b>{round(info["btts"] * 100)}%</b>')
            srcline = ("2-source consensus: rating model + " + _esc(info["source"])
                       if info.get("consensus") else "single source: rating model (Elo+Futi)")
            hfa = f' · home-field bonus: {_esc(info["hfa"])}' if info.get("hfa") else ""
        graded = ""
        if result is not None and logged:
            sa, sb = result
            outcome_i = 0 if sa > sb else (2 if sa < sb else 1)
            outcome_p = (pa, pd_, pb)[outcome_i]
            outcome_name = (f"{team_a} win" if sa > sb
                            else f"{team_b} win" if sa < sb else "draw")
            # canonical Brier: ledger.brier (sum form, 0 best / 2 worst /
            # 0.667 coin-flip) so the site and edition always publish the
            # same number; inline fallback uses the identical formula
            brier_fn = info.get("brier_fn") or (
                lambda p, i: sum((p[j] - (1 if j == i else 0)) ** 2 for j in range(3)))
            brier = brier_fn((pa, pd_, pb), outcome_i)
            graded = (f'<br><b>Graded:</b> final {sa}–{sb} ({_esc(outcome_name)}) · '
                      f'the logged call had it at <b>{round(outcome_p * 100)}%</b> · '
                      f'Brier <b>{brier:.3f}</b> (0 best · 0.667 coin-flip · 2 worst)')
        elif logged and info.get("awaiting"):
            graded = ('<br><b>Awaiting result</b> — kickoff has passed; this '
                      'logged call is frozen and will be graded when the score '
                      'is entered.')
        elif result is not None:
            graded = ('<br><b>Ungraded:</b> these are current-model numbers, not a '
                      'pre-kickoff logged call — no retroactive grading.')
        # in-bar labels are dropped under 6% (they would clip); the scale row
        # below always carries all three numbers
        la, ld, lb = (f"{w}%" if w >= 6 else "" for w in (wa, wd, wb))
        parts.append(
            '<div class="probs">\n'
            f'  <p class="sr-only">{_esc(team_a)} win {wa} percent, draw {wd} percent, '
            f'{_esc(team_b)} win {wb} percent.</p>\n'
            f'  <div class="probbar" aria-hidden="true">'
            f'<span class="pa" style="flex:{wa}">{la}</span>'
            f'<span class="pd" style="flex:{wd}">{ld}</span>'
            f'<span class="pb" style="flex:{wb}">{lb}</span></div>\n'
            f'  <div class="scale" aria-hidden="true"><span>{_esc(team_a)} {wa}%</span>'
            f'<span>draw {wd}%</span><span>{_esc(team_b)} {wb}%</span></div>\n'
            f'  <p class="factline">{" · ".join(facts)}<br>{srcline}{hfa}{graded}</p>\n'
            '</div>')
    if prebaked_lean:
        parts.append('<div class="prose"><blockquote><p><strong>Pre-baked lean '
                     f'(June 11):</strong> {sc._inline(prebaked_lean)}</p></blockquote></div>')
    return "\n".join(parts)


# ---------------------------------------------------------------- the market

def load_odds_engine():
    """Defensive adapter around odds.py + ledger.py (Phase 5, owned by another
    stream). Returns (callable, ledger_line, None) or (None, None, reason).
    The callable maps a fixtures row -> dict for render_market(); returns None
    for matches with no snapshot (placeholder state, per contract)."""
    try:
        import odds as od
        import ledger as lg
        import predict as pr
        odds_rows = od.load_odds()
        if not odds_rows:
            return None, None, "odds_log.csv is empty — no snapshots yet"
        ledger_rows = lg.load_ledger()
        picks = od.load_picks()
        model = pr.load_ratings()
    except Exception as e:                      # broad on purpose: never break the build
        return None, None, f"odds engine unavailable ({e.__class__.__name__}: {e})"

    def call(row: dict) -> dict | None:
        try:
            mid = row["match_id"]
            match_rows = [r for r in odds_rows
                          if r["match_id"] == mid and r["phase"] == "snapshot"]
            if not match_rows:
                return None
            a, b = row["team_a"].strip(), row["team_b"].strip()
            host = pr.HOST_BY_COUNTRY.get((row.get("country") or "").strip())
            hfa = host if host in (a, b) else None
            pred = pr.predict_match(model, a, b, hfa_team=hfa)
            ev = od.evaluate_match(mid, odds_rows, ledger_rows, pred)
            if not any(ev.get(m) for m in ("h2h", "totals", "spreads", "btts")):
                return None
            pick, flags = od.best_bet(ev)
            return {
                "evaluation": ev, "pick": pick, "flags": flags,
                "best_prices": od._best_prices(odds_rows, mid),
                "recorded": [p for p in picks if p["match_id"] == mid],
                "threshold": od.EDGE_THRESHOLD,
                "snapshot_ts": max(r["timestamp"] for r in match_rows),
            }
        except Exception:
            return None

    return call, od.units_summary(picks), None


_MARKET_LABELS = {"h2h": "1X2", "totals": "Total goals", "spreads": "Asian handicap",
                  "btts": "Both teams to score"}


def _sel_label(market: str, sel: str, line: str, team_a: str, team_b: str) -> str:
    if market == "h2h":
        return {"home": team_a, "draw": "Draw", "away": team_b}.get(sel, sel)
    if market == "totals":
        return f"{sel.capitalize()} {line}"
    if market == "spreads":
        team = team_a if sel == "home" else team_b
        try:
            return f"{team} {float(line):+g}"
        except (TypeError, ValueError):
            return f"{team} {line}"
    if market == "btts":
        return f"Both score — {sel}"
    return sel


def _fmt_snapshot_ts(ts: str) -> str:
    try:
        from datetime import datetime as _dt
        d = _dt.fromisoformat(ts)
        return f"{d:%b} {d.day}, {d:%I:%M %p} ET".replace(" 0", " ")
    except ValueError:
        return ts


def render_market(odds_info: dict | None, team_a: str, team_b: str,
                  prebaked: str | None, played: bool = False) -> str:
    """Odds & Best Bet block: the de-vigged edge table + the pick when the
    odds engine has a snapshot; the contract placeholder otherwise. Numbers
    come exclusively from odds.py — this function only formats."""
    if odds_info is None:
        return render_odds(prebaked, played=played)

    ev = odds_info["evaluation"]
    pick = odds_info["pick"]
    threshold = odds_info["threshold"]
    parts = []

    rows_html = []
    for market in ("h2h", "totals", "spreads", "btts"):
        for sel, line, odds_v, implied, our_p, edge in ev.get(market, []):
            is_pick = (pick is not None and pick["market"] == market
                       and pick["selection"] == sel and str(pick["line"]) == str(line))
            cls = ' class="pick-row"' if is_pick else ""
            edge_cls = "edge-pos" if edge >= threshold else ("edge-neg" if edge < 0 else "")
            rows_html.append(
                f'      <tr{cls}><td class="lbl">{_esc(_MARKET_LABELS[market])}</td>'
                f'<td class="lbl">{_esc(_sel_label(market, sel, line, team_a, team_b))}</td>'
                f'<td>{odds_v:.2f}</td><td>{implied:.0%}</td><td>{our_p:.0%}</td>'
                f'<td class="{edge_cls}">{edge:+.1%}</td></tr>')
    if rows_html:
        parts.append(
            '<div class="edge-wrap">\n  <table>\n'
            '    <caption class="sr-only">Market odds versus the model: implied '
            'probability, our probability, and the edge per selection</caption>\n'
            '    <thead><tr><th class="lbl" scope="col">Market</th>'
            '<th class="lbl" scope="col">Selection</th><th scope="col">Odds</th>'
            '<th scope="col">Implied</th><th scope="col">Ours</th>'
            '<th scope="col">Edge</th></tr></thead>\n    <tbody>\n'
            + "\n".join(rows_html) + "\n    </tbody>\n  </table>\n</div>")

    if pick:
        bp = odds_info["best_prices"].get(
            (pick["market"], pick["selection"], str(pick["line"])))
        price = f' — best price {bp[0]:.2f} ({_esc(bp[1])})' if bp else ""
        parts.append(
            '<div class="bet-callout"><span class="tag">Best bet</span>'
            f'<p><strong>{_esc(_sel_label(pick["market"], pick["selection"], pick["line"], team_a, team_b))}'
            f'</strong> ({_MARKET_LABELS[pick["market"]]}) @ {pick["odds"]:.2f}, '
            f'edge <strong>{pick["edge"]:+.1%}</strong>{price}. Flat 1u, paper record.</p></div>')
    elif rows_html:
        parts.append(f'<p class="no-bet"><b>NO BET</b> — no edge clears the '
                     f'{threshold:.0%} threshold (a normal, expected result).</p>')

    for rec in odds_info.get("recorded", []):
        status = rec.get("status", "open")
        line_bits = [f'Logged pick: {_esc(_sel_label(rec["market"], rec["selection"], rec["line"], team_a, team_b))} '
                     f'@ {_esc(rec["odds"])} ({_esc(rec["book"])}), edge {_esc(rec["edge_pp"])}pp']
        if status != "open":
            line_bits.append(f'settled <b>{_esc(status)}</b> for {_esc(rec["units"])}u')
            if rec.get("clv_pp"):
                line_bits.append(f'CLV {_esc(rec["clv_pp"])}pp')
        else:
            line_bits.append("open")
        parts.append(f'<p class="odds-note">{" · ".join(line_bits)}.</p>')

    for fl in odds_info.get("flags", []):
        parts.append(f'<p class="verify-flag">{_esc(fl)}</p>')

    notes = [f'market snapshot {_esc(_fmt_snapshot_ts(odds_info["snapshot_ts"]))} · '
             'median odds across books, de-vigged multiplicatively']
    if ev.get("totals") or ev.get("spreads") or ev.get("btts"):
        notes.append("totals / handicap / BTTS are model-priced from the score "
                     "matrix — the Opta overlay covers W/D/L only")
    notes.extend(ev.get("missing", []))
    parts.append('<p class="odds-note">' + " · ".join(_esc(n) for n in notes) + ".</p>")

    if prebaked:
        parts.append('<div class="prose"><blockquote><p><strong>Markets to watch '
                     f'(pre-baked):</strong> {sc._inline(prebaked)}</p></blockquote></div>')
    return "\n".join(parts)


# ---------------------------------------------------------------- match pages

_CALLOUT_SECTIONS = {"Key Duel", "Watch For", "Margin Notes",
                     "Shapes & Selection", "Projected Shapes & Selection Questions"}


def _strip_placeholder(body: str) -> str:
    m = _PLACEHOLDER_BRACKETS.match(body.strip())
    return m.group(1).strip() if m else body.strip()


def _clean_slot(body: str | None) -> str | None:
    """Pre-baked Call/Odds slot text -> the human part: unwrap the *[...]*
    placeholder and drop machine prefixes ('Model pending — lean:',
    'Phase 3 — markets to watch:')."""
    if not body:
        return None
    out = _MACHINE_PREFIX.sub("", _strip_placeholder(body)).strip()
    return out or None


def _stakes_line(gt: "st.GroupTable", team_a: str, team_b: str, matchday: int) -> str:
    """build_edition's factual stakes sentence, except its 'opens with this
    matchday' wording is only true on MD1 — later matchdays of an unstarted
    group get accurate phrasing instead."""
    played = sum(r.played for r in gt.rows) // 2
    if played == 0 and matchday > 1:
        return (f"Group {gt.group} hasn't kicked off yet — all four teams on "
                f"0 points, with this matchday-{matchday} meeting still ahead.")
    return be.stakes_sentence(gt, team_a, team_b)


def render_card_sections(sections: list[tuple[str, str]]) -> str:
    """The pre-baked tactical read: Matchup prose up top, the punchy sections
    as callout boxes, in card order. Live slots (Stakes/Call/Odds) are handled
    elsewhere and skipped here."""
    prose, callouts = [], []
    for label, body in sections:
        if not body:
            continue
        if label in ("Stakes", "The Call", "Odds & Best Bet",
                     "The Call / Odds & Best Bet"):
            continue
        if label in _CALLOUT_SECTIONS:
            tag = "Shapes & Selection" if label.startswith("Projected") else label
            callouts.append(f'  <div class="callout"><span class="tag">{_esc(tag)}</span>'
                            f'{sc.md_to_html(body)}</div>')
        else:  # The Matchup, Recap notes, preamble
            if label and label not in ("The Matchup",):
                prose.append(f"<p><strong>{_esc(label)}:</strong></p>")
            prose.append(sc.md_to_html(body))
    out = []
    if prose:
        out.append('<div class="prose">\n' + "\n".join(prose) + "\n</div>")
    if callouts:
        out.append('<div class="callout-grid">\n' + "\n".join(callouts) + "\n</div>")
    if any("verify" in (b or "").lower() for _, b in sections):
        out.append('<p class="verify-flag">Selection notes were pre-baked June 11 '
                   'and are verified day-of in the edition, not here — anything '
                   'marked “verify” must be confirmed before it is load-bearing.</p>')
    return "\n".join(out) or '<div class="placeholder-slot">No pre-baked card found for this match.</div>'


def render_odds(prebaked: str | None, played: bool = False) -> str:
    if played:
        slot = ('<div class="placeholder-slot">No odds were logged for this match — '
                'the market workflow (CLAUDE.md Phase 3: odds_log.csv, de-vig, edge '
                'vs threshold) was not live before kickoff.</div>')
    else:
        slot = ('<div class="placeholder-slot">No odds snapshot logged yet — this '
                'section activates with the odds workflow (CLAUDE.md Phase 3: '
                'odds_log.csv, de-vig, edge vs threshold). Odds are never invented.</div>')
    out = [slot]
    if prebaked:
        out.append('<div class="prose"><blockquote><p><strong>Markets to watch '
                   f'(pre-baked):</strong> {sc._inline(prebaked)}</p></blockquote></div>')
    return "\n".join(out)


def render_match_page(row: dict, s: "st.Standings",
                      forms: dict[str, list[str | None]],
                      cards_dir: Path, info: dict | None, css: str,
                      template_dir: Path = TEMPLATE_DIR,
                      warnings: list[str] | None = None,
                      odds_info: dict | None = None) -> str:
    mid, g = row["match_id"].strip(), row["group"].strip()
    team_a, team_b = row["team_a"].strip(), row["team_b"].strip()
    played = (row.get("status") or "").strip().lower() == "played"
    try:
        matchday = int(str(row.get("matchday") or "").strip() or 0)
    except ValueError:
        matchday = 0

    card_text, _src = be.extract_card(mid, team_a, team_b, cards_dir)
    sections: list[tuple[str, str]] = []
    if card_text:
        _hdr, sections = sc.parse_card(card_text)
    elif warnings is not None:
        warnings.append(f"{mid}: no card found — match page renders without the tactical read")

    by_label = {label: body for label, body in sections}
    lean = _clean_slot(by_label.get("The Call")
                       or by_label.get("The Call / Odds & Best Bet"))
    # MD3 cards combine Call+Odds in one lean line with no markets text:
    # quoting it twice would duplicate the lean, so Odds only quotes its own label.
    odds_note = _clean_slot(by_label.get("Odds & Best Bet"))

    result = None
    if played:
        try:
            result = (int(str(row["score_a"]).strip()), int(str(row["score_b"]).strip()))
        except (ValueError, KeyError):
            result = None

    gt = s.groups.get(g)
    stakes = _stakes_line(gt, team_a, team_b, matchday) if gt else ""
    mini = render_group_card(gt, forms, 0, root="../") if gt else ""

    scoreline = ""
    if result is not None:
        scoreline = (f'<p class="scoreline">{result[0]}–{result[1]}'
                     f'<span class="sr-only"> — final score: {_esc(team_a)} '
                     f'{result[0]}, {_esc(team_b)} {result[1]}</span></p>')

    moon = " · ☾ midnight ET, previous evening's slate" if row.get("_late_cap") else ""
    when = f'{_esc((row.get("date_et") or "").strip())} · ' \
           f'{_esc((row.get("kickoff_et") or "").strip())} ET{moon}'
    venue = ", ".join(p for p in [(row.get("stadium") or "").strip(),
                                  (row.get("city") or "").strip()] if p)

    tpl = Template((template_dir / "match.html").read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        site_css=css,
        match_id=_esc(mid),
        group=_esc(g),
        group_lower=_esc(g.lower()),
        matchday=matchday or "?",
        team_a=_esc(team_a),
        team_b=_esc(team_b),
        slug_a=sc.slugify(team_a),
        slug_b=sc.slugify(team_b),
        scoreline_html=scoreline,
        when=when,
        venue=_esc(venue) or "Venue TBD",
        tv=_esc((row.get("tv_us") or "").strip() or "TV TBD"),
        call_html=render_call(info, team_a, team_b, lean, result=result),
        stakes_sentence=_esc(stakes),
        mini_table_html=mini,
        card_html=render_card_sections(sections),
        odds_html=render_market(odds_info, team_a, team_b, odds_note, played=played),
        repo_url=REPO_URL,
    )


# ---------------------------------------------------------------- team pages

def _fixture_lines(team: str, rows: list[dict], root: str = "../") -> str:
    mine = sorted((r for r in rows if team in (r["team_a"].strip(), r["team_b"].strip())),
                  key=lambda r: int(r["match_id"][1]))
    out = []
    for r in mine:
        mid = r["match_id"]
        opp = r["team_b"].strip() if r["team_a"].strip() == team else r["team_a"].strip()
        at_home = r["team_a"].strip() == team
        played = (r.get("status") or "").strip().lower() == "played"
        link = (f'<a href="{root}matches/{_esc(mid)}.html">'
                f'{"vs" if at_home else "at"} {_esc(opp)}</a>')
        if played:
            us = int(r["score_a"]) if at_home else int(r["score_b"])
            them = int(r["score_b"]) if at_home else int(r["score_a"])
            letter = "W" if us > them else ("L" if us < them else "D")
            res = f'<span class="res">{us}–{them} {letter}</span>'
            when = "FT"
        else:
            res = ""
            when = f'{(r.get("date_et") or "").strip()} · {(r.get("kickoff_et") or "").strip()} ET'
            if r.get("_late_cap"):
                when += " ☾"
        out.append(f'    <li><span class="md">MD{(int(mid[1]) + 1) // 2}</span>'
                   f'<span class="fx">{link}</span>{res}'
                   f'<span class="when">{_esc(when)}</span></li>')
    return "\n".join(out)


_STRAP_ORDER = ("Manager", "Captain", "Projected XI shape", "World Cup history",
                "Most appearances", "Record goalscorer")


def render_team_page(profile: "sc.TeamProfile", s: "st.Standings",
                     forms: dict[str, list[str | None]], rows: list[dict],
                     css: str, template_dir: Path = TEMPLATE_DIR) -> str:
    team, g = profile.team, profile.group
    gt = s.groups.get(g)
    standing_line = "standing pending"
    if gt:
        group_played = sum(r.played for r in gt.rows) // 2
        if group_played == 0:
            standing_line = "all level — group not yet started"
        else:
            for pos, r in enumerate(gt.rows, 1):
                if r.team == team:
                    standing_line = (f"{be._ordinal(pos)} · {r.points} pt"
                                     f"{'s' if r.points != 1 else ''} · GD {st._fmt_gd(r.gd)}")
                    break
    form = forms.get(team, [None] * GAMES_PER_TEAM)

    strap = []
    seen = set()
    for key in _STRAP_ORDER:
        if key in profile.facts:
            strap.append(f'    <span><b>{_esc(key)}:</b> {sc._inline(profile.facts[key])}</span>')
            seen.add(key)
    for key, val in profile.facts.items():
        if key not in seen:
            strap.append(f'    <span><b>{_esc(key)}:</b> {sc._inline(val)}</span>')

    callouts = []
    for tag, body in (("Key player", profile.key_player),
                      ("Rising star", profile.rising_star),
                      ("Fun fact", profile.fun_fact)):
        if body:
            callouts.append(f'  <div class="callout"><span class="tag">{_esc(tag)}</span>'
                            f'<p>{sc._inline(body)}</p></div>')

    squad = "\n".join(
        f'    <div><dt>{_esc(pos)}</dt><dd>{_esc(names)}</dd></div>'
        for pos, names in profile.squad)

    tpl = Template((template_dir / "team.html").read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        site_css=css,
        team=_esc(team),
        group=_esc(g),
        group_lower=g.lower(),
        standing_line=_esc(standing_line),
        form_pips=_pips_html(form),
        strap_html="\n".join(strap),
        fixtures_html=_fixture_lines(team, rows),
        tactical_html=sc.md_to_html("\n\n".join(profile.tactical)),
        callouts_html="\n".join(callouts),
        squad_html=squad,
        repo_url=REPO_URL,
    )


# ---------------------------------------------------------------- assembly

def build_page(matches: "list[st.Match]", rows: list[dict], target: date,
               generated_at: str, template_path: Path = TEMPLATE_DIR / "page.html",
               editions_dir: Path = REPO_ROOT / "editions",
               css: str | None = None,
               ledger_line: str | None = None,
               fair_play: dict[str, int] | None = None,
               blurb_html: str = "",
               slate_picks: dict[str, str] | None = None) -> tuple[str, dict]:
    """Render the index page. Returns (html, data_dict)."""
    s = st.compute_standings(matches, fair_play=fair_play)
    forms = form_by_team(matches)
    today = be.select_matches(rows, target)
    day_n = (target - be.TOURNAMENT_START).days + 1

    n = len(today)
    slate_title = (f"{target:%A, %B} {target.day} · "
                   + (f"{n} match" + ("" if n == 1 else "es") if n else "rest day"))

    groups_html = "\n".join(
        render_group_card(s.groups[g], forms, i)
        for i, g in enumerate(sorted(s.groups)))

    data = st.to_dict(s)
    data["generated_at"] = generated_at
    data["slate_date"] = target.isoformat()
    # < is valid JSON inside strings and defuses every HTML parser-escape
    # sequence (</script>, <!--) in the inline embed.
    data_json = json.dumps(data, ensure_ascii=False, indent=1).replace("<", "\\u003c")

    page = Template(template_path.read_text(encoding="utf-8")).safe_substitute(
        site_css=css if css is not None else _site_css(template_path.parent),
        edition_no=f"No. {day_n}" if day_n >= 1 else "Preview",
        pretty_date=f"{target:%A, %B} {target.day}, {target.year}",
        played=s.played,
        total=s.total,
        progress_pct=f"{(100 * s.played / s.total) if s.total else 0:.1f}",
        group_nav_html=_group_nav(s.groups),
        slate_title=slate_title,
        slate_html=render_slate(today, picks=slate_picks),
        groups_html=groups_html,
        thirds_html=render_thirds(s, forms),
        archive_html=_archive(editions_dir),
        generated_at=_esc(generated_at),
        repo_url=REPO_URL,
        data_json=data_json,
        ledger_html=(f'<p class="ledger-line">{_esc(ledger_line)}</p>'
                     if ledger_line else ""),
        blurb_html=blurb_html,
    )
    return page, data


def build_site(out_dir: Path, target: date, generated_at: str,
               fixtures: Path = REPO_ROOT / "data" / "fixtures.csv",
               cards_dir: Path = REPO_ROOT / "cards",
               kb_path: Path = KB_GUIDE,
               template_dir: Path = TEMPLATE_DIR,
               editions_dir: Path = REPO_ROOT / "editions",
               predictor="auto", odds_engine="auto") -> list[str]:
    """Render the whole site (index + team cards + match previews + data.json)
    into out_dir. Returns warnings. ``predictor`` and ``odds_engine`` are
    "auto" (load the real modules defensively), None (placeholder state), or
    a callable (tests)."""
    warnings: list[str] = []

    matches = st.load_fixtures(fixtures)
    rows = be.read_rows(fixtures)
    for r in rows:  # load_fixtures validated the stripped values; use the same
        for k in ("match_id", "group", "team_a", "team_b"):
            r[k] = (r.get(k) or "").strip()
    fair_play = load_discipline()
    s = st.compute_standings(matches, fair_play=fair_play)
    warnings.extend(s.warnings)
    forms = form_by_team(matches)
    css = _site_css(template_dir)

    blurb_html = ""
    blurb_path = BLURBS_DIR / f"{target.isoformat()}.md"
    if blurb_path.exists():
        blurb_text = blurb_path.read_text(encoding="utf-8").strip()
        if blurb_text:
            blurb_html = ('<div class="blurb">' + sc.md_to_html(blurb_text)
                          + '<p class="blurb-tag">— the morning line, generated '
                          'from the day\'s data</p></div>')

    if predictor == "auto":
        predictor, why = load_predictor()
        if why:
            warnings.append(f"The Call renders as placeholder: {why}")

    if odds_engine == "auto":
        odds_call, ledger_line, odds_why = load_odds_engine()
        if odds_why:
            warnings.append(f"Odds sections render as placeholder: {odds_why}")
    else:
        odds_call, ledger_line = odds_engine, None

    profiles, kb_warnings = sc.parse_kb(kb_path)
    warnings.extend(f"kb: {w}" for w in kb_warnings)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "teams").mkdir(exist_ok=True)
    (out_dir / "matches").mkdir(exist_ok=True)

    slate_picks: dict[str, str] = {}
    if odds_call:
        for r in be.select_matches(rows, target):
            o = odds_call(r)
            if o and o["pick"]:
                p = o["pick"]
                slate_picks[r["match_id"]] = (
                    f"{_sel_label(p['market'], p['selection'], p['line'], r['team_a'], r['team_b'])}"
                    f" {p['edge']:+.1%}")

    index, data = build_page(matches, rows, target, generated_at,
                             template_path=template_dir / "page.html",
                             editions_dir=editions_dir, css=css,
                             ledger_line=ledger_line, fair_play=fair_play,
                             blurb_html=blurb_html, slate_picks=slate_picks)
    (out_dir / "index.html").write_text(index, encoding="utf-8")
    (out_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    fixture_teams = sorted({m.team_a for m in matches} | {m.team_b for m in matches})
    for team in fixture_teams:
        profile = profiles.get(team)
        if profile is None:
            warnings.append(f"kb: no profile for {team!r} — team page skipped")
            continue
        page = render_team_page(profile, s, forms, rows, css, template_dir)
        (out_dir / "teams" / f"{sc.slugify(team)}.html").write_text(page, encoding="utf-8")
    for team in sorted(set(profiles) - set(fixture_teams)):
        warnings.append(f"kb: profile {team!r} matches no fixtures team (canon mismatch?)")

    ledger = _load_ledger(warnings)
    predictions = 0
    scheduled = 0
    for row in rows:
        played = (row.get("status") or "").strip().lower() == "played"
        kicked_off = False
        if not played and ledger is not None:
            try:
                kicked_off = ledger["now"] >= ledger["kickoff_dt"](row)
            except Exception:
                kicked_off = False
        if played or kicked_off:
            # honesty rule: once kickoff passes, ONLY the verified pre-kickoff
            # logged call may be shown — never a live recomputation
            info = _logged_call(row["match_id"], ledger, row, warnings)
            if info is not None and kicked_off and not played:
                info["awaiting"] = True
            if played and info is None:
                warnings.append(f"{row['match_id']}: played with no usable logged "
                                "call — rendered ungraded")
        else:
            scheduled += 1
            info = _safe_predict(predictor, row, warnings)
            predictions += info is not None
        odds_info = odds_call(row) if odds_call else None
        page = render_match_page(row, s, forms, cards_dir, info, css,
                                 template_dir, warnings, odds_info=odds_info)
        (out_dir / "matches" / f"{row['match_id']}.html").write_text(
            page, encoding="utf-8")
    if predictor is not None and scheduled and predictions == 0:
        warnings.append("predictor loaded but produced no usable prediction for "
                        "any match — every Call rendered as placeholder")

    # reconcile: a renamed slug or removed match_id must not leave a stale
    # page deployed under docs/
    expected = ({f"{sc.slugify(t)}.html" for t in fixture_teams},
                {f"{r['match_id']}.html" for r in rows})
    for subdir, keep in zip(("teams", "matches"), expected):
        for f in (out_dir / subdir).glob("*.html"):
            if f.name not in keep:
                f.unlink()
                warnings.append(f"removed stale page {subdir}/{f.name}")

    return warnings


def _load_ledger(warnings: list[str]) -> dict | None:
    """The prediction-ledger API surface this renderer relies on, loaded
    defensively: rows, the published-source constant, the canonical Brier
    function, and kickoff math. A missing/changed ledger module degrades to
    'no logged call', never a retroactive grade."""
    try:
        import ledger as lg
        return {"rows": lg.load_ledger(),
                "published": getattr(lg, "PUBLISHED_SOURCE", "consensus"),
                "brier": lg.brier,
                "kickoff_dt": lg.kickoff_dt,
                "now": lg.now_et()}
    except Exception as e:
        warnings.append(f"prediction ledger unavailable ({e.__class__.__name__}: {e}) "
                        "— played matches render as 'no logged call'")
        return None


def _logged_call(match_id: str, ledger: dict | None, fixture_row: dict,
                 warnings: list[str]) -> dict | None:
    """The published consensus for a match, as render_call info — but only if
    it withstands integrity checks: probabilities valid per the contract, and
    the log timestamp VERIFIABLY before kickoff. Anything unverifiable renders
    as 'no logged call' rather than being stamped pre-kickoff."""
    import math
    from datetime import datetime as _dt
    if ledger is None:
        return None
    row = None
    for r in ledger["rows"]:   # last row wins, matching ledger.grade()
        if r.get("match_id") == match_id and r.get("source") == ledger["published"]:
            row = r
    if row is None:
        return None
    try:
        probs = (float(row["p_home"]), float(row["p_draw"]), float(row["p_away"]))
    except (KeyError, ValueError):
        warnings.append(f"{match_id}: malformed ledger row — rendered as no logged call")
        return None
    if (not all(math.isfinite(p) and 0 <= p <= 1 for p in probs)
            or abs(sum(probs) - 1.0) > 0.001):
        warnings.append(f"{match_id}: ledger probabilities fail the 1.0±0.001 "
                        "contract — rendered as no logged call")
        return None
    try:
        ts = _dt.fromisoformat((row.get("timestamp") or "").strip())
        if ts >= ledger["kickoff_dt"](fixture_row):
            warnings.append(f"{match_id}: ledger row logged at/after kickoff — "
                            "refused (no post-hoc grading)")
            return None
    except (ValueError, TypeError, KeyError) as e:
        warnings.append(f"{match_id}: cannot verify pre-kickoff timestamp "
                        f"({e.__class__.__name__}) — rendered as no logged call")
        return None
    return {"p_a": probs[0], "p_draw": probs[1], "p_b": probs[2],
            "predicted_score": (row.get("predicted_score") or "").strip(),
            "logged_ts": row.get("timestamp", ""), "logged": True,
            "brier_fn": ledger["brier"]}


def _safe_predict(predictor, row: dict, warnings: list[str]) -> dict | None:
    """Run the predictor for one row and validate its output; any failure or
    malformed value degrades that match to the placeholder, with a warning."""
    if predictor is None:
        return None
    import math
    try:
        info = predictor(row)
        if info is None:
            return None
        probs = (info["p_a"], info["p_draw"], info["p_b"])
        if not all(isinstance(p, (int, float)) and math.isfinite(p) and 0 <= p <= 1
                   for p in probs):
            raise ValueError(f"non-finite/out-of-range probabilities {probs}")
        info["modal_score"]  # required keys
        info["total"]
        return info
    except Exception as e:
        warnings.append(f"{row.get('match_id', '?')}: prediction skipped "
                        f"({e.__class__.__name__}: {e}) — Call rendered as placeholder")
        return None


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render the WC26 hub: standings, team cards, matchup previews.")
    ap.add_argument("--date", default=None,
                    help="editorial date for the slate, YYYY-MM-DD (default: today)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "docs")
    ap.add_argument("--template-dir", type=Path, default=TEMPLATE_DIR)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        target = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2
    if not args.fixtures.exists():
        print(f"error: {args.fixtures} not found.", file=sys.stderr)
        return 1
    for tname in ("page.html", "team.html", "match.html", "site.css"):
        if not (args.template_dir / tname).exists():
            print(f"error: template {args.template_dir / tname} not found.", file=sys.stderr)
            return 1

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        warnings = build_site(args.out_dir, target, generated_at,
                              fixtures=args.fixtures, template_dir=args.template_dir)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(str(args.out_dir / "index.html"))
    print(f"{len(list((args.out_dir / 'teams').glob('*.html')))} team cards, "
          f"{len(list((args.out_dir / 'matches').glob('*.html')))} match previews")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
