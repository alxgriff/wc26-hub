#!/usr/bin/env python3
"""WC26 static site builder — standings hub + team cards + matchup previews.

Outputs (all self-contained HTML, inline CSS from templates/site.css, minimal inline JS):

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
from datetime import date, datetime, timedelta
from pathlib import Path
from string import Template
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st            # noqa: E402
import build_edition as be        # noqa: E402
import site_content as sc         # noqa: E402
import scenarios as scen          # noqa: E402
import bracket as bk              # noqa: E402

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


def _team_cell(r: "st.TeamRow", syms: dict[str, str], root: str,
               fate: str | None = None) -> str:
    dag = syms.get(r.team, "")
    dag_html = f'<span class="dag">{dag}</span>' if dag else ""
    sr = (f'<span class="sr-only">{FATE_SR[fate]}</span>'
          if fate in FATE_SR else "")
    return (f'<th class="team" scope="row" title="{_esc(r.team)}">'
            f'<a href="{_team_link(r.team, root)}">{_esc(r.team)}</a>{sr}{dag_html}</th>')


def render_group_card(gt: "st.GroupTable", forms: dict[str, list[str | None]],
                      index: int, root: str = "",
                      fates: dict[str, str] | None = None) -> str:
    g = gt.group
    fates = fates or {}
    played = sum(r.played for r in gt.rows) // 2
    syms, sym_notes = _note_daggers(gt.rows, gt.notes)
    rows_html = []
    for pos, r in enumerate(gt.rows, 1):
        zone = "zone-top" if pos <= 2 else ("zone-third" if pos == 3 else "zone-out")
        fate = fates.get(r.team)
        if fate in FATE_SR:
            zone += f" fate-{fate}"
        form = forms.get(r.team, [None] * GAMES_PER_TEAM)
        rows_html.append(
            f'      <tr class="{zone}">\n'
            f'        <td class="pos">{pos}</td>\n'
            f'        {_team_cell(r, syms, root, fate)}\n'
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
                  root: str = "", fates: dict[str, str] | None = None) -> str:
    rows = s.third_place
    fates = fates or {}
    if not rows:
        return '<p class="standfirst">No third-place table yet — no completed group rows.</p>'
    syms, sym_notes = _note_daggers(rows, s.third_place_notes)
    body = []
    for pos, r in enumerate(rows, 1):
        qualifying = pos <= st.QUALIFYING_THIRDS
        cls = "q" if qualifying else "below"
        fate = fates.get(r.team)
        if fate in FATE_SR:
            cls += f" fate-{fate}"
        in_cell = ('<td class="in">✓<span class="sr-only"> qualifying as it stands</span></td>'
                   if qualifying else '<td class="in"><span class="sr-only">out as it stands</span></td>')
        body.append(
            f'      <tr class="{cls}">\n'
            f'        <td class="pos">{pos}</td>\n'
            f'        {_team_cell(r, syms, root, fate)}\n'
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
    cards = []
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
            kick = '<p class="kick"><span class="ftbadge">Full time</span></p>'
            centre = (f'<span class="sc">{_esc(str(r["score_a"]))}–'
                      f'{_esc(str(r["score_b"]))}</span>')
        else:
            kick = (f'<p class="kick">{_esc((r.get("kickoff_et") or "").strip())} ET'
                    f'{moon}</p>')
            centre = '<span class="v">v</span>'
        # the whole matchup is one link to the preview (a large, single target)
        matchup = (f'<a class="matchup" href="{href}">'
                   f'<span class="team ta">{_esc(r["team_a"])}</span>{centre}'
                   f'<span class="team tb">{_esc(r["team_b"])}</span></a>')
        pick_html = ""
        if mid in picks:
            pick_html = f'<p class="pickline">▸ best bet: {_esc(picks[mid])}</p>'
        cards.append(
            f'    <li class="card">{kick}{matchup}'
            f'<p class="meta">{_esc(mid)} · {_esc(tv)} · {_esc(venue)} · '
            f'<span class="pv">preview →</span></p>{pick_html}</li>')
    return "\n".join(cards)


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
                "lambda_a": p.lambda_a, "lambda_b": p.lambda_b,
            }
        except Exception:
            return None

    return call, None


def load_knockout_resolver():
    """Defensive adapter for projecting knockout ties (companion to load_predictor).
    Returns resolver(team_a, team_b) -> {"winner","loser","p"} | None, or None if
    predict.py is unavailable — in which case the bracket stays structural (blank
    downstream). Neutral venue: host HFA is NOT applied, because knockout venues
    aren't modelled in-repo. ``p`` is the winner's probability of advancing (90' +
    extra time + shootout, per predict.resolve_knockout)."""
    try:
        import predict as pr
        model = pr.load_ratings()
    except Exception:
        return None

    def resolve(a: str, b: str):
        try:
            kp = pr.resolve_knockout(model, a, b)        # neutral (no hfa_team)
            if kp.p_advance_a >= kp.p_advance_b:
                return {"winner": a, "loser": b, "p": kp.p_advance_a}
            return {"winner": b, "loser": a, "p": kp.p_advance_b}
        except Exception:
            return None

    return resolve


def load_group_projector():
    """Defensive adapter for the 'projected finish' bracket: score(match) -> (score_a, score_b)
    = the model's most-likely DECISIVE scoreline for a group game, or None if predict.py is
    unavailable. Decisive (skips the modal draw) so the projected final tables don't tie into a
    provisional third-place cutline that would block the bracket. Neutral venue (Match carries
    no country, and KO venues aren't modelled either — kept consistent)."""
    try:
        import predict as pr
        model = pr.load_ratings()
    except Exception:
        return None

    def score(m):
        try:
            a, b = pr._canon(m.team_a), pr._canon(m.team_b)
            if a not in model.teams or b not in model.teams:
                return (1, 0)
            pred = pr.predict_match(model, a, b)         # neutral
            for (i, j), _p in pred.top_scores:           # most-likely scorelines, high→low
                if i != j:
                    return (i, j)                        # first decisive one
            return (1, 0) if pred.p_a >= pred.p_b else (0, 1)   # all-draw top: favoured side by 1
        except Exception:
            return (1, 0)

    return score


def render_call(info: dict | None, team_a: str, team_b: str,
                prebaked_lean: str | None,
                result: tuple[int, int] | None = None,
                kicked_off: bool = False) -> str:
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
        elif kicked_off:
            parts.append(
                '<div class="placeholder-slot">No prediction was logged before this '
                'match kicked off — and the honesty rule forbids making or grading a '
                'call after kickoff, so no number is shown here.</div>')
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

def load_odds_engine(now=None):
    """Defensive adapter around odds.py + ledger.py (Phase 5, owned by another
    stream). Returns (callable, ledger_line, None) or (None, None, reason).
    The callable maps a fixtures row -> dict for render_market(); returns None
    for matches with no snapshot (placeholder state, per contract). ``now`` pins the
    clock for the per-market freshness check (so the card demotes a best bet whose
    line is too stale to record — same 12h gate as recording)."""
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
        _now = now or lg.now_et()
        # read here (not via getattr inside the swallowing closure) so a rename of
        # RECORD_THRESHOLD fails loudly as a clear "engine unavailable" reason rather
        # than silently desyncing the displayed bar to 0.05 in every pick callout
        record_threshold = od.RECORD_THRESHOLD
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
            match_picks, flags = od.best_bets(ev)
            # a best bet whose market line is older than the recording freshness gate
            # can't be recorded — flag it so the card shows it as a stale lean, not a
            # live "Best bet" (keeps the card consistent with the picks ledger).
            stale = {m for m in {p["market"] for p in match_picks}
                     if (age := od._snapshot_age_hours(odds_rows, mid, _now, m)) is None
                     or age > od.MAX_SNAPSHOT_AGE_HOURS}
            return {
                "evaluation": ev, "picks": match_picks,
                "pick": match_picks[0] if match_picks else None,  # back-compat
                "flags": flags,
                "stale_markets": stale,
                "max_age_h": od.MAX_SNAPSHOT_AGE_HOURS,
                "best_prices": od._best_prices(odds_rows, mid),
                "recorded": [p for p in picks if p["match_id"] == mid],
                "threshold": od.EDGE_THRESHOLD,
                "record_threshold": record_threshold,
                "model_sanity": od.MODEL_PRICED_SANITY,
                "sanity": od.SANITY_EDGE,
                "source_label": od.snapshot_source_label(odds_rows, mid),
                "snapshot_ts": max(r["timestamp"] for r in match_rows),
                "projection": {"total": pred.total,
                               "over": {f"{k:g}": v for k, v in pred.over.items()}},
            }
        except Exception:
            return None

    return call, od.units_summary(picks), None


# ---------------------------------------------------------------- the conditions (sweat factor)

def load_weather_engine():
    """Defensive adapter around weather.py (Phase 7). Returns (callable, None) or
    (None, reason). The callable maps a fixtures row → enriched info dict or None.
    Never breaks the build: any failure returns (None, reason)."""
    log_path = REPO_ROOT / "data" / "weather_log.csv"
    climate_path = REPO_ROOT / "data" / "team_climate.csv"
    try:
        import weather as wx
        if not log_path.exists():
            return None, "weather_log.csv not found — run weather.py --date first"
        baselines = wx.load_team_climate(climate_path)
    except Exception as e:
        return None, f"weather engine unavailable ({e.__class__.__name__}: {e})"

    def call(row: dict) -> dict | None:
        try:
            mid = row["match_id"]
            wx_row = wx.to_dict(mid, log_path)
            if wx_row is None:
                return None
            a, b = row["team_a"].strip(), row["team_b"].strip()
            cc = str(wx_row.get("climate_controlled", "")).strip().lower() == "true"
            result: dict = {
                "temp_c": wx_row.get("temp_c", ""),
                "rh_pct": wx_row.get("rh_pct", ""),
                "wbgt_est": wx_row.get("wbgt_est", ""),
                "climate_controlled": cc,
                "as_of": wx_row.get("as_of", ""),
                "source": wx_row.get("source", "forecast"),
                "dis_a": None, "delta_a": None,
                "dis_b": None, "delta_b": None,
                "mhi": None, "sf": None, "severity": None,
                "hum_desc": "",
            }
            if not cc:
                wbgt = float(wx_row["wbgt_est"])
                mhi_v, _, _ = wx.sweat_components(wbgt, wbgt)  # mhi only, delta=0
                result["mhi"] = round(mhi_v)
                result["hum_desc"] = wx._humidity_desc(float(wx_row["rh_pct"]))
                for team, key in ((a, "a"), (b, "b")):
                    bl = baselines.get(team)
                    if bl and bl.baseline_wbgt is not None:
                        mhi_v2, delta, dis = wx.sweat_components(wbgt, bl.baseline_wbgt)
                        result[f"dis_{key}"] = round(dis)
                        result[f"delta_{key}"] = delta
                max_dis = max(
                    result["dis_a"] if result["dis_a"] is not None else 0,
                    result["dis_b"] if result["dis_b"] is not None else 0,
                )
                result["sf"] = wx.sweat_factor(mhi_v, max_dis)
                result["severity"] = wx.severity_label(result["sf"])
            return result
        except Exception:
            return None

    return call, None


def _sweat_blurb(info: dict, team_a: str, team_b: str) -> str:
    """One factual sentence interpreting the sweat factor for this match."""
    sf = info.get("sf") or 0
    wbgt = float(info.get("wbgt_est") or 0)
    dis_a = info.get("dis_a") or 0
    delta_a = info.get("delta_a")
    dis_b = info.get("dis_b") or 0
    delta_b = info.get("delta_b")

    # Identify which team has more/less disadvantage
    if delta_a is not None and delta_b is not None:
        if dis_a >= dis_b:
            harder, easier = team_a, team_b
            hard_dis, easy_dis = dis_a, dis_b
            hard_delta, easy_delta = delta_a, delta_b
        else:
            harder, easier = team_b, team_a
            hard_dis, easy_dis = dis_b, dis_a
            hard_delta, easy_delta = delta_b, delta_a
        mismatch = (hard_dis - easy_dis) >= 20
    else:
        harder = easier = None
        hard_dis = easy_dis = 0
        hard_delta = easy_delta = None
        mismatch = False

    def _sign(d: float) -> str:
        d_f = d * 9 / 5
        return f"+{round(d_f)}" if d_f > 0 else str(round(d_f))

    wbgt_f = wbgt * 9 / 5 + 32

    if sf < 25:
        if mismatch and harder and hard_delta is not None:
            return (f"{harder} step into conditions {_sign(hard_delta)}°F from their home baseline, "
                    f"but the overall heat is low — unlikely to matter much.")
        return "Comfortable conditions — heat is a non-factor for both sides."

    if sf < 50:
        if mismatch and harder and hard_delta is not None:
            adapted = f"{easier} are the better-acclimated side" if (easy_delta or 0) < 3 else f"{easier} face a smaller adjustment"
            return (f"Warm out; {harder} are {_sign(hard_delta)}°F outside their comfort zone "
                    f"while {adapted}.")
        return f"Warm conditions ({wbgt_f:.0f}°F WBGT) — a moderate physical test for both squads."

    if sf < 75:
        if mismatch and harder and hard_delta is not None:
            if (easy_delta or 0) <= 0:
                return (f"Hot match-up, and a real edge for {easier}: "
                        f"{harder} are {_sign(hard_delta)}°F above their home climate "
                        f"while {easier} are well-adapted to this heat.")
            return (f"Significant heat; {harder} face the steeper climb "
                    f"({_sign(hard_delta)}°F vs home) compared to {easier}.")
        return f"Hot conditions ({wbgt_f:.0f}°F WBGT) — both sides will feel the physical load."

    # Severe
    if mismatch and harder and hard_delta is not None:
        if (easy_delta or 0) <= 0:
            return (f"Brutal heat — and {easier} hold a major acclimatization edge: "
                    f"{harder} are {_sign(hard_delta)}°F above their home baseline "
                    f"while {easier} are stepping into their climate.")
        return (f"Severe heat, and {harder} carry the bigger burden: "
                f"{_sign(hard_delta)}°F above their home baseline vs "
                f"{_sign(easy_delta)}°F for {easier}.")
    return f"Severe conditions ({wbgt_f:.0f}°F WBGT) — this is a demanding physical environment for both teams."


def render_sweat(info: dict | None, team_a: str, team_b: str) -> str:
    """Sweat Factor block. Placeholder when info is None."""
    if info is None:
        return (
            '<div class="placeholder-slot">Sweat Factor forecast pending — this match is not '
            'yet within the 16-day Open-Meteo forecast window. The section fills automatically '
            'once available; no data is invented.</div>'
        )
    if info.get("climate_controlled"):
        return '<p class="cond-ac">Indoors — climate-controlled. Heat not a factor.</p>'

    temp = _esc(f"~{float(info['temp_c']) * 9 / 5 + 32:.0f}")
    rh = _esc(f"{float(info['rh_pct']):.0f}")
    wbgt = _esc(f"{float(info['wbgt_est']) * 9 / 5 + 32:.1f}")
    hum = _esc(info.get("hum_desc") or "")
    source = (info.get("source") or "forecast").capitalize()
    as_of = _esc(info.get("as_of") or "")
    stamp = f"{source} · as of {as_of}" if as_of else source
    severity = _esc(info.get("severity") or "")
    sev_cls = (info.get("severity") or "mild").lower()
    mhi = info.get("mhi") or 0
    blurb = _esc(_sweat_blurb(info, team_a, team_b))

    def team_row(team: str, dis, delta) -> str:
        if delta is None:
            return ""
        delta_f = delta * 9 / 5
        sign = "+" if delta_f > 0 else ""
        fill_cls = "cond-dis-fill cond-dis-hot" if delta >= 5 else "cond-dis-fill"
        return (
            f'<div class="cond-team">'
            f'<span class="cond-tname">{_esc(team)}</span>'
            f'<div class="cond-dis-track" aria-label="Heat disadvantage {dis}/100">'
            f'<div class="{fill_cls}" style="width:{dis}%"></div></div>'
            f'<span class="cond-delta">{sign}{delta_f:.1f}°F vs home</span>'
            f'</div>'
        )

    teams_html = "".join(filter(None, [
        team_row(team_a, info.get("dis_a", 0) or 0, info.get("delta_a")),
        team_row(team_b, info.get("dis_b", 0) or 0, info.get("delta_b")),
    ]))

    return (
        f'<div class="conditions">\n'
        f'  <span class="cond-stamp">{stamp}</span>\n'
        f'  <p class="cond-stats">{temp}°F · {hum} ({rh}% RH) · WBGT {wbgt}°F</p>\n'
        f'  <div class="cond-track" aria-label="Match heat index: {severity} ({mhi}/100)">'
        f'<div class="cond-fill" style="width:{mhi}%"></div></div>\n'
        f'  <p class="cond-sev cond-sev-{_esc(sev_cls)}">{severity}</p>\n'
        f'  <p class="cond-blurb">{blurb}</p>\n'
        + (f'  <div class="cond-teams">{teams_html}</div>\n' if teams_html else "")
        + '</div>'
    )


def _js_str(s: str) -> str:
    """Escape a string for embedding in a single-quoted JS string literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


# JS template for the Poisson score matrix. __LA__ / __LB__ are replaced with
# per-match xG floats; __TEAM_A__ / __TEAM_B__ with JS-escaped team names.
_OUTCOME_GRID_JS = """\
(function(){
  var la=__LA__,lb=__LB__,N=8,SHOW=6,i,j;
  function pois(k,l){var f=1;for(var ii=2;ii<=k;ii++)f*=ii;return Math.exp(-l)*Math.pow(l,k)/f;}
  var pa=[],pb=[];
  for(i=0;i<=N;i++){pa.push(pois(i,la));pb.push(pois(i,lb));}
  var M=[],z=0;
  for(i=0;i<=N;i++){M[i]=[];for(j=0;j<=N;j++){M[i][j]=pa[i]*pb[j];z+=M[i][j];}}
  for(i=0;i<=N;i++)for(j=0;j<=N;j++)M[i][j]/=z;
  var maxp=0,mi=0,mj=0;
  for(i=0;i<=N;i++)for(j=0;j<=N;j++){if(M[i][j]>maxp){maxp=M[i][j];mi=i;mj=j;}}
  function region(i,j,q){
    if(q==='awin')return i>j; if(q==='draw')return i===j; if(q==='bwin')return i<j;
    if(q==='over')return(i+j)>2.5; if(q==='btts')return i>=1&&j>=1; return true;
  }
  function rColor(i,j){return i>j?'98,185,126':i===j?'217,169,72':'232,118,90';}
  var mx=document.getElementById('og-matrix');
  var html='<div class="og-corner">goals</div>';
  for(j=0;j<SHOW;j++)html+='<div class="og-ax-top">'+j+'</div>';
  for(i=0;i<SHOW;i++){
    html+='<div class="og-ax-side">'+i+'</div>';
    for(j=0;j<SHOW;j++){
      var p=M[i][j]*100;
      var alpha=Math.pow(M[i][j]/maxp,0.55)*0.92+0.05;
      var cls='og-cell'+(i===mi&&j===mj?' og-modal':'');
      html+='<div class="'+cls+'" data-i="'+i+'" data-j="'+j+'" '
        +'style="background:rgba('+rColor(i,j)+','+alpha.toFixed(3)+')" '
        +'aria-label="'+i+'-'+j+', '+p.toFixed(1)+'%">'
        +'<span>'+(p>=0.5?Math.round(p):'·')+'</span></div>';
    }
  }
  mx.innerHTML=html;
  var cells=[].slice.call(mx.querySelectorAll('.og-cell'));
  var tail=0;
  for(i=0;i<=N;i++)for(j=0;j<=N;j++){if(i>=SHOW||j>=SHOW)tail+=M[i][j];}
  var LABELS={
    awin:['__TEAM_A__ win','98,185,126'],
    draw:['Draw','217,169,72'],
    bwin:['__TEAM_B__ win','232,118,90'],
    over:['Over 2.5 goals','236,229,210'],
    btts:['Both teams score','236,229,210']
  };
  var readout=document.getElementById('og-readout'),active=null;
  function apply(q){
    if(!q){cells.forEach(function(c){c.classList.remove('og-dim','og-hot');});readout.innerHTML='';return;}
    cells.forEach(function(c){
      var hit=region(+c.dataset.i,+c.dataset.j,q);
      c.classList.toggle('og-dim',!hit);c.classList.toggle('og-hot',hit);
    });
    var sum=0;
    for(i=0;i<=N;i++)for(j=0;j<=N;j++){if(region(i,j,q))sum+=M[i][j];}
    var L=LABELS[q];
    readout.innerHTML='<b>'+L[0]+'</b> = <span class="og-big">'+Math.round(sum*100)+'%</span>';
  }
  document.getElementById('og-qbar').addEventListener('click',function(e){
    var btn=e.target.closest('.og-q');if(!btn)return;
    var q=btn.dataset.q,on=(active!==q);
    [].forEach.call(this.querySelectorAll('.og-q'),function(b){b.setAttribute('aria-pressed','false');});
    if(on){btn.setAttribute('aria-pressed','true');active=q;apply(q);}
    else{active=null;apply(null);}
  });
})();"""


def render_outcome_grid(info: dict | None, team_a: str, team_b: str) -> str:
    """Collapsible Poisson score-matrix under The Market section.
    Only renders for scheduled matches where the live predictor provides lambda
    values; returns '' otherwise — no placeholder, purely additive."""
    la = (info or {}).get("lambda_a")
    lb = (info or {}).get("lambda_b")
    if la is None or lb is None:
        return ""
    a_esc = _esc(team_a)
    b_esc = _esc(team_b)
    js = (_OUTCOME_GRID_JS
          .replace("__LA__", f"{la:.4f}")
          .replace("__LB__", f"{lb:.4f}")
          .replace("__TEAM_A__", _js_str(team_a))
          .replace("__TEAM_B__", _js_str(team_b)))
    return (
        f'<details class="outcome-grid-wrap">\n'
        f'<summary class="outcome-grid-toggle">Show score grid</summary>\n'
        f'<div class="outcome-grid-inner">\n'
        f'<div class="og-qbar" id="og-qbar">\n'
        f'  <button class="og-q awin" data-q="awin" aria-pressed="false">{a_esc} win</button>\n'
        f'  <button class="og-q draw" data-q="draw" aria-pressed="false">Draw</button>\n'
        f'  <button class="og-q bwin" data-q="bwin" aria-pressed="false">{b_esc} win</button>\n'
        f'  <button class="og-q over" data-q="over" aria-pressed="false">Over 2.5</button>\n'
        f'  <button class="og-q btts" data-q="btts" aria-pressed="false">Both score</button>\n'
        f'</div>\n'
        f'<div class="og-readout" id="og-readout"></div>\n'
        f'<div class="og-outer">\n'
        f'  <div class="og-team-side">{a_esc}</div>\n'
        f'  <div class="og-inner">\n'
        f'    <div class="og-team-top">{b_esc}</div>\n'
        f'    <div class="og-matrix" id="og-matrix"></div>\n'
        f'  </div>\n'
        f'</div>\n'
        f'</div>\n'
        f'</details>\n'
        f'<script>\n{js}\n</script>'
    )


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


def _american_odds(decimal: float) -> str:
    """Decimal -> American moneyline ('+150' / '-200'). Local mirror of
    odds.american_odds — the HTML layer keeps its own formatters (like
    _sel_label / _fmt_snapshot_ts) and a test asserts the two stay in lockstep."""
    if decimal <= 1.0:
        return "n/a"
    if decimal >= 2.0:
        return f"+{round((decimal - 1) * 100)}"
    return f"-{round(100 / (decimal - 1))}"


# The 2026-06-20 audit found the model systematically under-prices draws (mean p_draw
# ≈19% vs ≈36% realised group-stage), and that draw mass is parked on the favourite — so
# a win-side 1X2 (moneyline) edge is inflated and least trustworthy. We DISCLOSE this on
# every such pick rather than hand-pick a stricter edge bar (the draw magnitude is small-
# sample and is deferred to the post-MD3 re-grade; see DECISIONS.md / model-audit memo).
_DRAW_AUDIT_CAUTION = (
    "the model under-prices draws (June-20 audit: p_draw ≈19% vs ≈36% realised), which "
    "inflates favourite-side 1X2 (moneyline) edges — treat win-side moneyline picks with "
    "extra caution until the draw calibration is re-checked after MD3")


def _h2h_win_side(market: str, selection: str) -> bool:
    """A 1X2 pick backing a team to WIN — the selection whose edge the draw under-pricing
    inflates. (A draw pick has the opposite, conservative bias, so it isn't flagged.)"""
    return market == "h2h" and selection in ("home", "away")


def render_market(odds_info: dict | None, team_a: str, team_b: str,
                  prebaked: str | None, played: bool = False) -> str:
    """Odds & Best Bet block: the de-vigged edge table + the pick when the
    odds engine has a snapshot; the contract placeholder otherwise. Numbers
    come exclusively from odds.py — this function only formats."""
    if odds_info is None:
        return render_odds(prebaked, played=played)

    ev = odds_info["evaluation"]
    picks = odds_info.get("picks")
    if picks is None:                       # legacy single-pick callers
        picks = [odds_info["pick"]] if odds_info.get("pick") else []
    threshold = odds_info["threshold"]
    record_threshold = odds_info.get("record_threshold", 0.05)
    parts = []

    proj = odds_info.get("projection")
    if proj:
        ladder = " · ".join(f"over {line} <b>{p:.0%}</b>"
                            for line, p in sorted(proj["over"].items(),
                                                  key=lambda kv: float(kv[0])))
        parts.append(f'<p class="odds-note">model projection: '
                     f'<b>{proj["total"]:.2f}</b> total goals · {ladder}</p>')

    rows_html = []
    for market in ("h2h", "totals", "spreads", "btts"):
        for sel, line, odds_v, implied, our_p, edge in ev.get(market, []):
            is_pick = any(pk["market"] == market and pk["selection"] == sel
                          and str(pk["line"]) == str(line) for pk in picks)
            cls = ' class="pick-row"' if is_pick else ""
            edge_cls = "edge-pos" if edge >= threshold else ("edge-neg" if edge < 0 else "")
            rows_html.append(
                f'      <tr{cls}><td class="lbl">{_esc(_MARKET_LABELS[market])}</td>'
                f'<td class="lbl">{_esc(_sel_label(market, sel, line, team_a, team_b))}</td>'
                f'<td title="{odds_v:.2f} decimal">{_american_odds(odds_v)}</td>'
                f'<td>{implied:.0%}</td><td>{our_p:.0%}</td>'
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

    # A best bet whose line is too stale to record (DraftKings often posts the
    # moneyline early but totals/spreads late) is demoted from "Best bet" to a
    # clearly-labelled stale lean — so the card never recommends a bet the ledger
    # won't record. An intra-day run promotes it once the price refreshes.
    stale_markets = odds_info.get("stale_markets", set())
    max_age = odds_info.get("max_age_h", 12)
    fresh = [pk for pk in (picks or []) if pk["market"] not in stale_markets]
    stale = [pk for pk in (picks or []) if pk["market"] in stale_markets]

    if fresh:
        pick_lines = []
        for i, pk in enumerate(fresh, 1):
            bp = odds_info["best_prices"].get(
                (pk["market"], pk["selection"], str(pk["line"])))
            price = f' — best price {_american_odds(bp[0])} ({_esc(bp[1])})' if bp else ""
            num = f'{i}. ' if len(fresh) > 1 else ""
            pick_lines.append(
                f'<p>{num}<strong>{_esc(_sel_label(pk["market"], pk["selection"], pk["line"], team_a, team_b))}'
                f'</strong> ({_MARKET_LABELS[pk["market"]]}) @ {_american_odds(pk["odds"])}, '
                f'edge <strong>{pk["edge"]:+.1%}</strong>{price}. Flat 1u, paper record.</p>')
        corr = ('<p class="odds-note">same-match picks are correlated — they tend '
                'to win and lose together; the units record swings accordingly.</p>'
                if len(fresh) > 1 else "")
        draw_caut = (f'<p class="odds-note warn">⚠ {_DRAW_AUDIT_CAUTION}.</p>'
                     if any(_h2h_win_side(pk["market"], pk["selection"]) for pk in fresh) else "")
        tag = "Best bet" if len(fresh) == 1 else f"Best bets — top {len(fresh)}, ≥{record_threshold:.0%} edge"
        parts.append(f'<div class="bet-callout"><span class="tag">{_esc(tag)}</span>'
                     + "".join(pick_lines) + corr + draw_caut + '</div>')
    elif rows_html and not stale:
        parts.append(f'<p class="no-bet"><b>NO BET</b> — no edge clears the '
                     f'{record_threshold:.0%} recording bar (a normal, expected '
                     'result).</p>')

    if stale:
        lean_lines = "".join(
            f'<p><strong>{_esc(_sel_label(pk["market"], pk["selection"], pk["line"], team_a, team_b))}'
            f'</strong> ({_MARKET_LABELS[pk["market"]]}), model edge <strong>{pk["edge"]:+.1%}</strong></p>'
            for pk in stale)
        these = "these" if len(stale) > 1 else "this"
        parts.append(
            '<div class="bet-callout stale"><span class="tag">Model lean · stale line</span>'
            + lean_lines
            + f'<p class="odds-note">the model likes {these}, but the line is over '
              f'{max_age:g}h old (the book hasn\'t reposted), so {"they are" if len(stale) > 1 else "it is"} '
              '<strong>not recorded</strong> until the price refreshes — an intra-day run logs '
              'it once it does.</p></div>')

    for rec in odds_info.get("recorded", []):
        status = rec.get("status", "open")
        try:
            rec_odds = _american_odds(float(rec["odds"]))
        except (TypeError, ValueError, KeyError):   # never break the build on a bad log row
            rec_odds = _esc(str(rec.get("odds", "")))
        line_bits = [f'Logged pick: {_esc(_sel_label(rec["market"], rec["selection"], rec["line"], team_a, team_b))} '
                     f'@ {rec_odds} ({_esc(rec["book"])}), edge {_esc(rec["edge_pp"])}pp']
        if status != "open":
            line_bits.append(f'settled <b>{_esc(status)}</b> for {_esc(rec["units"])}u')
            if rec.get("clv_pp"):
                line_bits.append(f'CLV {_esc(rec["clv_pp"])}pp')
        else:
            line_bits.append("open")
        parts.append(f'<p class="odds-note">{" · ".join(line_bits)}.</p>')

    for fl in odds_info.get("flags", []):
        parts.append(f'<p class="verify-flag">{_esc(fl)}</p>')

    src = odds_info.get("source_label") or "market snapshot"
    notes = [f'market snapshot {_esc(_fmt_snapshot_ts(odds_info["snapshot_ts"]))} · '
             f'{src}, de-vigged multiplicatively']
    if ev.get("totals") or ev.get("spreads") or ev.get("btts"):
        ms = odds_info.get("model_sanity", 0.08)
        sn = odds_info.get("sanity", 0.15)
        notes.append("totals / handicap / BTTS are model-priced from the score "
                     "matrix — the Opta overlay covers W/D/L only, and with no "
                     f"independent consensus check they clear a stricter {ms:.0%} "
                     f"edge ceiling vs {sn:.0%} for 1X2")
    notes.extend(ev.get("missing", []))
    parts.append('<p class="odds-note">' + " · ".join(_esc(n) for n in notes) + ".</p>")

    if prebaked:
        parts.append('<div class="prose"><blockquote><p><strong>Markets to watch '
                     f'(pre-baked):</strong> {sc._inline(prebaked)}</p></blockquote></div>')
    return "\n".join(parts)


# ---------------------------------------------------------------- the wire

NEWS_DIR = REPO_ROOT / "news"
_WIRE_SECTION_RE = re.compile(r"^### ([A-L][1-6]):", re.MULTILINE)
# Exclude quotes and angle brackets so a URL token can never carry an
# attribute-breakout char into the href below. _linkify runs on ALREADY-escaped
# text (md_to_html, quote=False), so a literal " in an unverified news URL would
# otherwise close the href and inject a live event handler (stored XSS). We stop
# the match at the quote rather than re-escaping, which would double-escape the
# &amp; already produced for query-string ampersands.
_URL_RE = re.compile(r"""https?://[^\s<>"')\]]+""")


def load_wire(news_dir: Path = NEWS_DIR) -> dict[str, tuple[str, str]]:
    """{match_id: (digest_date, section_markdown)} from news/*.md — the latest
    digest mentioning a match wins. The repo-facing UNVERIFIED banner is
    stripped; the site applies its own wire-copy framing."""
    out: dict[str, tuple[str, str]] = {}
    if not news_dir.exists():
        return out
    for f in sorted(news_dir.glob("*.md")):   # ascending date: later files win
        text = f.read_text(encoding="utf-8")
        marks = list(_WIRE_SECTION_RE.finditer(text))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.start():end]
            # drop the "### A1: Team vs Team" heading line itself
            body = body.split("\n", 1)[1] if "\n" in body else ""
            out[m.group(1)] = (f.stem, body.strip())
    return out


def _linkify(escaped: str) -> str:
    """Make source URLs clickable in already-escaped text."""
    return _URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}" rel="nofollow">{m.group(0)}</a>',
        escaped)


