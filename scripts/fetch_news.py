#!/usr/bin/env python3
"""AUTO-GATHERED news digest for today's teams via Claude + web search.

Output goes to news/YYYY-MM-DD.md stamped UNVERIFIED. build_site relays it
on match pages as "The Wire" — attributed reporting in a visually distinct
box, never the hub's own voice. Per the CLAUDE.md verification rules, a
claim only becomes load-bearing (card edits, discipline.csv rows that feed
the tiebreaker math) after human verification at its source. Every claim
must carry a source URL; the model is instructed to report "nothing found"
rather than pad.

CLI:
    python scripts/fetch_news.py [DATE] [--out-dir news]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_edition as be      # noqa: E402
import ledger as lg             # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-4-6"
MAX_CONTINUATIONS = 3

SYSTEM = """You are a news researcher for a World Cup daily. Search the web for
injury, suspension, disciplinary, and confirmed-lineup news from the LAST 48
HOURS for the teams listed. Rules:
- Every claim must end with its source in parentheses: (source: URL).
- Only report what a source actually says; no inference, no speculation,
  no transfer gossip, no match previews.
- Suspensions and red/yellow card accumulations are especially valuable —
  note which match the cards came from when the source says.
- If you find nothing solid for a match, write exactly: "No verifiable
  updates found." for that match.
- Output: a markdown section per match (### {match_id}: {team A} vs {team B})
  with a short bullet list. Nothing else — no preamble, no summary."""

BANNER = """<!-- AUTO-GATHERED by scripts/fetch_news.py — UNVERIFIED.
Per CLAUDE.md verification rules these claims must be web-verified (or cut)
by the edition pass before they are load-bearing anywhere. -->

> ⚠️ **Auto-gathered news digest — UNVERIFIED.** Verify every claim at its
> source before it informs a card, an edition, or the discipline log.

"""


def build_prompt(target: date, fixtures: Path) -> str | None:
    rows = be.read_rows(fixtures)
    slate = be.select_matches(rows, target)
    if not slate:
        return None
    lines = [f"Today is {target.isoformat()}. Matches on today's World Cup slate:"]
    for r in slate:
        lines.append(f"- {r['match_id']}: {r['team_a']} vs {r['team_b']} "
                     f"(Group {r['group']}, kickoff {r['kickoff_et']} ET)")
    lines.append("\nSearch for the latest news per the rules and produce the digest.")
    return "\n".join(lines)


def gather(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    text_parts: list[str] = []
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            tools=[{"type": "web_search_20260209", "name": "web_search",
                    "max_uses": 8}],
            messages=messages,
        )
        text_parts.extend(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason != "pause_turn":
            break
        # server-side tool loop paused; re-send to resume where it left off
        messages = [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": resp.content}]
    digest = "".join(text_parts).strip()
    if not digest:
        raise RuntimeError("empty digest response")
    return digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gather (UNVERIFIED) news for today's slate.")
    ap.add_argument("date", nargs="?", help="editorial date YYYY-MM-DD (default today ET)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "news")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    target = date.fromisoformat(args.date) if args.date else lg.now_et().date()
    prompt = build_prompt(target, args.fixtures)
    if prompt is None:
        print(f"no matches on editorial date {target}; nothing to gather")
        return 0

    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("error: anthropic SDK not installed — news digest skipped.",
              file=sys.stderr)
        return 1
    try:
        digest = gather(prompt)
    except Exception as e:
        print(f"error: news gathering failed ({e.__class__.__name__}: {e}) — "
              "digest skipped.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{target.isoformat()}.md"
    out.write_text(BANNER + digest + "\n", encoding="utf-8")
    print(f"wrote {out} (UNVERIFIED — review before use)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
