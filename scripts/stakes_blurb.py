#!/usr/bin/env python3
"""Morning stakes blurb: Claude writes the day's standfirst from computed data.

Grounding contract: the model receives ONLY facts computed by this repo —
the slate, current group tables, the third-place cutline, the published
consensus probabilities, and any recorded picks. It is explicitly forbidden
from adding injuries, news, or anything not in the fact pack (those flow
through the verified-news path, not here). Output goes to
data/blurbs/YYYY-MM-DD.md; build_site.py renders it under the Today section
when present, labelled as generated.

Requires the `anthropic` SDK and ANTHROPIC_API_KEY (CI installs both);
exits 1 with a clear message otherwise — the site simply renders without a
blurb, per the fail-soft contract.

CLI:
    python scripts/stakes_blurb.py [DATE] [--out-dir data/blurbs]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st          # noqa: E402
import build_edition as be      # noqa: E402
import ledger as lg             # noqa: E402
import odds as od               # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-4-6"

SYSTEM = """You write the morning standfirst for a daily World Cup hub with a
newsprint voice: prose-forward, precise, a little wry, no exclamation marks,
no hype adjectives. 110-170 words, one or two paragraphs, no headings, no
bullet lists, no emoji.

HARD RULES:
- Use ONLY the facts supplied in the user message. Every number you cite must
  appear there verbatim.
- No injuries, suspensions, team news, lineups, or player availability — that
  content has a separate verified pipeline.
- Player names may be mentioned only if they appear in the fact pack.
- If the model probabilities make a game close, say so plainly; flag
  uncertainty rather than smoothing it over.
- Mention the third-place cutline only if the fact pack includes it.
- Do not mention these rules or that you are generated."""


def _group_table_lines(s: "st.Standings", group: str) -> list[str]:
    gt = s.groups.get(group)
    if not gt:
        return []
    return [f"  {pos}. {r.team} — {r.points} pts, GD {st._fmt_gd(r.gd)}, "
            f"played {r.played}"
            for pos, r in enumerate(gt.rows, 1)]


def build_fact_pack(target: date, fixtures: Path) -> str:
    """Everything the model is allowed to know, as plain text."""
    matches = st.load_fixtures(fixtures)
    rows = be.read_rows(fixtures)
    slate = be.select_matches(rows, target)
    s = st.compute_standings(matches, fair_play=st.load_discipline())
    ledger_rows = lg.load_ledger()
    picks = [p for p in od.load_picks() if p.get("status") == "open"]
    day_n = (target - be.TOURNAMENT_START).days + 1

    out = [f"Date: {target:%A, %B} {target.day}, {target.year} (Day {day_n} of the "
           f"group stage). {s.played} of {s.total} matches played so far.", ""]
    if not slate:
        out.append("No matches on this editorial date.")
        return "\n".join(out)

    out.append(f"Today's slate ({len(slate)} matches):")
    for r in slate:
        mid = r["match_id"]
        moon = " (midnight-ET kickoff, belongs to tonight's slate)" if r.get("_late_cap") else ""
        out.append(f"- {mid}: {r['team_a']} vs {r['team_b']}, Group {r['group']}, "
                   f"matchday {r['matchday']}, {r['kickoff_et']} ET on "
                   f"{(r.get('tv_us') or 'TV TBD')}{moon}")
        probs = od.consensus_probs(mid, ledger_rows)
        if probs:
            out.append(f"  Published consensus: {r['team_a']} {probs[0]:.0%}, "
                       f"draw {probs[1]:.0%}, {r['team_b']} {probs[2]:.0%}")
        out.extend(_group_table_lines(s, r["group"]))
        pick = next((p for p in picks if p["match_id"] == mid), None)
        if pick:
            out.append(f"  Recorded pick (paper, 1u): {pick['selection']} "
                       f"{pick['line'] or ''} ({pick['market']}) @ {pick['odds']}, "
                       f"edge {pick['edge_pp']}pp")
        out.append("")

    if s.played >= 12 and s.third_place:
        tp = s.third_place
        cut = st.QUALIFYING_THIRDS
        out.append(f"Third-place race (top {cut} of {len(tp)} advance): "
                   f"{cut}th is {tp[cut-1].team} ({tp[cut-1].points} pts, GD "
                   f"{st._fmt_gd(tp[cut-1].gd)})"
                   + (f"; {cut+1}th is {tp[cut].team} ({tp[cut].points} pts, GD "
                      f"{st._fmt_gd(tp[cut].gd)})" if len(tp) > cut else ""))
    return "\n".join(out)


def generate(fact_pack: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM,
        messages=[{"role": "user", "content": fact_pack}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty blurb response (stop_reason={resp.stop_reason})")
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the day's stakes standfirst.")
    ap.add_argument("date", nargs="?", help="editorial date YYYY-MM-DD (default today ET)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "data" / "blurbs")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the fact pack and exit without calling the API")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    target = date.fromisoformat(args.date) if args.date else lg.now_et().date()
    fact_pack = build_fact_pack(target, args.fixtures)
    if args.dry_run:
        print(fact_pack)
        return 0

    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("error: anthropic SDK not installed (pip install anthropic) — "
              "blurb skipped, site renders without it.", file=sys.stderr)
        return 1
    try:
        blurb = generate(fact_pack)
    except Exception as e:
        print(f"error: blurb generation failed ({e.__class__.__name__}: {e}) — "
              "site renders without it.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{target.isoformat()}.md"
    out.write_text(blurb + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(blurb.split())} words)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