def render_wire(entry: tuple[str, str] | None) -> str:
    """The Wire: auto-gathered, source-attributed news — relayed as reporting,
    never asserted in the hub's voice. Whole section omitted when no digest
    mentions the match."""
    if not entry:
        return ""
    digest_date, body = entry
    body_html = _linkify(sc.md_to_html(body))
    return (
        '<section aria-labelledby="wire-h">\n'
        '  <div class="sec-head">\n'
        '    <span class="kicker">The Wire</span>\n'
        '    <h2 id="wire-h">What\'s being reported</h2>\n'
        '  </div>\n'
        f'  <div class="wire">\n'
        f'    <p class="wire-tag">Auto-gathered {_esc(digest_date)} · every claim '
        'carries its source · relayed as reporting, not verified by the hub — '
        'click through before it bears weight.</p>\n'
        f'{body_html}\n'
        '  </div>\n'
        '</section>')


# ---------------------------------------------------------------- fates

FATE_SR = {"through": " — qualified for the Round of 32",
           "out": " — eliminated"}


def compute_fates(matches: "list[st.Match]", warnings: list[str]
                  ) -> tuple[dict[str, str], dict[str, "scen.ScenarioReport"]]:
    """Conservative within-group fates from the scenario enumerator:
    'through' only when a team finishes top-2 in EVERY outcome combination
    (margin-independent), 'out' only when it finishes 4th in every combination
    — a 3rd place is never marked, since the best-thirds cutline is cross-group
    math this deliberately does not guess. Also returns the per-group scenario
    report for groups in the canonical 2-games-left MD3 state."""
    fates: dict[str, str] = {}
    md3_reports: dict[str, "scen.ScenarioReport"] = {}
    for group in sorted({m.group for m in matches}):
        try:
            report = scen.enumerate_scenarios(group, matches)
        except Exception as e:
            warnings.append(f"group {group}: scenario enumeration failed "
                            f"({e.__class__.__name__}: {e}) — no fate marks")
            continue
        for ts in report.teams:
            if ts.counts["top2"] == report.n_combos:
                fates[ts.team] = "through"
            elif ts.counts["out"] == report.n_combos:
                fates[ts.team] = "out"
        if len(report.unplayed) == 2:
            md3_reports[group] = report
    return fates, md3_reports


