#!/usr/bin/env python3
"""WC26 static site builder — renders docs/index.html (+ docs/data.json).

Presentation layer only. Ranking and tiebreak logic live in standings.py;
the 🌙 editorial-date convention lives in build_edition.py; both are imported,
never re-implemented. The output is a single self-contained HTML page (inline
CSS, zero JavaScript) that works from file:// and from GitHub Pages serving
/docs, with the machine-readable standings embedded as JSON.

Importable API (for tests):

    forms     = form_by_team(matches)              # {team: [W|D|L|None, ...]}
    html_page, data = build_page(matches, rows, target_date, generated_at)

CLI:
    python scripts/build_site.py [--date 2026-06-12] [--fixtures ...]
                                 [--out-dir docs] [--template ...]
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

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / "templates" / "page.html"
REPO_URL = "https://github.com/alxgriff/wc26-hub"
DAGGERS = ["†", "‡", "§", "¶"]
GAMES_PER_TEAM = st.GAMES_PER_TEAM


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
    """Strip the engine's context prefix (redundant inside the table card) and
    the ⚠️ glyph (replaced by styling)."""
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


def _team_cell(r: "st.TeamRow", syms: dict[str, str]) -> str:
    dag = syms.get(r.team, "")
    dag_html = f'<span class="dag">{dag}</span>' if dag else ""
    return (f'<th class="team" scope="row" title="{_esc(r.team)}">'
            f'{_esc(r.team)}{dag_html}</th>')


def render_group_card(gt: "st.GroupTable", forms: dict[str, list[str | None]],
                      index: int) -> str:
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
            f'        {_team_cell(r, syms)}\n'
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


def render_thirds(s: "st.Standings", forms: dict[str, list[str | None]]) -> str:
    rows = s.third_place
    if not rows:
        return '<p class="standfirst">No third-place table yet — no completed group rows.</p>'
    syms, sym_notes = _note_daggers(rows, s.third_place_notes)
    body = []
    for pos, r in enumerate(rows, 1):
        qualifying = pos <= st.QUALIFYING_THIRDS
        cls = "q" if qualifying in (True,) else "below"
        in_cell = ('<td class="in">✓<span class="sr-only"> qualifying as it stands</span></td>'
                   if qualifying else '<td class="in"><span class="sr-only">out as it stands</span></td>')
        body.append(
            f'      <tr class="{cls}">\n'
            f'        <td class="pos">{pos}</td>\n'
            f'        {_team_cell(r, syms)}\n'
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


def render_slate(today: list[dict]) -> str:
    if not today:
        return '    <li class="empty">No matches on this editorial date.</li>'
    chips = []
    for r in today:
        moon = '<span class="moon" aria-label="midnight kickoff, this slate"> ☾</span>' \
            if r.get("_late_cap") else ""
        tv = (r.get("tv_us") or "").strip() or "TV TBD"
        venue = ", ".join(p for p in [(r.get("stadium") or "").strip(),
                                      (r.get("city") or "").strip()] if p)
        played = (r.get("status") or "").strip().lower() == "played"
        if played:
            teams = (f'{_esc(r["team_a"])} {_esc(str(r["score_a"]))}–'
                     f'{_esc(str(r["score_b"]))} {_esc(r["team_b"])}')
            time_bit = "FT"
        else:
            teams = f'{_esc(r["team_a"])} v {_esc(r["team_b"])}'
            time_bit = _esc((r.get("kickoff_et") or "").strip()) + " ET"
        chips.append(
            f'    <li><span class="t">{time_bit}{moon}</span>'
            f'<span class="teams">{teams}</span>'
            f'<span class="meta">{_esc(r["match_id"])} · {_esc(tv)} · {_esc(venue)}</span></li>')
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


# ---------------------------------------------------------------- assembly

def build_page(matches: "list[st.Match]", rows: list[dict], target: date,
               generated_at: str, template_path: Path = DEFAULT_TEMPLATE,
               editions_dir: Path = REPO_ROOT / "editions") -> tuple[str, dict]:
    """Render the full page. Returns (html, data_dict)."""
    s = st.compute_standings(matches)
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
        edition_no=f"No. {day_n}" if day_n >= 1 else "Preview",
        pretty_date=f"{target:%A, %B} {target.day}, {target.year}",
        played=s.played,
        total=s.total,
        progress_pct=f"{(100 * s.played / s.total) if s.total else 0:.1f}",
        group_nav_html=_group_nav(s.groups),
        slate_title=slate_title,
        slate_html=render_slate(today),
        groups_html=groups_html,
        thirds_html=render_thirds(s, forms),
        archive_html=_archive(editions_dir),
        generated_at=_esc(generated_at),
        repo_url=REPO_URL,
        data_json=data_json,
    )
    return page, data


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render the WC26 standings hub to a single static HTML page.")
    ap.add_argument("--date", default=None,
                    help="editorial date for the slate, YYYY-MM-DD (default: today)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "docs")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
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
    if not args.template.exists():
        print(f"error: template {args.template} not found.", file=sys.stderr)
        return 1

    try:
        matches = st.load_fixtures(args.fixtures)
        rows = be.read_rows(args.fixtures)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    page, data = build_page(matches, rows, target, generated_at,
                            template_path=args.template)

    for w in data.get("warnings", []):
        print(f"warning: {w}", file=sys.stderr)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    index = args.out_dir / "index.html"
    data_path = args.out_dir / "data.json"
    index.write_text(page, encoding="utf-8")
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print(str(index))
    print(str(data_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
