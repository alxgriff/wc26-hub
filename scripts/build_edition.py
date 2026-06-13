#!/usr/bin/env python3
"""WC26 daily-edition builder.

Assembles one markdown edition per ET *editorial* date from three inputs that
already exist in the repo:

  * ``data/fixtures.csv``      — the slate, scores, TV, venues (and yesterday's
                                 results for the Overnight recap)
  * ``scripts/standings.py``   — the group tables + third-place ranking
                                 (imported; ranking/tiebreak logic lives there
                                 only, never re-implemented here)
  * ``cards/*.md``             — pre-baked match cards in three layouts

The editorial date is the calendar date a match belongs to *in the edition*,
which differs from ``date_et`` for the three 🌙 late-cap games that kick off at
12:00 AM ET (D2, J2, F4): those belong to the previous evening's edition.

What this builder fills in is the **Stakes** slot of each card (current group
table + one factual sentence). It deliberately leaves **The Call** and **Odds &
Best Bet** untouched until Phase 5 (odds), and never synthesises card prose: a
missing card becomes a clearly-marked placeholder. Since Phase 4 went live, the
**The Call** slot is filled with the consensus prediction (predict.py + the
ledger; pre-baked qualitative leans preserved) and **Overnight** carries
prediction grading (✓/✗ + Brier + the running ledger line); missing prediction
inputs leave those in placeholder state — never invented.

Importable API (for tests and future scripts):

    rows      = read_rows(path)                       # list[dict], all columns
    today     = select_matches(rows, date(2026,6,12)) # editorial-date slice
    card, src = extract_card(mid, team_a, team_b, cards_dir)
    body      = build_stakes_body(standings, group, team_a, team_b)
    md        = build_edition(target, rows, standings, cards_dir)

CLI:
    python scripts/build_edition.py 2026-06-12 [--fixtures ...] [--cards-dir ...]
                                                [--out-dir ...] [--stdout]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st  # noqa: E402  (ranking/tiebreak logic lives here, not here)
import scenarios as sc  # noqa: E402  (MD3 qualification scenarios live here)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOURNAMENT_START = date(2026, 6, 11)          # June 11 = Day 1
LATE_CAP_KICKOFF = "00:00"                     # 12:00 AM ET → previous edition
EXPECTED_LATE_CAPS = {"D2", "J2", "F4"}        # the only rows that may shift
THIRD_PLACE_OUTSIDE_FOLD_FROM = date(2026, 6, 18)  # MD2 — tracker debuts


# ---------------------------------------------------------------- loading

def read_rows(path: str | Path) -> list[dict]:
    """All fixture rows as dicts (every column preserved), with two computed
    helper keys: ``_editorial`` (date) and ``_late_cap`` (bool)."""
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        kick = (r.get("kickoff_et_24h") or "").strip()
        mid = (r.get("match_id") or "").strip()
        # only the three sanctioned 00:00 games shift to the previous edition; a
        # stray 00:00 kickoff elsewhere stays on its own date (and is warned about)
        r["_late_cap"] = kick == LATE_CAP_KICKOFF and mid in EXPECTED_LATE_CAPS
        r["_editorial"] = editorial_date_of((r.get("date_et") or "").strip(), kick, mid)
    return rows


def editorial_date_of(date_et: str, kickoff_et_24h: str, match_id: str = "") -> date:
    """Edition date for a match: its ET calendar date, shifted one day earlier for
    the three sanctioned 🌙 late-cap games (D2/J2/F4) at 12:00 AM ET. A stray 00:00
    kickoff on any other fixture is NOT shifted (the 'only these may shift' contract
    self-enforces). Called without match_id, it shifts on the kickoff alone."""
    d = date.fromisoformat(date_et)
    if (kickoff_et_24h.strip() == LATE_CAP_KICKOFF
            and (not match_id or match_id in EXPECTED_LATE_CAPS)):
        return d - timedelta(days=1)
    return d


def _kickoff_sort_key(row: dict) -> int:
    """Minutes past midnight, with late-cap (00:00) pushed to end-of-day so the
    🌙 game sorts *after* the evening games it shares an edition with. A malformed
    kickoff sorts last (sentinel) rather than crashing the whole edition build."""
    try:
        h, m = (int(x) for x in (row.get("kickoff_et_24h") or "0:0").split(":"))
        mins = h * 60 + m
    except (ValueError, TypeError):
        return 99 * 60   # unknown/malformed kickoff -> sort after everything
    return mins + 24 * 60 if row.get("_late_cap") else mins


def select_matches(rows: list[dict], target: date) -> list[dict]:
    """Rows whose *editorial* date is ``target``, in chronological kickoff order."""
    todays = [r for r in rows if r.get("_editorial") == target]
    return sorted(todays, key=_kickoff_sort_key)


# ---------------------------------------------------------------- card extraction

_SEP_RE = re.compile(r"^-{3,}$")


def _strip_trailing_separators(lines: list[str]) -> list[str]:
    out = list(lines)
    while out and (out[-1].strip() == "" or _SEP_RE.match(out[-1].strip())):
        out.pop()
    return out


def _slice(lines: list[str], start: int, is_stop) -> str:
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if is_stop(lines[j]):
            end = j
            break
    return "\n".join(_strip_trailing_separators(lines[start:end]))


def _extract_by_id(text: str, mid: str, header: str, stop_prefixes: tuple[str, ...]) -> str | None:
    """Formats A/B: a card headed ``{header} {mid}:`` (e.g. ``## A1:`` or
    ``### A5:``), sliced to the next header at one of ``stop_prefixes``."""
    lines = text.splitlines()
    start_re = re.compile(rf"^{re.escape(header)}\s+{re.escape(mid)}:")
    for i, ln in enumerate(lines):
        if start_re.match(ln):
            return _slice(lines, i, lambda l: any(l.startswith(p) for p in stop_prefixes))
    return None


def _extract_by_teams(text: str, team_a: str, team_b: str) -> str | None:
    """Format C (template.md): an H1 (``# ...``) line naming *both* teams,
    sliced to the next H1 or the ``## Pre-bake status`` table."""
    lines = text.splitlines()

    def is_h1(line: str) -> bool:
        return line.startswith("# ")

    for i, ln in enumerate(lines):
        if is_h1(ln) and team_a in ln and team_b in ln:
            return _slice(lines, i, lambda l: is_h1(l) or l.startswith("## Pre-bake status"))
    return None


def extract_card(mid: str, team_a: str, team_b: str, cards_dir: str | Path
                 ) -> tuple[str | None, str | None]:
    """Locate a match's pre-baked card. Returns ``(card_text, source_filename)``
    or ``(None, None)`` if absent anywhere.

    Layout by match_id digit: 1–2 → md1.md (``## {id}:``), 3–4 → md2.md
    (``## {id}:``), 5–6 → md3.md (``### {id}:``). B1 and D1 live only in
    template.md (H1-by-team-names), so MD1 ids fall back to it.
    """
    cards_dir = Path(cards_dir)
    digit = int(mid[1])
    if digit in (1, 2):
        plan = [("md1.md", "id", "##"), ("template.md", "teams", None)]
    elif digit in (3, 4):
        plan = [("md2.md", "id", "##")]
    else:
        plan = [("md3.md", "id", "###")]

    for fname, kind, header in plan:
        path = cards_dir / fname
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if kind == "id":
            stop = ("## ",) if header == "##" else ("### ", "## ")
            block = _extract_by_id(text, mid, header, stop)
        else:
            block = _extract_by_teams(text, team_a, team_b)
        if block:
            return block, fname
    return None, None


# ---------------------------------------------------------------- stakes injection

# single-line only: real *[...]* lean/hint markers never span lines, and DOTALL
# could let the non-greedy capture run across the wrong emphasis run in the
# multi-line "## The Call" section path
_LEAN_RE = re.compile(r"\*\[(.+?)\]\*")


def inject_call(card: str, call_body: str) -> tuple[str, bool]:
    """Replace a card's The Call placeholder with ``call_body``, preserving the
    pre-baked qualitative lean (the *[...]* text) as a trailing italic line and
    leaving Odds & Best Bet in placeholder state (Phase 5).

    Handles all three card layouts: ``**The Call:**`` line (md1/md2), the
    combined ``**The Call / Odds & Best Bet:**`` line (md3), and the
    ``## The Call`` section (template.md). Returns ``(card, replaced?)``."""
    lines = card.split("\n")

    def with_lean(text_after_marker: str) -> str:
        m = _LEAN_RE.search(text_after_marker)
        lean = m.group(1).strip() if m else ""
        return call_body + (f"\n\n_Pre-baked lean: {lean}_" if lean else "")

    for i, ln in enumerate(lines):
        if ln.startswith("**The Call / Odds & Best Bet:**"):
            block = ["**The Call:**", "", with_lean(ln),
                     "**Odds & Best Bet:** *[Phase 5 — market snapshot pending.]*"]
            return "\n".join(lines[:i] + block + lines[i + 1:]), True
        if ln.startswith("**The Call:**"):
            block = ["**The Call:**", "", with_lean(ln)]
            return "\n".join(lines[:i] + block + lines[i + 1:]), True

    for i, ln in enumerate(lines):
        if ln.strip() == "## The Call":
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            body_text = "\n".join(lines[i + 1:j])
            block = [lines[i], "", with_lean(body_text), ""]
            return "\n".join(lines[:i] + block + lines[j:]), True

    return card, False


def inject_odds(card: str, odds_body: str) -> tuple[str, bool]:
    """Replace a card's Odds & Best Bet placeholder with ``odds_body``,
    preserving the pre-baked "markets to watch" hint as a trailing italic line.
    Handles the inline ``**Odds & Best Bet:**`` line (md1/md2, and md3 after
    inject_call splits its combined slot) and the ``## Odds & Best Bet``
    section (template.md). Returns ``(card, replaced?)``."""
    lines = card.split("\n")

    def with_hint(placeholder_text: str) -> str:
        m = _LEAN_RE.search(placeholder_text)
        hint = m.group(1).strip() if m else ""
        return odds_body + (f"\n\n_Pre-baked note: {hint}_" if hint else "")

    for i, ln in enumerate(lines):
        if ln.startswith("**Odds & Best Bet:**"):
            block = ["**Odds & Best Bet:**", "", with_hint(ln)]
            return "\n".join(lines[:i] + block + lines[i + 1:]), True
    for i, ln in enumerate(lines):
        if ln.strip() == "## Odds & Best Bet":
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            block = [lines[i], "", with_hint("\n".join(lines[i + 1:j])), ""]
            return "\n".join(lines[:i] + block + lines[j:]), True
    return card, False


def inject_stakes(card: str, stakes_body: str) -> tuple[str, bool]:
    """Replace a card's Stakes placeholder with ``stakes_body``, leaving The Call
    and Odds & Best Bet exactly as written. Returns ``(card, replaced?)``.

    Handles both card layouts: the inline ``**Stakes:** *[...]*`` line
    (md1/md2/md3) and the ``## Stakes`` section with a placeholder body
    (template.md)."""
    lines = card.split("\n")

    for i, ln in enumerate(lines):
        if ln.startswith("**Stakes:**"):
            block = ["**Stakes:**", "", stakes_body]
            return "\n".join(lines[:i] + block + lines[i + 1:]), True

    for i, ln in enumerate(lines):
        if ln.strip() == "## Stakes":
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            block = [lines[i], "", stakes_body, ""]
            return "\n".join(lines[:i] + block + lines[j:]), True

    return card, False


def _fmt_gd(gd: int) -> str:
    return f"+{gd}" if gd > 0 else str(gd)


def render_group_table(gt: "st.GroupTable") -> str:
    """A single group's table, in the same column layout as standings.render_markdown
    (rows already ranked by the standings engine — this only formats them)."""
    out = [
        f"_Group {gt.group} — table after {_played_in_group(gt)} of 6 matches_",
        "",
        "| Pos | Team | P | W | D | L | GF | GA | GD | Pts |",
        "|---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for pos, r in enumerate(gt.rows, 1):
        out.append(
            f"| {pos} | {r.team} | {r.played} | {r.won} | {r.drawn} | {r.lost} "
            f"| {r.gf} | {r.ga} | {_fmt_gd(r.gd)} | {r.points} |"
        )
    return "\n".join(out)


def _played_in_group(gt: "st.GroupTable") -> int:
    return sum(r.played for r in gt.rows) // 2


def _pts(r: "st.TeamRow") -> str:
    return f"{r.points} pt" + ("" if r.points == 1 else "s")


_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def _ordinal(pos: int) -> str:
    return _ORDINALS.get(pos, f"{pos}th")


def _record(r: "st.TeamRow") -> str:
    return f"{r.won}-{r.drawn}-{r.lost}"


def _standing_clause(team: str, pos: int, r: "st.TeamRow") -> str:
    return f"{team} {_ordinal(pos)} on {_pts(r)} ({_record(r)}, {_fmt_gd(r.gd)} GD)"


def _gap_phrase(gap: int) -> str:
    if gap <= 0:
        return "level on points"
    return f"{gap} pt{'' if gap == 1 else 's'} back"


def _cutline_note(gt: "st.GroupTable", team_a: str, pa: int, ra: "st.TeamRow",
                  team_b: str, pb: int, rb: "st.TeamRow") -> str:
    """Describe where the two match teams sit relative to the top-two cutline.
    Purely a reading of the current table — no projection of future results."""
    if len(gt.rows) < 2:
        return ""
    second = gt.rows[1].points
    a_in, b_in = pa <= 2, pb <= 2
    if a_in and b_in:
        note = "both hold top-two places as it stands"
    elif not a_in and not b_in:
        note = (f"both sit outside the top two ({team_a} {_gap_phrase(second - ra.points)}, "
                f"{team_b} {_gap_phrase(second - rb.points)})")
    elif a_in:
        note = f"{team_a} hold a top-two spot; {team_b} {_gap_phrase(second - rb.points)} of the cutline"
    else:
        note = f"{team_b} hold a top-two spot; {team_a} {_gap_phrase(second - ra.points)} of the cutline"
    if 3 in (pa, pb):
        note += " — the best eight third-placed teams also advance"
    return note


def stakes_sentence(gt: "st.GroupTable", team_a: str, team_b: str) -> str:
    """A few factual sentences of standings context for the two teams in this
    match — positions, records, the group lead, and the top-two cutline. Strictly
    descriptive of the current table; outcome scenarios are Phase 3 (scenarios.py)."""
    n = _played_in_group(gt)
    if n == 0:
        return (f"Group {gt.group} opens with this matchday — all four teams "
                "start on 0 points.")

    pos = {r.team: i for i, r in enumerate(gt.rows, 1)}
    by_team = {r.team: r for r in gt.rows}
    pa, pb = pos.get(team_a), pos.get(team_b)
    ra, rb = by_team.get(team_a), by_team.get(team_b)
    if ra is None or rb is None:  # team not in this group's table (shouldn't happen)
        return f"Group {gt.group}: {n} match{'es' if n != 1 else ''} played."

    parts = [
        f"After {n} match{'es' if n != 1 else ''} in Group {gt.group}: "
        f"{_standing_clause(team_a, pa, ra)}, {_standing_clause(team_b, pb, rb)}."
    ]
    top = gt.rows[0].points
    leaders = [r for r in gt.rows if r.points == top]
    if len(leaders) == 1:
        ctx = f"{leaders[0].team} lead the group on {_pts(leaders[0])}"
    else:
        ctx = f"{' and '.join(r.team for r in leaders)} share the lead on {_pts(leaders[0])}"
    cut = _cutline_note(gt, team_a, pa, ra, team_b, pb, rb)
    parts.append(f"{ctx}; {cut}." if cut else f"{ctx}.")
    return " ".join(parts)


def build_stakes_body(standings: "st.Standings", group: str, team_a: str, team_b: str,
                      scenario: "sc.ScenarioReport | None" = None) -> str:
    """Stakes block = current group table + context. On MD3 days the context is
    the qualification-scenario slice for these two teams; otherwise it is the
    factual one-liner (positions/records/cutline)."""
    gt = standings.groups.get(group)
    if gt is None:
        return f"*[Standings for Group {group} unavailable.]*"
    if scenario is not None:
        context = sc.render_match_stakes(scenario, team_a, team_b)
    else:
        context = stakes_sentence(gt, team_a, team_b)
    return render_group_table(gt) + "\n\n" + context


# ---------------------------------------------------------------- edition assembly

def _third_place_section(full_md: str) -> str | None:
    """Slice the third-place block out of a full render_markdown() string, so the
    formatting can never drift from the in-fold copy."""
    marker = "## Third-place ranking"
    idx = full_md.find(marker)
    return full_md[idx:].rstrip() if idx != -1 else None


def _slate_line(r: dict) -> str:
    moon = " 🌙" if r.get("_late_cap") else ""
    tv = (r.get("tv_us") or "").strip() or "TV TBD"
    venue = ", ".join(p for p in [(r.get("stadium") or "").strip(),
                                  (r.get("city") or "").strip()] if p)
    return (f"- **{(r.get('kickoff_et') or '').strip()} ET**{moon} · "
            f"{r['match_id']} · {r['team_a']} vs {r['team_b']} · {tv} · {venue}")


def _overnight_section(rows: list[dict], yesterday: date,
                       graded: dict | None = None,
                       cumulative: str | None = None) -> list[str]:
    """Yesterday's results, with per-match prediction grades (✓/✗ + Brier) when
    the ledger has a published call for a match."""
    graded = graded or {}
    prior = select_matches(rows, yesterday)
    out = ["## Overnight", ""]
    if not prior:
        out.append("_No matches on the previous editorial date._")
        out.append("")
        return out
    pretty = f"{yesterday:%A, %B} {yesterday.day}"
    out.append(f"Results from {pretty}:")
    out.append("")
    for r in prior:
        moon = " 🌙" if r.get("_late_cap") else ""
        note = (r.get("notes") or "").strip()
        sa, sb = (r.get("score_a") or "").strip(), (r.get("score_b") or "").strip()
        # guard the importable API (which bypasses load_fixtures validation): a
        # "played" row with a blank score must NOT publish a dash with empty operands
        if (r.get("status") or "").strip().lower() == "played" and sa != "" and sb != "":
            line = (f"- **{r['match_id']}**{moon} {r['team_a']} "
                    f"{sa}–{sb} {r['team_b']}")
            if note:
                line += f" — {note}"
            out.append(line)
            g = graded.get(r["match_id"])
            if g:
                call = "/".join(f"{x:.0%}" for x in g["p"])
                score = f", predicted {g['predicted_score']}" if g.get("predicted_score") else ""
                out.append(f"  - Our call: {call} (H/D/A{score}) → "
                           f"{'✓ correct' if g['correct'] else '✗ wrong'} · "
                           f"Brier {g['brier']:.3f}")
        else:
            out.append(f"- ⚠️ **{r['match_id']}**{moon} {r['team_a']} vs "
                       f"{r['team_b']} — **result not yet entered**")
    out.append("")
    out.append(cumulative if cumulative else
               "_No graded predictions yet — the Brier ledger starts once a "
               "logged call's match is played._")
    out.append("")
    return out


def _verify_callouts(today: list[dict]) -> list[str]:
    """Surface any 'verify before publishing' flags carried in the notes field
    (e.g. F4 Tunisia–Japan's kickoff re-verification for the June 20 edition)."""
    out = []
    for r in today:
        note = (r.get("notes") or "").strip()
        if "verify" in note.lower():
            out.append(f"> ⚠️ **Verify before publishing — {r['match_id']} "
                       f"{r['team_a']} vs {r['team_b']}:** {note}")
    return out


def build_edition(target: date, rows: list[dict], standings: "st.Standings",
                  cards_dir: str | Path,
                  matches: "list[st.Match] | None" = None,
                  calls: "dict[str, str] | None" = None,
                  graded: dict | None = None,
                  cumulative: str | None = None,
                  odds_bodies: "dict[str, str] | None" = None) -> tuple[str, list[str]]:
    """Render the full edition markdown for ``target``. Returns
    ``(markdown, warnings)``; warnings are data-integrity notes for stderr.

    ``matches`` (the parsed fixtures) enables MD3 qualification scenarios in the
    Stakes slots; when omitted, MD3 cards fall back to the factual block.
    ``calls`` ({match_id: The-Call body}) fills the cards' The Call slots;
    ``graded``/``cumulative`` (from ledger.grade / ledger.cumulative_line) add
    prediction grading to Overnight. All optional: when absent, those slots stay
    in placeholder state (never invented)."""
    warnings: list[str] = []
    calls = calls or {}
    odds_bodies = odds_bodies or {}

    # key on the raw kickoff, not _late_cap (which is now gated to the sanctioned
    # ids) — this is exactly how a stray 00:00 fixture surfaces for review
    unexpected = {r["match_id"] for r in rows
                  if (r.get("kickoff_et_24h") or "").strip() == LATE_CAP_KICKOFF} - EXPECTED_LATE_CAPS
    if unexpected:
        warnings.append(
            f"unexpected 00:00 ET kickoff(s) {sorted(unexpected)} — only "
            f"{sorted(EXPECTED_LATE_CAPS)} should be 🌙 late-cap games; a fixture's "
            "kickoff may have changed")

    today = select_matches(rows, target)
    day_n = (target - TOURNAMENT_START).days + 1
    parts: list[str] = []

    # Masthead
    parts.append(f"# WC26 Daily — {target:%A, %B} {target.day}")
    parts.append("")
    if today:
        first = today[0]
        tv = (first.get("tv_us") or "").strip()
        kick = f"{(first.get('kickoff_et') or '').strip()} ET" + (f" ({tv})" if tv else "")
        count = f"{len(today)} match" + ("" if len(today) == 1 else "es")
        parts.append(f"**Day {day_n}** of the group stage · {count} · First kickoff {kick}")
    else:
        parts.append(f"**Day {day_n}** of the group stage · no matches on this editorial date")
    parts.append("")

    for callout in _verify_callouts(today):
        parts.append(callout)
        parts.append("")

    # Overnight
    parts.extend(_overnight_section(rows, target - timedelta(days=1),
                                    graded=graded, cumulative=cumulative))

    # Today's slate
    parts.append("## Today's slate")
    parts.append("")
    if today:
        parts.extend(_slate_line(r) for r in today)
    else:
        parts.append("_No matches today._")
    parts.append("")

    # Standings snapshot
    full_md = st.render_markdown(standings)
    parts.append("## Standings")
    parts.append("")
    parts.append("<details>")
    parts.append("<summary>All group tables + third-place race</summary>")
    parts.append("")
    parts.append(full_md.rstrip())
    parts.append("")
    parts.append("</details>")
    parts.append("")
    if target >= THIRD_PLACE_OUTSIDE_FOLD_FROM:
        third = _third_place_section(full_md)
        if third:
            parts.append("### Third-place race")
            parts.append("")
            parts.append(third)
            parts.append("")

    # Match cards
    parts.append("## Match cards")
    parts.append("")
    if any(int(r["match_id"][1]) >= 3 for r in today):  # MD2/MD3 cards present
        parts.append("> ⚠️ **Pre-baked cards:** injury and selection notes below "
                     "were written June 11. Verify day-of against current news; "
                     'anything tagged "(verify before use)" must be confirmed or cut.')
        parts.append("")

    # Precompute one MD3 scenario report per group that has an MD3 card today
    # (both that group's cards reuse it). Needs the parsed fixtures.
    scenarios_by_group: dict[str, "sc.ScenarioReport"] = {}
    if matches is not None:
        for g in sorted({r["group"] for r in today if int(r["match_id"][1]) >= 5}):
            unplayed = sum(1 for m in matches if m.group == g and not m.is_played)
            if unplayed != 2:
                warnings.append(f"group {g}: {unplayed} games unplayed, not the clean "
                                "2-game MD3 state — enter MD1/MD2 results first; using "
                                "factual stakes for now")
                continue
            try:
                scenarios_by_group[g] = sc.enumerate_scenarios(g, matches)
            except (ValueError, KeyError) as e:
                warnings.append(f"group {g}: MD3 scenario enumeration failed ({e}); "
                                "falling back to factual stakes")

    card_chunks: list[str] = []
    for r in today:
        mid, team_a, team_b, group = r["match_id"], r["team_a"], r["team_b"], r["group"]
        scenario = scenarios_by_group.get(group) if int(mid[1]) >= 5 else None
        body = build_stakes_body(standings, group, team_a, team_b, scenario=scenario)
        card, src = extract_card(mid, team_a, team_b, cards_dir)
        if card is None:
            warnings.append(f"{mid}: no card found in cards/ — inserting placeholder")
            chunk = (f"## {mid}: {team_a} vs {team_b}\n\n"
                     f"> ⚠️ **Card not found** in cards/. Placeholder — no card "
                     f"prose synthesised.\n\n**Stakes:**\n\n{body}\n\n"
                     f"**The Call:** *[Model pending — Phase 4.]*\n"
                     f"**Odds & Best Bet:** *[Phase 5.]*")
        else:
            chunk, replaced = inject_stakes(card, body)
            if not replaced:
                warnings.append(f"{mid}: card has no Stakes slot — left unchanged "
                                f"(source {src})")
            if mid in calls:
                chunk, call_ok = inject_call(chunk, calls[mid])
                if not call_ok:
                    warnings.append(f"{mid}: card has no The Call slot — prediction "
                                    f"not injected (source {src})")
            if mid in odds_bodies:
                chunk, odds_ok = inject_odds(chunk, odds_bodies[mid])
                if not odds_ok:
                    warnings.append(f"{mid}: card has no Odds & Best Bet slot — "
                                    f"odds not injected (source {src})")
        card_chunks.append(chunk)
    parts.append("\n\n---\n\n".join(card_chunks) if card_chunks else "_No cards today._")

    return "\n".join(parts).rstrip() + "\n", warnings


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build one WC26 daily edition from fixtures + standings + cards.")
    ap.add_argument("date", help="editorial date, YYYY-MM-DD (e.g. 2026-06-12)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--cards-dir", type=Path, default=REPO_ROOT / "cards")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "editions")
    ap.add_argument("--stdout", action="store_true",
                    help="also echo the edition to stdout")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        print(f"error: date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2
    if not args.fixtures.exists():
        print(f"error: {args.fixtures} not found.", file=sys.stderr)
        return 1

    try:
        matches = st.load_fixtures(args.fixtures)       # validates canon/structure
        rows = read_rows(args.fixtures)                 # all columns for the edition
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    standings = st.compute_standings(matches, fair_play=st.load_discipline())
    for w in standings.warnings:
        print(f"warning: {w}", file=sys.stderr)

    # Predictions + grading (Phase 4). Local imports: ledger imports this module,
    # so the dependency must stay one-way at import time. Any failure leaves the
    # affected slots in placeholder state — never invented.
    calls: dict[str, str] = {}
    graded = None
    cumulative = None
    try:
        import ledger as lg
        import predict as pr
        for line in lg.log_slate(target, args.fixtures):
            print(f"ledger: {line}", file=sys.stderr)
        ledger_rows = lg.load_ledger()
        graded = lg.grade(matches, ledger_rows)
        cumulative = lg.cumulative_line(matches, ledger_rows)
        model = pr.load_ratings(fixtures=args.fixtures)
        overlay = pr.load_match_overlay()
        for r in select_matches(rows, target):
            mid = r["match_id"]
            host = pr.HOST_BY_COUNTRY.get((r.get("country") or "").strip())
            hfa = host if host in (r["team_a"], r["team_b"]) else None
            pred = pr.predict_match(model, r["team_a"], r["team_b"], hfa_team=hfa)
            ov = overlay.get(mid)
            if ov:
                pa, pd_, pb = pr.blend_wdl(pred, ov)
                srcs = (f"consensus of our model "
                        f"({pred.p_a:.0%}/{pred.p_draw:.0%}/{pred.p_b:.0%}) and "
                        f"{ov['source'] or 'Opta'} "
                        f"({ov['p_home']:.0%}/{ov['p_draw']:.0%}/{ov['p_away']:.0%})")
            else:
                pa, pd_, pb = pred.p_a, pred.p_draw, pred.p_b
                srcs = "our model (no second source for this match yet)"
            mi, mj = pred.modal_score
            calls[mid] = (
                f"**{r['team_a']} {pa:.0%} · Draw {pd_:.0%} · {r['team_b']} {pb:.0%}** — "
                f"{srcs}. Predicted score **{mi}–{mj}** "
                f"(xG {pred.lambda_a:.2f}–{pred.lambda_b:.2f}); "
                f"Over 2.5 {pred.over[2.5]:.0%}, BTTS {pred.btts:.0%}.")
    except Exception as e:  # missing ratings, canon mismatch, ledger guard, ...
        print(f"warning: predictions unavailable ({e}) — The Call slots left "
              "in placeholder state", file=sys.stderr)

    # Odds & best bets (Phase 5): consume logged snapshots only — no snapshot
    # for a match means its slot stays in placeholder state (never invented).
    odds_bodies: dict[str, str] = {}
    try:
        import ledger as lg
        import odds as od
        import predict as pr
        odds_rows = od.load_odds()
        if odds_rows:
            # NB: settling picks is the pipeline's job (odds.py settle), not the
            # renderer's — building an edition must not mutate the picks ledger as
            # a side effect. We only READ here.
            ledger_rows = lg.load_ledger()
            model = pr.load_ratings(fixtures=args.fixtures)
            now = lg.now_et()
            for r in select_matches(rows, target):
                mid = r["match_id"]
                if not any(o["match_id"] == mid for o in odds_rows):
                    continue
                host = pr.HOST_BY_COUNTRY.get((r.get("country") or "").strip())
                hfa = host if host in (r["team_a"], r["team_b"]) else None
                pred = pr.predict_match(model, r["team_a"], r["team_b"], hfa_team=hfa)
                ev = od.evaluate_match(mid, odds_rows, ledger_rows, pred)
                picks, flags = od.best_bets(ev)
                bp = od._best_prices(odds_rows, mid)
                # Rendering only: `odds.py evaluate --record` is the single
                # canonical recorder (it owns the day-of and snapshot-freshness
                # gates) — building an edition must never place a bet.
                odds_bodies[mid] = od.render_odds_section(mid, ev, picks, flags, bp)
            units = od.units_summary(od.load_picks())
            if units:
                cumulative = (cumulative + "\n" + units) if cumulative else units
    except Exception as e:
        print(f"warning: odds evaluation unavailable ({e}) — Odds & Best Bet "
              "slots left in placeholder state", file=sys.stderr)

    edition, warnings = build_edition(target, rows, standings, args.cards_dir,
                                      matches=matches, calls=calls,
                                      graded=graded, cumulative=cumulative,
                                      odds_bodies=odds_bodies)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{args.date}.md"
    out_path.write_text(edition, encoding="utf-8")
    print(str(out_path))
    if args.stdout:
        sys.stdout.write("\n" + edition)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