def render_scenario_block(report: "scen.ScenarioReport",
                          team_a: str, team_b: str) -> str:
    """The MD3 scenario block for one match page: finish distribution across
    all outcome combinations plus the Win/Draw/Loss prospects for both teams.
    Numbers come exclusively from scenarios.py — this only formats."""
    ta = next((t for t in report.teams if t.team == team_a), None)
    tb = next((t for t in report.teams if t.team == team_b), None)
    if ta is None or tb is None:
        return ""
    rows = []
    for ts in (ta, tb):
        c = ts.counts
        rows.append(f'      <tr><td class="lbl">{_esc(ts.team)}</td>'
                    f'<td>{c["top2"]}</td><td>{c["third"]}</td>'
                    f'<td>{c["out"]}</td><td>{c["margin"]}</td></tr>')
    parts = [
        '<div class="scenario">',
        f'  <p class="scenario-intro">Final matchday — both Group {_esc(report.group)} '
        f'games kick off simultaneously: {report.n_combos} possible outcomes. '
        'Anything that comes down to goal difference is flagged margin-dependent, '
        'never guessed.</p>',
        '  <table>',
        '    <caption class="sr-only">Finish distribution across all outcome '
        'combinations</caption>',
        '    <thead><tr><th class="lbl" scope="col">Team</th>'
        '<th scope="col">Top 2</th><th scope="col">3rd</th>'
        '<th scope="col">Out</th><th scope="col">Margin</th></tr></thead>',
        '    <tbody>\n' + "\n".join(rows) + '\n    </tbody>',
        '  </table>',
    ]
    for ts in (ta, tb):
        if ts.stakes:
            items = "".join(f"<li>{sc._inline(s)}</li>" for s in ts.stakes)
            parts.append(f'  <p class="scenario-team">{_esc(ts.team)}</p>'
                         f'  <ul class="scenario-stakes">{items}</ul>')
    parts.append('</div>')
    return "\n".join(parts)


