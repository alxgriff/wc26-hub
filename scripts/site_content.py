#!/usr/bin/env python3
"""Content parsers for the WC26 static site.

Three concerns, no rendering decisions:

  * parse_kb()    — the 48 team profiles out of kb/2026_fifa_world_cup_guide.md
  * parse_card()  — a match card (any of the three repo formats) into
                    ordered (section_label, body_markdown) pairs
  * md_to_html()  — the constrained markdown dialect the cards/kb use
                    (bold, italics, bullet lists, paragraphs) to HTML,
                    escape-first so card prose can never inject markup

Plus slugify() for stable team-page filenames. Parsing is strict about the
team-name canon: parse_kb() reports missing teams rather than guessing.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# Order matters: this is the display order on match pages.
CARD_SECTIONS = (
    "The Matchup", "Recap notes for the hub", "Key Duel", "Watch For",
    "Shapes & Selection", "Projected Shapes & Selection Questions",
    "Stakes", "The Call", "Odds & Best Bet", "The Call / Odds & Best Bet",
    "Margin Notes",
)


def slugify(name: str) -> str:
    """Canon team name -> stable ascii filename slug.
    'Côte d'Ivoire' -> 'cote-divoire', 'Türkiye' -> 'turkiye'."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s


# ---------------------------------------------------------------- kb profiles

@dataclass
class TeamProfile:
    team: str
    group: str
    facts: dict[str, str] = field(default_factory=dict)   # Manager, Captain, ...
    squad: list[tuple[str, str]] = field(default_factory=list)  # (position, names)
    tactical: list[str] = field(default_factory=list)     # paragraphs
    key_player: str = ""
    rising_star: str = ""
    fun_fact: str = ""


_FACT_SPLIT_RE = re.compile(r"\*\*([^*]+?):\*\*")
_GROUP_RE = re.compile(r"^# Group ([A-L])\s*$")
_TEAM_RE = re.compile(r"^## (.+?)\s*$")


def parse_kb(path: str | Path) -> tuple[dict[str, TeamProfile], list[str]]:
    """Parse the kb guide into {canon_team: TeamProfile}. Returns
    (profiles, warnings); warnings flag structural surprises rather than
    silently absorbing them."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    profiles: dict[str, TeamProfile] = {}
    warnings: list[str] = []

    group = None
    i = 0
    while i < len(lines):
        gm = _GROUP_RE.match(lines[i])
        if gm:
            group = gm.group(1)
            i += 1
            continue
        tm = _TEAM_RE.match(lines[i])
        if tm and group:
            # slice this team's block
            j = i + 1
            while j < len(lines) and not (lines[j].startswith("## ")
                                          or lines[j].startswith("# ")):
                j += 1
            profile = _parse_team_block(tm.group(1), group, lines[i + 1:j], warnings)
            profiles[profile.team] = profile
            i = j
            continue
        i += 1

    return profiles, warnings


def _parse_team_block(team: str, group: str, block: list[str],
                      warnings: list[str]) -> TeamProfile:
    p = TeamProfile(team=team, group=group)
    para: list[str] = []
    mode = None  # None | "squad" | "tactical"

    def flush_para():
        nonlocal para
        if para:
            p.tactical.append(" ".join(para))
            para = []

    for raw in block:
        line = raw.rstrip()
        s = line.strip()
        if not s or re.fullmatch(r"-{3,}", s):
            if mode == "tactical":
                flush_para()
            continue

        if s.startswith("**Squad by position:**"):
            mode = "squad"
            continue
        m = re.match(r"\*\*(Tactical preview|Key player|Rising star|Fun fact):\*\*\s*(.*)",
                     s)
        if m:
            label, rest = m.group(1), m.group(2)
            if label == "Tactical preview":
                mode = "tactical"
                para = [rest] if rest else []
            else:
                mode = None
                flush_para()
                setattr(p, label.lower().replace(" ", "_"), rest)
            continue

        if mode == "squad":
            sm = re.match(r"-\s*([^:]+):\s*(.+)", s)
            if sm:
                p.squad.append((sm.group(1).strip(), sm.group(2).strip()))
            else:
                warnings.append(f"{team}: unparsed squad line: {s[:60]!r}")
            continue
        if mode == "tactical":
            para.append(s)
            continue

        # strap fact lines: **Manager:** X | **Captain:** Y | ...
        # Split on the bold labels so values may themselves contain '|'
        # (e.g. "18 appearances (1930–2026) | Recent finish: ...").
        if s.startswith("**"):
            parts = _FACT_SPLIT_RE.split(s)
            for label, value in zip(parts[1::2], parts[2::2]):
                p.facts[label.strip()] = value.strip().strip("|").strip()
            continue
        warnings.append(f"{team}: unparsed line: {s[:60]!r}")

    flush_para()
    if not p.tactical:
        warnings.append(f"{team}: no tactical preview parsed")
    if not p.squad:
        warnings.append(f"{team}: no squad parsed")
    return p


# ---------------------------------------------------------------- match cards

_INLINE_LABEL_RE = re.compile(r"^\*\*([^*]+?)(?: \(as previewed\))?:\*\*\s*(.*)$")
_H2_SECTION_RE = re.compile(r"^## (.+?)\s*$")


def parse_card(card_text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split an extracted card into (header_line, [(section, body_md), ...]).

    Handles all three repo formats:
      A. md1/md2:    '## A2: ...' header, '**Label:** body' sections
      B. md3:        '### B5: ...' header, same inline labels
      C. template.md: '# Team vs Team' header, '## Label' section headers
    Unknown labels are kept (order preserved) so no card prose is dropped.
    """
    lines = card_text.splitlines()
    if not lines:
        return "", []
    header = lines[0].lstrip("#").strip()
    body = lines[1:]

    if lines[0].startswith("# ") and not lines[0].startswith("## "):
        return header, _split_h2_sections(body)
    return header, _split_inline_sections(body)


