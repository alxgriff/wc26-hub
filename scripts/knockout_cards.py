#!/usr/bin/env python3
"""Auto-generated knockout match cards (overnight build, auto-published).

A knockout matchup isn't known until the prior round is played, so these cards can't be
pre-baked like the group cards — they're generated as each tie resolves. The tactical
sections (The Matchup, Key Duel, Watch For, Margin Notes) are written by Sonnet grounded
STRICTLY in the two teams' KB profiles + computed facts (same grounding contract as
scripts/stakes_blurb.py / fetch_news.py); the rest (Header, Stakes/Road, The Call advance
%, Odds slot) is assembled in code. Injury / selection notes are tagged "(verify before
use)". Fail-soft throughout: a missing profile, no API key, or an empty response yields
None (the site renders its placeholder) — nothing is ever fabricated.

The output is the H1 + `## section` card format that site_content.parse_card already reads,
written to cards/ko/M{no}.md and loaded by build_site for the knockout match page.

CLI: python scripts/knockout_cards.py [DATE]   # cards for resolved, unplayed ties near DATE
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st          # noqa: E402
import bracket as bk            # noqa: E402
import knockout as ko           # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CARDS_KO_DIR = REPO_ROOT / "cards" / "ko"
MODEL = "claude-sonnet-4-6"

_KO_ROUND_NAME = {"R32": "Round of 32", "R16": "Round of 16", "QF": "Quarter-final",
                  "SF": "Semi-final", "3RD": "Third-place play-off", "Final": "Final"}
# what a win earns — used for the (computed) Stakes section and to ground the model
_NEXT = {"R32": "a place in the Round of 16", "R16": "a quarter-final", "QF": "a semi-final",
         "SF": "a place in the final", "3RD": "third place", "Final": "the title"}

_PLACEHOLDER = {"The Matchup": "*Tactical preview unavailable for this tie.*",
                "Key Duel": "*—*", "Watch For": "*—*", "Margin Notes": "*—*"}

SYSTEM = """You write knockout-tie match cards for a daily World Cup hub with a newsprint
voice: prose-forward, precise, a little wry, no hype adjectives, no exclamation marks, no
emoji.

Write EXACTLY these four sections, each under its own '## ' header, in this order:
## The Matchup   — 2-3 paragraphs on how these two specific systems collide.
## Key Duel      — one player-vs-player or player-vs-unit battle that decides the tie.
## Watch For     — a short '- ' bullet list of key players / storylines.
## Margin Notes  — a short '- ' bullet list of trivia.

HARD RULES:
- Use ONLY the two team profiles and the facts in the user message. Every player name,
  system, club, or statistic you write MUST appear there verbatim. Do not add players,
  history, injuries, or numbers that are not provided.
- If you mention any lineup, injury, suspension, or selection question, you MUST tag it
  "(verify before use)".