# ---------------------------------------------------------------- the record

def _hit_chip(correct: bool) -> str:
    if correct:
        return ('<span class="hit hit-y" aria-hidden="true">✓</span>'
                '<span class="sr-only">correct call</span>')
    return ('<span class="hit hit-n" aria-hidden="true">✗</span>'
            '<span class="sr-only">missed</span>')


def render_record_calls(matches: "list[st.Match]", rows: list[dict],
                        ledger: dict | None, root: str = "") -> tuple[str, str]:
    """(calls_table_html, cumulative_line_text) for the record page."""
    if ledger is None:
        return "", "Prediction ledger unavailable."
    grades = ledger["grade"](matches, ledger["rows"])
    cumulative = ledger["cumulative"](matches, ledger["rows"]) \
        or ("No graded calls yet — grades land when a logged match gets its "
            "result entered.")
    if not grades:
        return "", cumulative
    by_mid = {m.match_id: m for m in matches}
    editorial = {r["match_id"]: r.get("_editorial") for r in rows}
    ordered = sorted(grades, key=lambda mid: (editorial.get(mid) or date.min, mid))

    body, day, day_acc = [], None, []

    def flush_day():
        if day is None or not day_acc:
            return
        n = len(day_acc)
        hits = sum(1 for g in day_acc if g["correct"])
        mean_b = sum(g["brier"] for g in day_acc) / n
        body.append(
            f'      <tr class="subtotal"><td colspan="4">{day:%B} {day.day} — '
            f'{n} graded</td><td>{hits}/{n}</td><td>{mean_b:.3f}</td></tr>')

    for mid in ordered:
        g = grades[mid]
        m = by_mid[mid]
        ed = editorial.get(mid)
        if ed != day:
            flush_day()
            day, day_acc = ed, []
        day_acc.append(g)
        p = g["p"]
        pred = f' ({_esc(g["predicted_score"])})' if g["predicted_score"] else ""
        body.append(
            f'      <tr>\n'
            f'        <td class="lbl"><a href="{root}matches/{_esc(mid)}.html">'
            f'{_esc(m.team_a)} v {_esc(m.team_b)}</a></td>\n'
            f'        <td>{p[0]:.0%}/{p[1]:.0%}/{p[2]:.0%}{pred}</td>\n'
            f'        <td>{m.score_a}–{m.score_b}</td>\n'
            f'        <td>{("home", "draw", "away")[g["outcome"]]}</td>\n'
            f'        <td>{_hit_chip(g["correct"])}</td>\n'
            f'        <td class="pts">{g["brier"]:.3f}</td>\n'
            f'      </tr>')
    flush_day()
    table = (
        '<div class="record-wrap">\n  <table>\n'
        '    <caption class="sr-only">Every logged call graded: probabilities, '
        'result, hit or miss, Brier score</caption>\n'
        '    <thead><tr><th class="lbl" scope="col">Match</th>'
        '<th scope="col">Logged H/D/A</th><th scope="col">Final</th>'
        '<th scope="col">Outcome</th><th scope="col">Hit</th>'
        '<th scope="col">Brier</th></tr></thead>\n'
        '    <tbody>\n' + "\n".join(body) + "\n    </tbody>\n  </table>\n</div>")
    return table, cumulative


