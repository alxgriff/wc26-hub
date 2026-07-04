#!/usr/bin/env python3
"""As-it-stands 2026 World Cup knockout-bracket projector.

Projects the Round of 32 -> Final bracket AS IF the current group standings were
final. The 2026 bracket is a pure, draw-free function of final group positions +
a predetermined fixture template (matches 73-104) + Annex C of the FIFA WC2026
regulations (the 495-combination third-place table; see data/annex_c.csv).

GATED, per the feature brief: a group with no games played renders as abstract
slots ("Winner E", "Runner-up F"); the eight "group winner vs best third" matches
resolve only once ALL 12 groups have a standing (so the cross-group third-place set
is meaningful) and the third-place cutline isn't flagged provisional. Strictly an
"as it stands" scenario tool — it shows WHO would land WHERE in the R32 and the
fixed paths beyond; it does NOT predict match winners (the tree shows the shape).

Reuses scripts/standings.py (group tables + the best-8 third ranking, already
ordered to the 2026 tiebreakers). Structural only; no model/prediction. Pure
functions, no I/O beyond loading the committed Annex C table.

CLI:  python scripts/bracket.py [--fixtures data/fixtures.csv]
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import standings as st  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
ANNEX_C = REPO_ROOT / "data" / "annex_c.csv"

# --- R32 fixture template (matches 73-88). Slot is ("W", g) | ("RU", g) | ("3RD", pool)
R32_TEMPLATE: dict[int, tuple] = {
    73: (("RU", "A"), ("RU", "B")),
    74: (("W", "E"),  ("3RD", "ABCDF")),
    75: (("W", "F"),  ("RU", "C")),
    76: (("W", "C"),  ("RU", "F")),
    77: (("W", "I"),  ("3RD", "CDFGH")),
    78: (("RU", "E"), ("RU", "I")),
    79: (("W", "A"),  ("3RD", "CEFHI")),
    80: (("W", "L"),  ("3RD", "EHIJK")),
    81: (("W", "D"),  ("3RD", "BEFIJ")),
    82: (("W", "G"),  ("3RD", "AEHIJ")),
    83: (("RU", "K"), ("RU", "L")),
    84: (("W", "H"),  ("RU", "J")),
    85: (("W", "B"),  ("3RD", "EFGIJ")),
    86: (("W", "J"),  ("RU", "H")),
    87: (("W", "K"),  ("3RD", "DEIJL")),
    88: (("RU", "D"), ("RU", "G")),
}
# matches that host a best-third, and the winner-slot column (in annex_c) that names its third
THIRD_HOSTING = tuple(m for m, (_a, b) in R32_TEMPLATE.items() if b[0] == "3RD")
THIRD_MATCH_WINNER = {m: "1" + a[1] for m, (a, b) in R32_TEMPLATE.items() if b[0] == "3RD"}

# --- Bracket tree: each later match is the winners of two earlier matches.
BRACKET_TREE: dict[int, tuple[int, int]] = {
    89: (74, 77), 90: (73, 75), 91: (76, 78), 92: (79, 80),
    93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87),
    97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96),
    101: (97, 98), 102: (99, 100),
    104: (101, 102),            # Final
}
THIRD_PLACE_MATCH = 103         # losers of 101 & 102
ROUND_NAME = {32: "Round of 32", 16: "Round of 16", 8: "Quarter-finals",
              4: "Semi-finals", 2: "Final"}


def load_annex_c(path: Path = ANNEX_C) -> dict[tuple[str, ...], dict[int, str]]:
    """{sorted 8-group combo: {r32_match_no: group whose third fills it}}, loaded
    from data/annex_c.csv with integrity checks. Stop-and-report on any violation."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — regenerate it with scripts/parse_annex_c.py "
            "(FIFA WC2026 Annex C, via the public Wikipedia template).")
    out: dict[tuple[str, ...], dict[int, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            combo = tuple(sorted(row["combo"]))
            assign = {m: row[THIRD_MATCH_WINNER[m]] for m in THIRD_HOSTING}
            if tuple(sorted(assign.values())) != combo:
                raise ValueError(f"annex_c {row['combo']}: thirds not a permutation of the combo")
            for m, g in assign.items():
                if g not in R32_TEMPLATE[m][1][1]:        # respects the slot's candidate pool
                    raise ValueError(f"annex_c {row['combo']}: 3{g} not eligible for match {m} "
                                     f"(pool {R32_TEMPLATE[m][1][1]})")
            out[combo] = assign
    if len(out) != 495:
        raise ValueError(f"annex_c.csv: expected 495 combinations, got {len(out)}")
    return out


def _group_started(gt: "st.GroupTable") -> bool:
    return any(r.played > 0 for r in gt.rows)


def _cutline_ambiguous(third_place: "list[st.TeamRow]") -> bool:
    """Is the SET of 8 qualifying thirds genuinely undecided? Only when a tie straddles the
    8th/9th boundary — i.e. the 8th and 9th teams are level on the PRIMARY modelled criteria
    (points, GD, goals scored), so which of them takes the last slot is unresolved (it falls to
    the unmodelled FIFA ranking). A tie ENTIRELY inside the top 8 (both qualify) or entirely
    below it (neither qualifies) doesn't change the set, so it must not gate the bracket. With
    fewer than 9 thirds there is no boundary to straddle. (Conservative on the rare fair-play
    edge: if the 8th/9th are level on points/GD/goals but fair-play separates them, this still
    reports ambiguous — the safe direction, never a wrong resolve.)"""
    if len(third_place) <= st.QUALIFYING_THIRDS:
        return False
    a = third_place[st.QUALIFYING_THIRDS - 1]      # 8th — last in
    b = third_place[st.QUALIFYING_THIRDS]          # 9th — first out
    return (a.points, a.gd, a.gf) == (b.points, b.gd, b.gf)


def _slot_label(kind: str, g: str) -> str:
    return {"W": f"Winner {g}", "RU": f"Runner-up {g}"}[kind]


def project(standings: "st.Standings", annex: dict | None = None,
            resolve_provisional: bool = False, clinched: dict | None = None) -> dict:
    """Project the R32->Final bracket from current standings (as if final). Returns a
    pure dict (see to_dict for the shape). Gated: unstarted groups -> abstract slots;
    third-hosting matches resolve only when all 12 groups have a standing and the
    third-place cutline is not flagged provisional. ``resolve_provisional=True`` (the
    'projected finish' view) breaks a provisional cutline deterministically — using the
    standings' own order (alphabetical on a modelled tie) — so the full bracket resolves
    for the scenario; the as-it-stands view leaves it False and stays honestly gated.

    ``clinched`` is an optional {group_letter: {team: clinched_rank}} map (from
    scenarios.clinched_ranks, computed with the full 2026 tiebreakers). When supplied it
    drives the per-side ``home_confirmed``/``away_confirmed`` flags: a slot is CONFIRMED
    when its occupant has mathematically secured that exact seed and so cannot be
    reshuffled by any remaining result. It is only ever read for the honest as-it-stands
    view; the hypothetical 'projected finish' view passes None, so nothing there is ever
    marked confirmed (a projection clinches nothing)."""
    annex = annex if annex is not None else load_annex_c()
    groups = standings.groups
    warnings: list[str] = list(standings.warnings)
    groups_complete = (len(groups) == 12 and all(
        r.played >= st.GAMES_PER_TEAM for gt in groups.values() for r in gt.rows))

    def team_or_label(kind: str, g: str):
        """(team | None, label, provisional?, confirmed?) for a Winner/Runner-up slot.
        confirmed = the occupant has clinched this exact seed (rank 1 for a winner, 2 for
        a runner-up) per the full tiebreakers — only ever True when a clinch map is given."""
        gt = groups.get(g)
        idx = 0 if kind == "W" else 1
        if gt is None or not _group_started(gt) or len(gt.rows) <= idx:
            return None, _slot_label(kind, g), True, False
        team = gt.rows[idx].team
        prov = gt.rows[idx].played < st.GAMES_PER_TEAM
        confirmed = bool(clinched and clinched.get(g, {}).get(team) == idx + 1)
        return team, _slot_label(kind, g), prov, confirmed

    # --- can the 8 third-hosting matches be resolved?
    all_started = len(groups) == 12 and all(_group_started(gt) for gt in groups.values())
    # The bracket depends ONLY on the SET of 8 qualifying thirds (Annex C keys on the set of
    # group letters, not their order). So gate on whether a tie STRADDLES the 8th/9th boundary
    # — not on any provisional note: an order-only tie WITHIN the top 8 (both teams qualify
    # regardless of who's ranked ahead) leaves the set, and thus the whole bracket, determined.
    cutline_provisional = _cutline_ambiguous(standings.third_place)
    third_assign: dict[int, str] = {}
    thirds_resolved = False
    if all_started and len(standings.third_place) >= st.QUALIFYING_THIRDS:
        qualifying = standings.third_place[:st.QUALIFYING_THIRDS]
        combo = tuple(sorted(r.group for r in qualifying))
        if combo in annex and (resolve_provisional or not cutline_provisional):
            third_assign = annex[combo]
            thirds_resolved = True
            if cutline_provisional and resolve_provisional:
                warnings.append("Third-place cutline has ties broken deterministically (FIFA World "
                                "Ranking isn't modelled, so the standings' order decides) — the "
                                "qualifying thirds and their slots are provisional and may change.")
        elif cutline_provisional:
            warnings.append("Third-place cutline is provisional (teams level on all "
                            "modelled criteria) — the eight winner-vs-third matches are not resolved.")

    def third_slot(match_no: int):
        pool = R32_TEMPLATE[match_no][1][1]
        if thirds_resolved:
            g = third_assign[match_no]
            gt = groups.get(g)
            row = gt.rows[2] if gt and len(gt.rows) > 2 else None
            team = row.team if row else None
            # a third-place slot stays provisional until the WHOLE group stage is done:
            # even a team that's finished its games can be reshuffled out of (or into) the
            # qualifying-8 set by other groups' results, which re-slots Annex C entirely.
            prov = (not groups_complete) if team else True
            confirmed = bool(team and clinched and groups_complete
                             and not cutline_provisional
                             and clinched.get(g, {}).get(team) == 3)
            return team, f"3rd {g}", prov, confirmed
        return None, "Best 3rd of " + "/".join(pool), True, False

    r32 = {}
    for m, (a, b) in R32_TEMPLATE.items():
        sa = team_or_label(a[0], a[1]) if a[0] != "3RD" else third_slot(m)
        sb = team_or_label(b[0], b[1]) if b[0] != "3RD" else third_slot(m)
        prov = bool(sa[2] or sb[2] or sa[0] is None or sb[0] is None)
        half = "top" if m in _TOP_HALF_R32 else "bottom"
        r32[m] = {"match": m, "home": sa[0], "home_label": sa[1],
                  "away": sb[0], "away_label": sb[1], "provisional": prov, "half": half,
                  # per-side: a CONCRETE team whose group position isn't sealed yet (can change)
                  "home_provisional": bool(sa[0] is not None and sa[2]),
                  "away_provisional": bool(sb[0] is not None and sb[2]),
                  # per-side: this exact seed is mathematically secured (clinch map supplied)
                  "home_confirmed": sa[3], "away_confirmed": sb[3]}

    return {
        "schema": 1,
        "as_of_matches_played": standings.played,
        "fully_projectable": all_started and thirds_resolved,
        "thirds_resolved": thirds_resolved,
        "r32": r32,
        "tree": {m: list(pair) for m, pair in BRACKET_TREE.items()},
        "third_place_match": THIRD_PLACE_MATCH,
        "warnings": warnings,
    }


# the eight R32 matches feeding semi-final 101 (top) vs 102 (bottom)
_TOP_HALF_R32 = frozenset({73, 74, 75, 77, 81, 82, 83, 84})


def project_final_standings(matches: "list[st.Match]", score_fn) -> "st.Standings":
    """Standings as if every remaining group game were played to the score ``score_fn``
    predicts — so ``project`` can resolve the WHOLE bracket (all 12 groups final, thirds
    slotted). ``score_fn(match) -> (score_a, score_b)`` is injected, so bracket.py stays
    model-agnostic (build_site passes a predict-based one; tests a deterministic one).
    A pure projection: it never touches fixtures.csv or the live as-it-stands standings."""
    import dataclasses
    projected = [m if m.is_played
                 else dataclasses.replace(m, status="played",
                                          score_a=(sc := score_fn(m))[0], score_b=sc[1])
                 for m in matches]
    return st.compute_standings(projected)


def _poisson_sample(lam: float, rng) -> int:
    """A goal count drawn from rate ``lam`` (Knuth's algorithm, stdlib only)."""
    import math
    target = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= target:
            return k
        k += 1


def project_modal_standings(matches: "list[st.Match]", rates_fn,
                            n_sims: int = 2000, seed: int = 20260625) -> "st.Standings":
    """Standings projected by SIMULATING the remaining group games (draws included) and
    taking, per group, the model's MOST LIKELY winner plus the most likely final ordering
    GIVEN that winner. A drop-in for project_final_standings in the 'projected finish'
    bracket — returns a Standings.

    Why not project_final_standings: that forces every remaining game to its single most-
    likely DECISIVE result and chains them, which can crown a group winner that isn't even
    the model's most likely one — it discards draws (so a leader who only needs a point can
    be dropped) and forces a near-even game to the marginal favourite. Here each remaining
    game is sampled from its modelled goal rates, the whole group is ranked by the real 2026
    tiebreakers per simulation (compute_standings — head-to-head and all), and the modal
    winner wins; a near-expected-margin representative of that ordering supplies concrete
    scorelines so the cross-group third-place cutline stays coherent. ``rates_fn(match) ->
    (lambda_a, lambda_b)`` is injected so bracket.py stays model-agnostic (build_site passes
    a predict-based one; tests a deterministic one). Deterministic (seeded), pure (never
    touches fixtures.csv)."""
    import dataclasses
    import random as _random
    from collections import Counter
    rng = _random.Random(seed)
    fp = st.load_discipline()
    chosen: dict[str, tuple] = {}      # match_id -> (score_a, score_b) for the unplayed games
    for g in sorted({m.group for m in matches}):
        gms = [m for m in matches if m.group == g]
        unplayed = [m for m in gms if not m.is_played]
        if not unplayed:
            continue
        played_g = [m for m in gms if m.is_played]
        rates = {m.match_id: rates_fn(m) for m in unplayed}
        tally: dict[tuple, list] = {}                # ordering -> [count, repr scores, deviation]
        for _ in range(n_sims):
            scores, dev = {}, 0.0
            for m in unplayed:
                la, lb = rates[m.match_id]
                sa, sb = _poisson_sample(la, rng), _poisson_sample(lb, rng)
                scores[m.match_id] = (sa, sb)
                dev += abs(sa - la) + abs(sb - lb)       # distance from the expected scoreline
            proj = played_g + [dataclasses.replace(m, status="played",
                                  score_a=scores[m.match_id][0], score_b=scores[m.match_id][1])
                               for m in unplayed]
            order = tuple(r.team for r in st.compute_standings(proj, fair_play=fp).groups[g].rows)
            slot = tally.get(order)
            if slot is None:
                tally[order] = [1, scores, dev]
            else:
                slot[0] += 1
                if dev < slot[2]:                        # keep the most typical-margin sample
                    slot[1], slot[2] = scores, dev
        # the model's most likely WINNER (summed across orderings), then the modal ordering
        # that produces it — so the winner is never a non-modal artefact of forced results.
        winners = Counter()
        for order, slot in tally.items():
            winners[order[0]] += slot[0]
        modal_winner = winners.most_common(1)[0][0]
        modal = max((o for o in tally if o[0] == modal_winner), key=lambda o: tally[o][0])
        chosen.update(tally[modal][1])
    projected = [m if m.is_played
                 else dataclasses.replace(m, status="played",
                                          score_a=chosen[m.match_id][0], score_b=chosen[m.match_id][1])
                 for m in matches]
    return st.compute_standings(projected, fair_play=fp)


def feed(projection: dict, resolver, results: "dict | None" = None) -> dict:
    """Propagate winners through the bracket tree, filling downstream rounds.

    ``resolver(team_a, team_b) -> {"winner", "loser", "p"} | None`` decides a single
    knockout tie (the model lives outside this module — build_site injects one built
    on predict.resolve_knockout; tests inject a deterministic one, so this stays
    model-agnostic). ``results`` optionally maps a match number to the winning team
    name; an actual result overrides the model (the hook for real knockout results
    once those fixtures exist). Only ties with BOTH sides concretely known are
    resolved, so projection halts at the gating frontier — an abstract 'Winner E'
    feeder blocks everything above it, exactly as it stands.

    Returns a NEW projection dict with two added keys: ``winners`` {match: {team,
    loser, p, source}} and ``participants`` {match: [team_a, team_b]}, plus
    ``champion`` (the projected Final winner, or None). The input is not mutated."""
    results = results or {}
    tree = {int(k): tuple(v) for k, v in projection["tree"].items()}
    r32 = {int(k): v for k, v in projection["r32"].items()}
    winners: dict[int, dict] = {}
    participants: dict[int, list] = {}

    def decide(m: int, a, b) -> None:
        if not a and not b:
            return
        # record participants even when only ONE side is known, so a team that has won its
        # tie advances onto the next round's line immediately (the other slot reads TBD) —
        # like a real bracket. A winner is only resolved once BOTH sides are known.
        participants[m] = [a, b]
        if not a or not b:
            return
        if m in results:
            w = results[m]
            winners[m] = {"team": w, "loser": b if w == a else a,
                          "p": None, "source": "result"}
            return
        r = resolver(a, b)
        if r and r.get("winner"):
            winners[m] = {"team": r["winner"], "loser": r.get("loser"),
                          "p": r.get("p"), "source": "model"}

    for m in range(73, 89):                      # R32 from resolved group positions
        decide(m, r32[m]["home"], r32[m]["away"])
    for m in sorted(tree):                        # R16->Final; feeders are lower-numbered
        a = winners.get(tree[m][0], {}).get("team")
        b = winners.get(tree[m][1], {}).get("team")
        decide(m, a, b)
    tp = projection["third_place_match"]          # losers of the two semi-finals
    decide(tp, winners.get(101, {}).get("loser"), winners.get(102, {}).get("loser"))

    out = dict(projection)
    out["winners"] = winners
    out["participants"] = participants
    out["champion"] = winners.get(max(tree), {}).get("team")
    return out


def render_markdown(projection: dict, ko_by_no: "dict | None" = None) -> str:
    """``ko_by_no`` ({match_no: KnockoutMatch}, e.g. knockout.by_no) annotates a PLAYED
    R32 tie with its result and who advanced — without it every slot renders as an open
    matchup even after the round is history."""
    out = ["# Knockout bracket — as it stands", ""]
    n = projection["as_of_matches_played"]
    out.append(f"*Projection from the current group standings as if they were final "
               f"({n}/72 group matches played). Provisional — slots fill in as groups "
               f"play; “Best 3rd of {{…}}” resolves once all 12 groups have a standing.*")
    out.append("")
    if not projection["thirds_resolved"]:
        out.append("> ⚠️ The eight *winner-vs-best-third* matches are not yet resolved "
                   "(the cross-group third-place set isn't settled).")
        out.append("")

    def line(m):
        km = (ko_by_no or {}).get(m)
        if km is not None and km.is_played and km.winner_team:
            tag = {"extra_time": " AET", "penalties": " pens"}.get(km.decided_by, "")
            return (f"- **M{m}:** {km.team_a} {km.score_a}–{km.score_b} "
                    f"{km.team_b}{tag} — **{km.winner_team} advance**")
        e = projection["r32"][m]
        h = e["home"] or f"*{e['home_label']}*"
        a = e["away"] or f"*{e['away_label']}*"
        flag = " ·provisional" if e["provisional"] else ""
        return f"- **M{m}:** {h} vs {a}{flag}"

    for half, title in (("top", "Top half → Semi-final 1"), ("bottom", "Bottom half → Semi-final 2")):
        out.append(f"## {title}")
        for m in sorted(projection["r32"]):
            if projection["r32"][m]["half"] == half:
                out.append(line(m))
        out.append("")

    for w in projection["warnings"]:
        out.append(f"_{w}_")
    return "\n".join(out).rstrip() + "\n"


def to_dict(projection: dict) -> dict:
    return projection   # already a pure, JSON-safe dict


def main(argv: list | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Project the as-it-stands WC26 knockout bracket.")
    ap.add_argument("--fixtures", type=Path, default=REPO_ROOT / "data" / "fixtures.csv")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    matches = st.load_fixtures(args.fixtures)
    standings = st.compute_standings(matches, fair_play=st.load_discipline())
    proj = project(standings)
    print(render_markdown(proj))
    for w in proj["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