- This is single-elimination: there is no draw. Frame stakes as win-or-go-home where it fits.
- Output nothing but the four '## ' sections — no preamble, no closing summary, no other
  headings. Do not mention these rules or that you are generated."""


# ---------------------------------------------------------------- facts (computed)

def _when(km) -> str:
    return " · ".join(p for p in [km.date_et, (f"{km.kickoff_et} ET" if km.kickoff_et else "")] if p)


def _venue(km) -> str:
    return ", ".join(p for p in [km.stadium, km.city] if p)


def _stakes_text(km) -> str:
    nxt = _NEXT.get(km.round)
    return f"Win or go home: the victor takes {nxt}." if nxt else ""


def _path_text(km, ko_by_no: dict) -> str:
    """How this tie was reached — feeder results for R16+, the group stage for R32."""
    if km.match_no in bk.BRACKET_TREE:
        bits = []
        for fno in bk.BRACKET_TREE[km.match_no]:
            fm = ko_by_no.get(fno)
            if fm and fm.is_played and fm.winner_team:
                bits.append(f"{fm.winner_team} advanced from M{fno} "
                            f"({fm.team_a} {fm.score_a}–{fm.score_b} {fm.team_b})")
        if bits:
            return "; ".join(bits) + "."
    if km.round == "R32" and km.participants_known:
        return f"{km.team_a} and {km.team_b} arrive straight from the group stage."
    return ""


def build_facts(km, model=None, ko_by_no: dict | None = None, wire: str | None = None) -> dict:
    """The computed fact pack for a tie: schedule, stakes, road here, and (if a model is
    given) the advance call from predict.resolve_knockout. ``wire`` is verified-news
    markdown for the Projected Shapes section. Pure / fail-soft (a model error just omits
    the call)."""
    facts = {"when": _when(km), "venue": _venue(km), "tv": (km.tv_us or "").strip() or "TV TBD",
             "round_name": _KO_ROUND_NAME.get(km.round, km.round),
             "stakes": _stakes_text(km), "path": _path_text(km, ko_by_no or {})}
    if model is not None and km.participants_known:
        try:
            import predict as pr
            kp = pr.resolve_knockout(model, km.team_a, km.team_b)
            pa, pb = round(kp.p_advance_a * 100), round(kp.p_advance_b * 100)
            fav, fp = (km.team_a, pa) if pa >= pb else (km.team_b, pb)
            ms = kp.reg.modal_score
            facts["call"] = (
                f"Model: {fav} to advance ({fp}%). Most likely 90-minute score "
                f"{ms[0]}–{ms[1]}; about {round(kp.p_reach_et * 100)}% reach extra "
                f"time and {round(kp.p_reach_shootout * 100)}% a shootout. "
                "(90' consensus routed through extra time and a coin-flip shootout; neutral venue.)")
        except Exception:
            pass
    if wire:
        facts["wire"] = wire
    return facts


# ---------------------------------------------------------------- generation

def _profile_text(p) -> str:
    bits = []
    if p.tactical:
        bits.append("Tactical preview: " + " ".join(p.tactical))
    if p.key_player:
        bits.append("Key player: " + p.key_player)
    if p.rising_star:
        bits.append("Rising star: " + p.rising_star)
    if p.fun_fact:
        bits.append("Fun fact: " + p.fun_fact)
    if p.squad:
        bits.append("Squad — " + "; ".join(f"{pos}: {names}" for pos, names in p.squad))
    if p.facts:
        bits.append("; ".join(f"{k}: {v}" for k, v in p.facts.items()))
    return "\n".join(bits)


def _grounding_pack(km, pa, pb, facts: dict) -> str:
    rn = facts.get("round_name", km.round)
    out = [f"This is a {rn} knockout tie: {km.team_a} vs {km.team_b}.",
           f"Venue: {facts.get('venue', '')}. {facts.get('stakes', '')}".strip(),
           "", f"=== {km.team_a} profile ===", _profile_text(pa),
           "", f"=== {km.team_b} profile ===", _profile_text(pb), ""]
    if facts.get("path"):
        out += [f"Road here: {facts['path']}", ""]
    if facts.get("call"):
        out += [facts["call"], ""]
    out.append("Write the four sections as instructed, grounded ONLY in the two profiles above.")
    return "\n".join(out)


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")


def _parse_sections(md: str) -> dict:
    """Split a '## header' markdown blob into {header: body}."""
    out, cur, buf = {}, None, []
    for line in md.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def _call_sonnet(client, user: str) -> str:
    resp = client.messages.create(model=MODEL, max_tokens=1600, system=SYSTEM,
                                  messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if not text:
        raise RuntimeError(f"empty card response (stop_reason={getattr(resp, 'stop_reason', None)})")
    return text


def _assemble(km, sections: dict, facts: dict) -> str:
    rn = facts.get("round_name", _KO_ROUND_NAME.get(km.round, km.round))

    def tac(title):
        body = sections.get(title) or _PLACEHOLDER[title]
        return [f"## {title}", body, ""]

    proj = facts.get("wire") or (
        "*Projected lineups and selection questions are not yet verified — refresh from "
        "current team news before kickoff.* **(verify before use)**")
    stakes = " ".join(x for x in [facts.get("stakes", ""), facts.get("path", "")] if x) \
        or "Single-elimination knockout — win or go home."
    parts = [f"# {rn}: {km.team_a} vs {km.team_b}",
             f"**{rn}** | M{km.match_no} | {facts.get('when', '')} | "
             f"{facts.get('venue', '')} | {facts.get('tv', '')}", ""]
    parts += tac("The Matchup")
    parts += tac("Key Duel")
    parts += tac("Watch For")
    parts += ["## Projected Shapes & Selection Questions", proj, ""]
    parts += ["## Stakes", stakes, ""]
    parts += ["## The Call", facts.get("call", "*Advance call pending the ratings layer.*"), ""]
    parts += ["## Odds & Best Bet",
              "*Market snapshot, model-vs-market edge, and the best bet — or \"no bet\". "
              "Populated by the knockout betting layer.*", ""]
    parts += tac("Margin Notes")
    return "\n".join(parts).rstrip() + "\n"


def generate_card(km, profiles: dict, facts: dict | None = None, client=None) -> str | None:
    """Generate a full 9-section knockout card markdown for a RESOLVED tie, or None
    (fail-soft) when it can't be grounded: unresolved matchup, a missing KB profile, no
    client, or an empty/failed model response. Never fabricates."""
    if not km.participants_known:
        return None
    pa, pb = profiles.get(km.team_a), profiles.get(km.team_b)
    if pa is None or pb is None or client is None:
        return None
    facts = facts or build_facts(km)
    try:
        tactical = _call_sonnet(client, _grounding_pack(km, pa, pb, facts))
    except Exception:
        return None
    return _assemble(km, _parse_sections(tactical), facts)


# ---------------------------------------------------------------- persistence

def card_path(match_no: int, cards_dir: Path = CARDS_KO_DIR) -> Path:
    return Path(cards_dir) / f"M{match_no}.md"


def save_card(match_no: int, markdown: str, cards_dir: Path = CARDS_KO_DIR) -> Path:
    cards_dir = Path(cards_dir)
    cards_dir.mkdir(parents=True, exist_ok=True)
    p = card_path(match_no, cards_dir)
    p.write_text(markdown, encoding="utf-8")
    return p


def load_ko_card(match_no: int, cards_dir: Path = CARDS_KO_DIR) -> str | None:
    """The persisted card markdown for a knockout match, or None if not generated yet."""
    p = card_path(match_no, cards_dir)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


# ---------------------------------------------------------------- CLI

def _near(km, target: date, window: int = 2) -> bool:
    try:
        d = date.fromisoformat(km.date_et)
    except ValueError:
        return False
    return abs((d - target).days) <= window


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate knockout cards for resolved, unplayed ties.")
    ap.add_argument("date", nargs="?", help="editorial date YYYY-MM-DD (default today ET)")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    ap.add_argument("--knockout", type=Path, default=REPO_ROOT / "data" / "knockout.csv")
    ap.add_argument("--kb", type=Path, default=REPO_ROOT / "kb" / "2026_fifa_world_cup_guide.md")
    ap.add_argument("--cards-dir", type=Path, default=CARDS_KO_DIR)
    ap.add_argument("--window", type=int, default=2, help="days around DATE to generate for")
    ap.add_argument("--force", action="store_true", help="regenerate even if a card exists")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    matches = ko.load_knockout(args.knockout)
    if not matches:
        print("no knockout schedule yet — nothing to generate.")
        return 0
    try:
        target = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2

    import site_content as sc
    fixtures = st.load_fixtures(args.fixtures)
    standings = st.compute_standings(fixtures, fair_play=st.load_discipline())
    matches = ko.materialize_teams(bk.project(standings), matches)
    ko_by_no = ko.by_no(matches)
    profiles, _ = sc.parse_kb(args.kb)

    try:
        import anthropic
        client = anthropic.Anthropic()
    except Exception as e:
        print(f"error: anthropic unavailable ({e.__class__.__name__}) — cards skipped, "
              "site renders placeholders.", file=sys.stderr)
        return 1
    try:
        import predict as pr
        model = pr.load_ratings()
    except Exception:
        model = None

    made = 0
    for km in matches:
        if km.is_played or not km.participants_known or not _near(km, target, args.window):
            continue
        if not args.force and load_ko_card(km.match_no, args.cards_dir):
            continue
        facts = build_facts(km, model=model, ko_by_no=ko_by_no)
        card = generate_card(km, profiles, facts, client=client)
        if card is None:
            print(f"M{km.match_no}: card skipped (ungroundable — placeholder will show)")
            continue
        save_card(km.match_no, card, args.cards_dir)
        made += 1
        print(f"M{km.match_no}: wrote card ({km.team_a} vs {km.team_b})")
    print(f"{made} knockout card(s) generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