def render_record_bets(rows: list[dict], root: str = "",
                       picks_log: Path | None = None) -> tuple[str, str]:
    """(bets_table_html, units_line_text) for the record page."""
    try:
        import odds as od
        picks = od.load_picks(picks_log) if picks_log else od.load_picks()
        units = od.units_summary(picks)
    except Exception:
        return ('<p class="standfirst">Picks ledger unavailable.</p>',
                "Picks ledger unavailable.")
    if not picks:
        return ('<p class="standfirst">No picks recorded yet.</p>',
                "No picks recorded yet — the first qualifying edge gets logged "
                "on the morning run.")
    teams = {r["match_id"]: (r["team_a"], r["team_b"]) for r in rows}
    editorial = {r["match_id"]: r.get("_editorial") for r in rows}
    open_picks = [p for p in picks if p.get("status") == "open"]
    units_line = units or "No picks settled yet."
    if open_picks:
        units_line += (f" {len(open_picks)} open pick"
                       f"{'s' if len(open_picks) != 1 else ''}, "
                       f"{len(open_picks)}u at risk.")
    body = []
    for p in sorted(picks, key=lambda p: (editorial.get(p["match_id"]) or date.min,
                                          p["match_id"])):
        mid = p["match_id"]
        a, b = teams.get(mid, (mid, ""))
        ed = editorial.get(mid)
        when = f"{ed:%b} {ed.day}" if ed else ""
        status = p.get("status") or "open"
        units_cell = p.get("units") or ("—" if status == "open" else "")
        clv_cell = f'{p["clv_pp"]}pp' if p.get("clv_pp") else "—"
        try:
            odds_cell = f'<td title="{float(p["odds"]):.2f} decimal">{_american_odds(float(p["odds"]))}</td>'
        except (ValueError, KeyError):
            odds_cell = f'<td>{_esc(str(p.get("odds", "")))}</td>'
        caut = ' <abbr class="caut" title="moneyline edge inflated by draw under-pricing — see note">‡</abbr>' \
            if _h2h_win_side(p["market"], p["selection"]) else ""
        body.append(
            f'      <tr>\n'
            f'        <td class="lbl">{_esc(when)}</td>\n'
            f'        <td class="lbl"><a href="{root}matches/{_esc(mid)}.html">'
            f'{_esc(a)} v {_esc(b)}</a></td>\n'
            f'        <td class="lbl">{_esc(_sel_label(p["market"], p["selection"], p["line"], a, b))}{caut}</td>\n'
            f'        {odds_cell}\n'
            f'        <td class="lbl">{_esc(p.get("book") or "")}</td>\n'
            f'        <td>{_esc(p.get("edge_pp") or "")}pp</td>\n'
            f'        <td class="lbl status-{_esc(status)}">{_esc(status)}</td>\n'
            f'        <td class="pts">{_esc(units_cell)}</td>\n'
            f'        <td>{_esc(clv_cell)}</td>\n'
            f'      </tr>')
    table = (
        '<div class="record-wrap">\n  <table>\n'
        '    <caption class="sr-only">Every recorded pick: selection, price, '
        'edge at record, settlement, units, closing line value</caption>\n'
        '    <thead><tr><th class="lbl" scope="col">Day</th>'
        '<th class="lbl" scope="col">Match</th><th class="lbl" scope="col">Pick</th>'
        '<th scope="col">Odds</th><th class="lbl" scope="col">Book</th>'
        '<th scope="col">Edge</th><th class="lbl" scope="col">Status</th>'
        '<th scope="col">Units</th><th scope="col">CLV</th></tr></thead>\n'
        '    <tbody>\n' + "\n".join(body) + "\n    </tbody>\n  </table>\n</div>")
    if any(_h2h_win_side(p["market"], p["selection"]) for p in picks):
        table += (f'\n<p class="odds-note warn">‡ {_DRAW_AUDIT_CAUTION}.</p>')
    return table, units_line