def _split_inline_sections(body: list[str]) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    preamble: list[str] = []
    for line in body:
        m = _INLINE_LABEL_RE.match(line.strip())
        if m and (m.group(1) in CARD_SECTIONS or m.group(1).rstrip(":") in CARD_SECTIONS):
            current = [m.group(2)] if m.group(2) else []
            sections.append((m.group(1), current))
        elif current is not None:
            current.append(line)
        elif line.strip():
            preamble.append(line)
    out = []
    if preamble:
        out.append(("", "\n".join(preamble).strip()))
    out.extend((label, "\n".join(chunk).strip()) for label, chunk in sections)
    return out


def _split_h2_sections(body: list[str]) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    preamble: list[str] = []
    for line in body:
        m = _H2_SECTION_RE.match(line)
        if m:
            current = []
            sections.append((m.group(1), current))
        elif current is not None:
            current.append(line)
        elif line.strip():
            preamble.append(line)
    out = []
    if preamble:
        out.append(("", "\n".join(preamble).strip()))
    out.extend((label, "\n".join(chunk).strip()) for label, chunk in sections)
    return out


# ---------------------------------------------------------------- mini markdown

_BOLD_ITALIC_RE = re.compile(r"\*\*\*([^*\n]+?)\*\*\*")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")


def _inline(text: str) -> str:
    """Escape, then apply the only inline marks the cards use. *** is handled
    first so bold/italic passes can't mis-nest each other's tags."""
    out = html.escape(text, quote=False)
    out = _BOLD_ITALIC_RE.sub(r"<strong><em>\1</em></strong>", out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    return out


def md_to_html(text: str) -> str:
    """The cards' constrained markdown -> HTML: paragraphs, '- ' bullet
    lists, '> ' asides, **bold**, *italic*. Everything is HTML-escaped
    before any markup is applied."""
    blocks: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para():
        if para:
            blocks.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_bullets():
        if bullets:
            items = "".join(f"<li>{_inline(b)}</li>" for b in bullets)
            blocks.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s in ("---", "------"):
            flush_para()
            flush_bullets()
            continue
        if s.startswith("- "):
            flush_para()
            bullets.append(s[2:])
        elif s.startswith("> "):
            flush_para()
            flush_bullets()
            blocks.append(f"<blockquote><p>{_inline(s[2:])}</p></blockquote>")
        else:
            flush_bullets()
            para.append(s)
    flush_para()
    flush_bullets()
    return "\n".join(blocks)