def render_record_shadow(rows: list[dict], root: str = "",
                         shadow_log: Path | None = None) -> tuple[str, str]:
    """(shadow_table_html, summary_line) for the record page's Shadow Book — the RISKY
    calls where the model and market severely disagree (edges above the sanity ceiling).
    Tracked but too risky to stake; walled off from the units/CLV record; the units
    shown are HYPOTHETICAL flat-1u, on paper, never staked."""
    try:
        import odds as od
        picks = od.load_shadow_picks(shadow_log) if shadow_log else od.load_shadow_picks()
        summary = od.shadow_summary(picks)
    except Exception:
        return "", "Shadow book unavailable."
    if not picks:
        return ('<p class="standfirst">Nothing in the shadow book yet — it fills as the '
                'model flags edges above the sanity ceiling (the risky calls where model '
                'and market severely disagree).</p>',
                "Nothing tracked yet — the first risky call lands here.")
    teams = {r["match_id"]: (r["team_a"], r["team_b"]) for r in rows}
    editorial = {r["match_id"]: r.get("_editorial") for r in rows}
    open_n = sum(1 for p in picks if p.get("status") == "open")
    summary_line = (summary or "Nothing settled yet.") + (
        f" {open_n} awaiting result." if open_n else "")
    body = []
    for p in sorted(picks, key=lambda p: (editorial.get(p["match_id"]) or date.min,
                                          p["match_id"])):
        mid = p["match_id"]
        a, b = teams.get(mid, (mid, ""))
        ed = editorial.get(mid)
        when = f"{ed:%b} {ed.day}" if ed else ""
        status = p.get("status") or "open"
        units_cell = p.get("units") or ("—" if status == "open" else "")
        try:
            odds_cell = _american_odds(float(p["odds"]))
            modp = f'{float(p["our_p"]) * 100:.0f}%'
        except (ValueError, KeyError):
            odds_cell, modp = _esc(str(p.get("odds", ""))), "—"
        caut = ' <abbr class="caut" title="moneyline edge inflated by draw under-pricing — see note">‡</abbr>' \
            if _h2h_win_side(p["market"], p["selection"]) else ""
        body.append(
            f'      <tr>\n'
            f'        <td class="lbl">{_esc(when)}</td>\n'
            f'        <td class="lbl"><a href="{root}matches/{_esc(mid)}.html">'
            f'{_esc(a)} v {_esc(b)}</a></td>\n'
            f'        <td class="lbl">{_esc(_sel_label(p["market"], p["selection"], p["line"], a, b))}{caut}</td>\n'
            f'        <td>{odds_cell}</td>\n'
            f'        <td>{_esc(p.get("edge_pp") or "")}pp</td>\n'
            f'        <td>{modp}</td>\n'
            f'        <td class="lbl status-{_esc(status)}">{_esc(status)}</td>\n'
            f'        <td class="pts">{_esc(units_cell)}</td>\n'
            f'      </tr>')
    table = (
        '<div class="record-wrap">\n  <table>\n'
        '    <caption class="sr-only">The shadow book: risky calls above the sanity '
        'ceiling (severe model–market disagreement), tracked on paper but too risky to '
        'stake, with hypothetical flat-1u units</caption>\n'
        '    <thead><tr><th class="lbl" scope="col">Day</th>'
        '<th class="lbl" scope="col">Match</th><th class="lbl" scope="col">Conviction</th>'
        '<th scope="col">Odds</th><th scope="col">Edge</th>'
        '<th scope="col">Model</th><th class="lbl" scope="col">Result</th>'
        '<th scope="col">Units*</th></tr></thead>\n'
        '    <tbody>\n' + "\n".join(body) + "\n    </tbody>\n  </table>\n</div>\n"
        '<p class="odds-note">*hypothetical flat-1u, on paper — these are risky calls '
        '(severe model–market gap), tracked to test the model, not staked.</p>')
    if any(_h2h_win_side(p["market"], p["selection"]) for p in picks):
        table += (f'\n<p class="odds-note warn">‡ {_DRAW_AUDIT_CAUTION}.</p>')
    return table, summary_line


_ROUND_TITLES = ("Round of 32", "Round of 16", "Quarter-finals",
                 "Semi-finals", "Final")


def _bracket_ordered_rounds() -> list[list[int]]:
    """Match numbers per round in *bracket* (in-order traversal) order, so a column
    laid out top-to-bottom places each match exactly between the two feeders below
    it. Derived from bracket.BRACKET_TREE — the winner-of wiring is the single
    source of truth for who-feeds-whom; this just reads the tree's leaf order."""
    parent: dict[int, int] = {}
    for m, (a, b) in bk.BRACKET_TREE.items():
        parent[a] = m
        parent[b] = m
    root = max(bk.BRACKET_TREE)            # the Final
    leaves: list[int] = []

    def walk(m: int) -> None:
        kids = bk.BRACKET_TREE.get(m)
        if kids:
            walk(kids[0])
            walk(kids[1])
        else:
            leaves.append(m)               # an R32 match

    walk(root)
    rounds = [leaves]
    while len(rounds[-1]) > 1:
        prev = rounds[-1]
        rounds.append([parent[prev[i]] for i in range(0, len(prev), 2)])
    return rounds                          # [R32(16), R16(8), QF(4), SF(2), Final(1)]


def _humanize_origin(label: str) -> str:
    """Turn a slot's terse origin label into prose for the hover tooltip, so a reader can
    see HOW a team landed where it did: 'Winner E' -> 'Group E winner', 'Runner-up B' ->
    'Group B runner-up', '3rd C' -> '3rd place, Group C', 'Best 3rd of A/B/C' -> 'one of
    the best third-placed teams (Group A/B/C)'. Unknown shapes pass through unchanged."""
    if not label:
        return ""
    if label.startswith("Winner "):
        return f"Group {label[7:]} winner"
    if label.startswith("Runner-up "):
        return f"Group {label[10:]} runner-up"
    if label.startswith("3rd ") and " of " not in label:
        return f"3rd place, Group {label[4:]}"
    if label.startswith("Best 3rd of "):
        return f"one of the best third-placed teams (Group {label[12:]})"
    return label


def render_bracket_html(proj: dict, root: str = "") -> str:
    """The as-it-stands knockout bracket as a traditional left-to-right cascade:
    R32 cards on the left, each later round's matches centred between their two
    feeders, connector lines implying where a winner advances (so no match numbers
    are needed). R32 slots resolve to linked team names where the group has played
    and abstract 'Winner E' / 'Best 3rd of …' labels where gated. If bracket.feed
    has run (``winners``/``participants`` present), downstream slots fill with the
    model's projected advancer — green, with its chance to advance — and the loser
    reads muted; otherwise downstream slots stay blank fill-in lines. The cascade
    only extends as far as both sides of a tie are concretely known.
    Presentation only; the projection + propagation are computed in bracket.py."""
    r32 = {int(k): v for k, v in proj["r32"].items()}
    rounds = _bracket_ordered_rounds()
    winners = {int(k): v for k, v in proj.get("winners", {}).items()}
    parts = {int(k): tuple(v) for k, v in proj.get("participants", {}).items()}

    def slot(team=None, label=None, *, win=False, p=None, prov=False, conf=False):
        # origin = how this slot was reached (Group D winner, 3rd place Group C, …) — shown
        # on hover whether the slot is still abstract or already filled by a concrete team.
        origin = _humanize_origin(label)
        mark = ""
        if team:
            name = f'<a href="{_team_link(team, root)}">{_esc(team)}</a>'
            cls = "bslot"
            base = f"{team} — {origin}" if origin else team
            if conf:                                # this exact seed is mathematically secured
                cls += " conf"
                title = f"{base} (confirmed — this seed is mathematically secured)"
                mark = '<span class="bmark" aria-label="confirmed" role="img">✓</span>'
            elif prov:                              # concrete team, group position not sealed
                cls += " prov"
                title = f"{base} (provisional — not yet sealed, can still change)"
            else:
                title = base
        elif label:
            name, cls, title = _esc(label), "bslot tbd", origin or label
        else:
            name, cls, title = "", "bslot tbd", ""
        if win:
            cls += " win"
        pct = (f'<span class="bp">{round(p * 100)}%</span>'
               if win and p is not None else "")
        ttl = f' title="{_esc(title)}"' if title else ""
        return f'<span class="{cls}"{ttl}><span class="bn">{name}</span>{mark}{pct}</span>'

    def card(m, a_team, a_label, b_team, b_label, a_prov=False, b_prov=False,
             a_conf=False, b_conf=False):
        w = winners.get(m, {})
        wt, p = w.get("team"), w.get("p")
        sa = slot(team=a_team, label=a_label, win=a_team is not None and a_team == wt,
                  p=p, prov=a_prov, conf=a_conf)
        sb = slot(team=b_team, label=b_label, win=b_team is not None and b_team == wt,
                  p=p, prov=b_prov, conf=b_conf)
        return f'<li class="btie" id="m{m}"><div class="bpair">{sa}{sb}</div></li>'

    def downstream_card(m):
        a, b = parts.get(m, (None, None))
        return card(m, a, None, b, None)

    cols = []
    for r, nums in enumerate(rounds):
        items = []
        for m in nums:
            if r == 0:
                e = r32[m]
                items.append(card(m, e["home"], e["home_label"], e["away"], e["away_label"],
                                  e.get("home_provisional", False), e.get("away_provisional", False),
                                  e.get("home_confirmed", False), e.get("away_confirmed", False)))
            else:
                items.append(downstream_card(m))
        cols.append(f'<li class="bround" data-r="{r}"><h3>{_esc(_ROUND_TITLES[r])}</h3>'
                    f'<ul>{"".join(items)}</ul></li>')

    third = (f'<div class="bracket-third"><h3>Third-place play-off</h3>'
             f'<p class="bthird-sub">The two semi-final losers</p>'
             f'<ul>{downstream_card(proj["third_place_match"])}</ul></div>')
    return f'<ol class="bracket">{"".join(cols)}</ol>{third}'


def _bracket_view(proj: dict, view_cls: str, champ_label: str, sub: str) -> str:
    """One bracket view (champion banner + sub-note + the scrollable cascade)."""
    champ = proj.get("champion")
    cb = (f'<p class="bchamp"><span class="lbl">{champ_label}</span>'
          f'<strong>{_esc(champ)}</strong></p>' if champ else "")
    return (f'<div class="bview {view_cls}">{cb}'
            f'<p class="bview-sub">{_esc(sub)}</p>'
            f'<div class="bracket-wrap" tabindex="0" role="region" '
            f'aria-label="Knockout bracket, scrollable">{render_bracket_html(proj, root="")}'
            f'</div></div>')


def render_bracket_page(proj: dict, css: str, generated_at: str,
                        template_dir: Path = TEMPLATE_DIR,
                        projected: "dict | None" = None) -> str:
    n = proj["as_of_matches_played"]
    resolved = ("All eight group-winner-vs-best-third matches are projected from "
                "the current third-place race." if proj["thirds_resolved"] else
                "The eight group-winner-vs-best-third matches aren’t resolved yet — "
                "they fill in once all twelve groups have a standing and the "
                "third-place cutline is settled.")
    notes_html = "".join(f'<li>{_esc(w)}</li>' for w in proj["warnings"])
    notes_html = f'<ul class="bnotes">{notes_html}</ul>' if notes_html else ""

    now_view = _bracket_view(
        proj, "view-now", "Projected to lift the trophy",
        "Drawn all the way out from the standings as they stand — every group winner, runner-up "
        "and the eight best thirds (slotted by the FIFA Annex C logic). A green ✓ marks a seed "
        "already mathematically secured; a dashed underline marks a provisional position (not yet "
        "sealed — it shifts with each result); abstract slots remain only where a group hasn't "
        "kicked off. Hover any slot to see how a team reached it.")
    if projected is not None:
        proj_view = _bracket_view(
            projected, "view-proj", "Projected champion",
            "Every remaining group game played out to the model's most likely decisive result, "
            "then the whole bracket run through — a full scenario, not a forecast of who qualifies.")
        # CSS-only radio toggle (no JS in the output): the radios precede the views so the
        # `#id:checked ~ .view` sibling rules can show/hide. 'As it stands' is the default.
        bracket_html = (
            '<input type="radio" name="bview" id="bv-now" class="bview-radio" checked>'
            '<input type="radio" name="bview" id="bv-proj" class="bview-radio">'
            '<div class="bview-tabs" role="tablist" aria-label="Bracket view">'
            '<label for="bv-now">As it stands</label>'
            '<label for="bv-proj">Projected finish</label></div>'
            + now_view + proj_view)
    else:
        bracket_html = now_view

    tpl = Template((template_dir / "bracket.html").read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        site_css=css,
        bracket_html=bracket_html,
        played=n, total=72, progress_pct=round(n / 72 * 100),
        resolved_note=_esc(resolved),
        champion_html="",                  # champion banners now live inside each view
        notes_html=notes_html,
        generated_at=_esc(generated_at),
        repo_url=REPO_URL,
    )


def render_record_page(matches: "list[st.Match]", rows: list[dict],
                       ledger: dict | None, css: str, generated_at: str,
                       template_dir: Path = TEMPLATE_DIR,
                       picks_log: Path | None = None,
                       shadow_log: Path | None = None) -> str:
    calls_html, cumulative = render_record_calls(matches, rows, ledger)
    bets_html, units_line = render_record_bets(rows, picks_log=picks_log)
    shadow_html, shadow_line = render_record_shadow(rows, shadow_log=shadow_log)
    tpl = Template((template_dir / "record.html").read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        site_css=css,
        calls_html=calls_html,
        cumulative_line=_esc(cumulative),
        bets_html=bets_html,
        units_line=_esc(units_line),
        shadow_html=shadow_html,
        shadow_line=_esc(shadow_line),
        generated_at=_esc(generated_at),
        repo_url=REPO_URL,
    )


def render_overnight(rows: list[dict], target: date,
                     matches: "list[st.Match]", ledger: dict | None,
                     root: str = "") -> str:
    """Yesterday's results with their graded calls, for the index. Empty
    string when yesterday had no matches."""
    prior = be.select_matches(rows, target - timedelta(days=1))
    if not prior:
        return ""
    grades = ledger["grade"](matches, ledger["rows"]) if ledger else {}
    yesterday = target - timedelta(days=1)
    items = []
    for r in prior:
        mid = r["match_id"]
        href = f'{root}matches/{_esc(mid)}.html'
        played = (r.get("status") or "").strip().lower() == "played"
        if played:
            line = (f'<a href="{href}">{_esc(r["team_a"])} '
                    f'{_esc(str(r["score_a"]))}–{_esc(str(r["score_b"]))} '
                    f'{_esc(r["team_b"])}</a>')
            g = grades.get(mid)
            if g:
                grade_bit = (f' <span class="grade">{_hit_chip(g["correct"])} '
                             f'logged {g["p"][g["outcome"]]:.0%} on the result · '
                             f'Brier {g["brier"]:.3f}</span>')
            else:
                grade_bit = ' <span class="grade">no logged call</span>'
        else:
            line = (f'<a href="{href}">{_esc(r["team_a"])} v {_esc(r["team_b"])}'
                    '</a>')
            grade_bit = ' <span class="grade warn">⚠ result not yet entered</span>'
        items.append(f'    <li><span class="mid">{_esc(mid)}</span>{line}{grade_bit}</li>')
    return (
        '<section aria-labelledby="overnight-h">\n'
        '  <div class="sec-head">\n'
        '    <span class="kicker">The Morning After</span>\n'
        f'    <h2 id="overnight-h">Overnight — {yesterday:%A, %B} {yesterday.day}</h2>\n'
        '  </div>\n'
        '  <ul class="overnight">\n' + "\n".join(items) + '\n  </ul>\n'
        f'  <p class="overnight-more"><a href="{root}record.html">Full record — every '
        'call and pick, graded →</a></p>\n'
        '</section>')


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
                      odds_info: dict | None = None,
                      scenario_html: str = "",
                      wire_html: str = "",
                      sweat_info: dict | None = None,
                      kicked_off: bool = False,
                      grid_info: dict | None = None) -> str:
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
        call_html=render_call(info, team_a, team_b, lean, result=result,
                              kicked_off=kicked_off),
        stakes_sentence=_esc(stakes),
        scenario_html=scenario_html,
        mini_table_html=mini,
        sweat_html=render_sweat(sweat_info, team_a, team_b),
        card_html=render_card_sections(sections),
        wire_html=wire_html,
        odds_html=render_market(odds_info, team_a, team_b, odds_note, played=played),
        outcome_grid_html=render_outcome_grid(grid_info or info, team_a, team_b),
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
               slate_picks: dict[str, str] | None = None,
               overnight_html: str = "",
               fates: dict[str, str] | None = None) -> tuple[str, dict]:
    """Render the index page. Returns (html, data_dict)."""
    s = st.compute_standings(matches, fair_play=fair_play)
    forms = form_by_team(matches)
    fates = fates or {}
    today = be.select_matches(rows, target)
    day_n = (target - be.TOURNAMENT_START).days + 1

    n = len(today)
    slate_title = (f"{target:%A, %B} {target.day} · "
                   + (f"{n} match" + ("" if n == 1 else "es") if n else "rest day"))

    groups_html = "\n".join(
        render_group_card(s.groups[g], forms, i, fates=fates)
        for i, g in enumerate(sorted(s.groups)))

    data = st.to_dict(s)
    for gd in data["groups"].values():
        for row in gd["rows"]:
            row["fate"] = fates.get(row["team"])
    for row in data["third_place"]["rows"]:
        row["fate"] = fates.get(row["team"])
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
        thirds_html=render_thirds(s, forms, fates=fates),
        archive_html=_archive(editions_dir),
        generated_at=_esc(generated_at),
        repo_url=REPO_URL,
        data_json=data_json,
        ledger_html=(f'<p class="ledger-line">{_esc(ledger_line)}</p>'
                     if ledger_line else ""),
        blurb_html=blurb_html,
        overnight_html=overnight_html,
    )
    return page, data


def build_site(out_dir: Path, target: date, generated_at: str,
               fixtures: Path = REPO_ROOT / "data" / "fixtures.csv",
               cards_dir: Path = REPO_ROOT / "cards",
               kb_path: Path = KB_GUIDE,
               template_dir: Path = TEMPLATE_DIR,
               editions_dir: Path = REPO_ROOT / "editions",
               discipline: Path = DISCIPLINE,
               blurbs_dir: Path = BLURBS_DIR,
               news_dir: Path = NEWS_DIR,
               predictions_log: Path | None = None,
               picks_log: Path | None = None,
               shadow_log: Path | None = None,
               now=None,
               predictor="auto", odds_engine="auto",
               weather_engine="auto", knockout_resolver="auto") -> list[str]:
    """Render the whole site (index + team cards + match previews + data.json)
    into out_dir. Returns warnings. ``predictor`` and ``odds_engine`` are
    "auto" (load the real modules defensively), None (placeholder state), or
    a callable (tests).

    All data inputs are injectable so tests (and historical re-builds) run
    against a frozen snapshot rather than the live, evolving ``data/`` tree:
    ``predictions_log``/``picks_log`` default to the ledger/odds module paths,
    and ``now`` pins the wall clock used for kicked-off/awaiting state (default
    real time) so a pinned ``target`` actually freezes the rendered world."""
    warnings: list[str] = []

    matches = st.load_fixtures(fixtures)
    rows = be.read_rows(fixtures)
    for r in rows:  # load_fixtures validated the stripped values; use the same
        for k in ("match_id", "group", "team_a", "team_b"):
            r[k] = (r.get(k) or "").strip()
    fair_play = load_discipline(discipline)
    s = st.compute_standings(matches, fair_play=fair_play)
    warnings.extend(s.warnings)
    forms = form_by_team(matches)
    fates, md3_reports = compute_fates(matches, warnings)
    wire = load_wire(news_dir)
    css = _site_css(template_dir)

    blurb_html = ""
    blurb_path = blurbs_dir / f"{target.isoformat()}.md"
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
        odds_call, ledger_line, odds_why = load_odds_engine(now=now)
        if odds_why:
            warnings.append(f"Odds sections render as placeholder: {odds_why}")
    else:
        odds_call, ledger_line = odds_engine, None

    if weather_engine == "auto":
        wx_call, wx_why = load_weather_engine()
        if wx_why:
            warnings.append(f"Conditions sections render as placeholder: {wx_why}")
    else:
        wx_call = weather_engine

    profiles, kb_warnings = sc.parse_kb(kb_path)
    warnings.extend(f"kb: {w}" for w in kb_warnings)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "teams").mkdir(exist_ok=True)
    (out_dir / "matches").mkdir(exist_ok=True)

    slate_picks: dict[str, str] = {}
    if odds_call:
        for r in be.select_matches(rows, target):
            o = odds_call(r)
            if o and o.get("picks"):
                p = o["picks"][0]
                more = f" (+{len(o['picks']) - 1} more)" if len(o["picks"]) > 1 else ""
                slate_picks[r["match_id"]] = (
                    f"{_sel_label(p['market'], p['selection'], p['line'], r['team_a'], r['team_b'])}"
                    f" {p['edge']:+.1%}{more}")

    ledger = _load_ledger(warnings, predictions_log, now)
    # as-it-stands knockout bracket — gated projection from the live standings.
    # Annex C is committed static data; a missing/corrupt table degrades to no
    # bracket page + a warning rather than a broken build (never an invented slot).
    bracket_proj = None
    bracket_full = None
    try:
        # resolve_provisional: slot the thirds via Annex C using the standings' deterministic
        # order even when the cutline is provisional (FIFA ranking unmodelled) — so the
        # as-it-stands bracket shows the real third-place logic, honestly flagged, rather than
        # abstract "Best 3rd of …" pools. Still gated on all 12 groups having STARTED.
        # clinched: which exact seeds are mathematically secured (full 2026 tiebreakers, incl.
        # head-to-head) — drives the confirmed (✓) vs provisional (dashed) per-side marks.
        clinched = {g: scen.clinched_ranks(g, matches) for g in s.groups}
        bracket_proj = bk.project(s, resolve_provisional=True, clinched=clinched)
    except (FileNotFoundError, ValueError) as e:
        warnings.append(f"Bracket page skipped: {e}")
    if bracket_proj is not None:
        if knockout_resolver == "auto":
            knockout_resolver = load_knockout_resolver()
            if knockout_resolver is None:
                warnings.append("Bracket winners not projected: prediction model unavailable")
        if knockout_resolver:
            # results=None: no knockout has been played yet (group stage only);
            # actual results will override the model here once those fixtures exist
            bracket_proj = bk.feed(bracket_proj, knockout_resolver)
        # 'Projected finish' view: project every remaining group game to a decisive result,
        # resolve the WHOLE bracket (all 12 groups final, thirds slotted), run it through.
        projector = load_group_projector()
        if projector is not None:
            try:
                s_full = bk.project_final_standings(matches, projector)
                bracket_full = bk.project(s_full, resolve_provisional=True)
                if knockout_resolver:
                    bracket_full = bk.feed(bracket_full, knockout_resolver)
            except (FileNotFoundError, ValueError) as e:
                warnings.append(f"Projected-finish bracket skipped: {e}")

    index, data = build_page(matches, rows, target, generated_at,
                             template_path=template_dir / "page.html",
                             editions_dir=editions_dir, css=css,
                             ledger_line=ledger_line, fair_play=fair_play,
                             blurb_html=blurb_html, slate_picks=slate_picks,
                             overnight_html=render_overnight(rows, target,
                                                             matches, ledger),
                             fates=fates)
    if bracket_proj is not None:
        data["bracket"] = bk.to_dict(bracket_proj)
    if bracket_full is not None:
        data["bracket_projected"] = bk.to_dict(bracket_full)
    (out_dir / "index.html").write_text(index, encoding="utf-8")
    (out_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # daily snapshot archive: the raw material for movement arrows and any
    # future day-over-day analysis — cheap now, unrecoverable later
    (out_dir / "data").mkdir(exist_ok=True)
    (out_dir / "data" / f"{target.isoformat()}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    (out_dir / "record.html").write_text(
        render_record_page(matches, rows, ledger, css, generated_at, template_dir,
                           picks_log=picks_log, shadow_log=shadow_log),
        encoding="utf-8")
    if bracket_proj is not None:
        (out_dir / "bracket.html").write_text(
            render_bracket_page(bracket_proj, css, generated_at, template_dir,
                                projected=bracket_full),
            encoding="utf-8")

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
        sweat_info = wx_call(row) if wx_call else None
        scenario_html = ""
        if int(row["match_id"][1]) >= 5 and row["group"] in md3_reports:
            scenario_html = render_scenario_block(
                md3_reports[row["group"]], row["team_a"], row["team_b"])
        grid_info = (_safe_predict(predictor, row, [])
                     if predictor is not None and (played or kicked_off) else None)
        page = render_match_page(row, s, forms, cards_dir, info, css,
                                 template_dir, warnings, odds_info=odds_info,
                                 scenario_html=scenario_html,
                                 wire_html=render_wire(wire.get(row["match_id"])),
                                 sweat_info=sweat_info,
                                 kicked_off=kicked_off,
                                 grid_info=grid_info)
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


def _load_ledger(warnings: list[str], predictions_path=None, now=None) -> dict | None:
    """The prediction-ledger API surface this renderer relies on, loaded
    defensively: rows, the published-source constant, the canonical Brier
    function, and kickoff math. A missing/changed ledger module degrades to
    'no logged call', never a retroactive grade. ``predictions_path``/``now``
    are injectable so a build can be pinned to a frozen ledger and a fixed
    clock instead of the live file and real wall-clock time."""
    try:
        import ledger as lg
        rows = lg.load_ledger(predictions_path) if predictions_path else lg.load_ledger()
        return {"rows": rows,
                "published": getattr(lg, "PUBLISHED_SOURCE", "consensus"),
                "brier": lg.brier,
                "kickoff_dt": lg.kickoff_dt,
                "grade": lg.grade,
                "cumulative": lg.cumulative_line,
                "now": now or lg.now_et()}
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
    import ledger as lg
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
    if not lg.probs_valid(probs):   # same gate as the bet-driving consensus
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
    for tname in ("page.html", "team.html", "match.html", "bracket.html", "site.css"):
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
