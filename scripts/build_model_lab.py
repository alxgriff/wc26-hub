#!/usr/bin/env python3
"""Regenerate docs/model-lab.html — the experimental model comparison page.

Shows every available forecaster on all played WC26 matches side-by-side
with RPS scorecards and per-match probability bars.

Models included (each inert when its artifact is absent):
  #1  Structural           — production model (Elo + Futi, Poisson matrix)
  #2+ Structural variants  — all entries in data/calibration/struct_variant.json

Usage:
    python3 scripts/build_model_lab.py [--out docs/model-lab.html]
"""
from __future__ import annotations

import argparse
import csv
import html
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import predict as pr                    # noqa: E402
import struct_variant as sv_mod         # noqa: E402


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def rps(probs: tuple[float, float, float], outcome_i: int) -> float:
    """Ranked Probability Score — lower is better (0 = perfect, 0.5 = coin flip)."""
    cum_p = 0.0
    score = 0.0
    for i, p in enumerate(probs):
        cum_p += p
        cum_o = 1.0 if i >= outcome_i else 0.0
        score += (cum_p - cum_o) ** 2
    return score / (len(probs) - 1)


def _outcome(sa: int, sb: int) -> int:
    """0 = home win, 1 = draw, 2 = away win."""
    return 0 if sa > sb else (2 if sa < sb else 1)


def _pct(p: float) -> int:
    return max(round(p * 100), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "model-lab.html"))
    args = ap.parse_args()

    # ---- load model ----
    model = pr.load_ratings()

    # ---- played matches ----
    rows = []
    with (REPO / "data" / "fixtures.csv").open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("status", "").strip().lower() == "played":
                rows.append(r)

    # ---- struct_variant list ----
    sv_variants = sv_mod._load_config() or []
    sv_tuned_list = []
    if sv_variants:
        sv_tuned_list = sv_mod._get_tuned_list(model, sv_variants)

    # ---- per-model predictions ----
    # Each forecaster is (label, fn) where fn(row) -> (p_a, p_draw, p_b) | None
    def _struct(row):
        host = pr.HOST_BY_COUNTRY.get((row.get("country") or "").strip())
        a, b = row["team_a"].strip(), row["team_b"].strip()
        hfa = host if host in (a, b) else None
        p = pr.predict_match(model, a, b, hfa_team=hfa)
        return (p.p_a, p.p_draw, p.p_b)

    # forecasters: list of (label, fn, group)
    # group=None for pre-tournament models; "post-md1" for in-tournament fitted ones.
    forecasters = [("Structural", _struct, None)]

    # Structural variants
    for cfg, tuned in sv_tuned_list:
        label = str(cfg.get("label", "Structural (tuned)"))
        group = cfg.get("group") or None
        def _sv_fn(row, _tuned=tuned):
            host = pr.HOST_BY_COUNTRY.get((row.get("country") or "").strip())
            a, b = row["team_a"].strip(), row["team_b"].strip()
            hfa = host if host in (a, b) else None
            p = pr.predict_match(_tuned, a, b, hfa_team=hfa)
            return (p.p_a, p.p_draw, p.p_b)
        forecasters.append((label, _sv_fn, group))

    # ---- compute results ----
    # match_results[row_i] = [(probs, rps_val) | None, ...]  indexed by forecaster
    # post-md1 models are suppressed for MD1 matches (their ratings were fitted on that data)
    match_results: list[list[tuple | None]] = []
    for row in rows:
        sa, sb = int(row["score_a"]), int(row["score_b"])
        oi = _outcome(sa, sb)
        matchday = int(row.get("matchday", 1))
        per_fc = []
        for label, fn, group in forecasters:
            if group == "post-md1" and matchday == 1:
                per_fc.append(None)   # poisoned — ratings trained on this data
                continue
            try:
                probs = fn(row)
                if probs is None:
                    per_fc.append(None)
                else:
                    per_fc.append((probs, rps(probs, oi)))
            except Exception:
                per_fc.append(None)
        match_results.append(per_fc)

    # ---- scoreboard stats ----
    n_matches = len(rows)
    stats = []  # [(label, mean_rps, hits, n_valid, group)]
    struct_rps = None
    for fc_i, (label, _, group) in enumerate(forecasters):
        rps_vals = [match_results[mi][fc_i][1]
                    for mi in range(n_matches)
                    if match_results[mi][fc_i] is not None]
        n_valid = len(rps_vals)
        mean = sum(rps_vals) / n_valid if n_valid else None

        probs_list = [match_results[mi][fc_i][0]
                      for mi in range(n_matches)
                      if match_results[mi][fc_i] is not None]
        hit = 0
        for mi, row in enumerate(rows):
            fc_data = match_results[mi][fc_i]
            if fc_data is None:
                continue
            probs, _ = fc_data
            sa, sb = int(row["score_a"]), int(row["score_b"])
            oi = _outcome(sa, sb)
            if probs.index(max(probs)) == oi:
                hit += 1

        stats.append((label, mean, hit, n_valid, group))
        if fc_i == 0:
            struct_rps = mean

    best_rps = min((s[1] for s in stats if s[1] is not None), default=None)

    # ---- HTML generation ----
    # Short label for match bars (keep width manageable)
    _short = {
        "Structural": "struct",
        "Structural (conf-tuned)": "conf-tuned",
        "100% Futi (no Elo)": "futi-only",
        "Structural (post-MD1 fitted)": "post·struct",
        "100% Futi (post-MD1 fitted)": "post·futi",
    }

    def short(label):
        return _short.get(label, label[:9].lower())

    POST_MD1_SECTION_HTML = (
        '<div class="section-sep">'
        '<span class="section-sep-line"></span>'
        '<span class="section-sep-label">Post-MD1 Fitted</span>'
        '<span class="section-sep-line"></span>'
        '</div>'
    )

    # Scoreboard cards
    card_html = []
    sep_inserted = False
    for fc_i, (label, mean_rps, hits, n_valid, group) in enumerate(stats):
        if group == "post-md1" and not sep_inserted:
            card_html.append(POST_MD1_SECTION_HTML)
            sep_inserted = True
        if mean_rps is None:
            continue
        is_best = (mean_rps == best_rps)
        if fc_i == 0 or struct_rps is None:
            delta_html = '<span class="delta">—</span>'
        else:
            delta_pct = (mean_rps - struct_rps) / struct_rps * 100
            cls = "good" if delta_pct < -0.5 else ("bad" if delta_pct > 0.5 else "")
            sign = "+" if delta_pct >= 0 else ""
            delta_html = f'<span class="delta {cls}">{sign}{delta_pct:.1f}% vs structural</span>'
        card_html.append(
            f'<div class="card{"  best" if is_best else ""}">'
            f'<div class="nm">#{fc_i + 1} {_esc(label)}</div>'
            f'<div class="rps">{mean_rps:.4f}</div>'
            f'<div class="sub">mean RPS · {delta_html}</div>'
            f'<div class="sub">hit rate {hits}/{n_valid}</div>'
            f'</div>')

    # Match bars
    match_html = []
    current_matchday = None
    for mi, row in enumerate(rows):
        matchday = int(row.get("matchday", 1))
        if current_matchday is not None and matchday != current_matchday:
            match_html.append(
                f'<div class="section-sep" style="margin:22px 0 14px">'
                f'<span class="section-sep-line"></span>'
                f'<span class="section-sep-label">Matchday {matchday}</span>'
                f'<span class="section-sep-line"></span>'
                f'</div>'
            )
        current_matchday = matchday
        a, b = _esc(row["team_a"]), _esc(row["team_b"])
        sa, sb = int(row["score_a"]), int(row["score_b"])
        oi = _outcome(sa, sb)
        host = pr.HOST_BY_COUNTRY.get((row.get("country") or "").strip())
        hfa_team = (row["team_a"].strip() if row["team_a"].strip() == host
                    else (row["team_b"].strip() if row["team_b"].strip() == host else None))
        home_icon = "🏠 " if hfa_team == row["team_a"].strip() else ""

        bars = []
        bar_sep_inserted = False
        for fc_i, (label, _, group) in enumerate(forecasters):
            if group == "post-md1" and not bar_sep_inserted:
                bars.append('<div class="bar-sep"></div>')
                bar_sep_inserted = True
            fc_data = match_results[mi][fc_i]
            if fc_data is None:
                continue
            probs, _ = fc_data
            wa, wd, wb = _pct(probs[0]), _pct(probs[1]), _pct(probs[2])
            la = f"{wa}" if wa >= 6 else ""
            ld = f"{wd}" if wd >= 6 else ""
            lb = f"{wb}" if wb >= 6 else ""
            bars.append(
                f'<div class=barrow>'
                f'<span class=lbl>#{fc_i + 1} {short(label)}</span>'
                f'<div class="bar">'
                f'<span class="pa" style="flex:{wa}">{la}</span>'
                f'<span class="pd" style="flex:{wd}">{ld}</span>'
                f'<span class="pb" style="flex:{wb}">{lb}</span>'
                f'</div></div>')

        match_html.append(
            f'<div class=match>'
            f'<div class=hd>'
            f'<span class=teams>{home_icon}{a} v {b}</span>'
            f'<span class=res>{sa}-{sb}</span>'
            f'</div>'
            + "".join(bars)
            + f'<div class=scale><span>{a}</span><span>draw</span><span>{b}</span></div>'
            + f'</div>')

    n_fc = len(forecasters)
    n_post = sum(1 for _, _, g in forecasters if g == "post-md1")
    n_md1 = sum(1 for r in rows if int(r.get("matchday", 1)) == 1)
    n_md2plus = n_matches - n_md1
    lede = (f'{n_fc} forecaster{"s" if n_fc != 1 else ""} '
            f'({n_fc - n_post} pre-tournament · {n_post} post-MD1) '
            f'on {n_matches} played WC26 matches. '
            f'Lower RPS = sharper. '
            f'Pre-tournament models scored on all {n_matches} matches. '
            f'Post-MD1 models scored on MD2+ only ({n_md2plus} match{"es" if n_md2plus != 1 else ""}) '
            f'— their ratings incorporate MD1 results so MD1 comparisons would be poisoned.')

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    out = f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Model Lab — WC26 Daily Hub</title>
<link rel=preconnect href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Spline+Sans+Mono:wght@400;700&display=swap" rel=stylesheet>
<style>
:root{{--paper:#f5f1e6;--paper-2:#efe9da;--ink:#1b1812;--ink-soft:#615a4b;
--hairline:rgba(27,24,18,.16);--green:#1c6b35;--green-soft:rgba(28,107,53,.12);
--draw:#8a8170;--draw-soft:rgba(138,129,112,.14);--verm:#a83018;--verm-soft:rgba(168,48,24,.10);
--serif:"Fraunces",Georgia,serif;--mono:"Spline Sans Mono",Consolas,monospace;}}
@media (prefers-color-scheme:dark){{:root{{--paper:#16140e;--paper-2:#1d1a12;--ink:#ece5d2;
--ink-soft:#a59c86;--hairline:rgba(236,229,210,.18);--green:#62b97e;--green-soft:rgba(98,185,126,.14);
--draw:#a59c86;--draw-soft:rgba(165,156,134,.14);--verm:#e8765a;--verm-soft:rgba(232,118,90,.12);}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif);
line-height:1.5;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:920px;margin:0 auto;padding:24px 18px 80px}}
h1{{font-size:1.9rem;margin:0 0 4px}}
h2{{font-size:1.15rem;border-bottom:2px solid var(--ink);padding-bottom:5px;margin:34px 0 14px}}
.kicker{{font-family:var(--mono);font-size:.68rem;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-soft)}}
.lede{{color:var(--ink-soft);font-style:italic;max-width:62ch}}
a{{color:var(--verm)}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}}
.card{{flex:1 1 200px;border:1px solid var(--ink);background:var(--paper-2);
box-shadow:4px 4px 0 var(--hairline);padding:12px 14px}}
.card.best{{border-width:2px;box-shadow:5px 5px 0 var(--green-soft)}}
.card .nm{{font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--ink-soft)}}
.card .rps{{font-size:1.7rem;font-weight:700;margin:3px 0}}
.card .sub{{font-family:var(--mono);font-size:.72rem;color:var(--ink-soft)}}
.card .delta.good{{color:var(--green);font-weight:700}}
.card .delta.bad{{color:var(--verm);font-weight:700}}
.match{{border:1px solid var(--hairline);background:var(--paper-2);padding:11px 13px;margin:9px 0}}
.match .hd{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}}
.match .teams{{font-weight:700}}
.match .res{{font-family:var(--mono);font-size:.8rem}}
.barrow{{display:grid;grid-template-columns:74px 1fr;gap:8px;align-items:center;margin-top:7px}}
.barrow .lbl{{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-soft)}}
.bar{{display:flex;height:20px;border:1px solid var(--ink);font-family:var(--mono);font-size:.62rem;font-weight:700}}
.bar span{{display:grid;place-items:center;overflow:hidden;min-width:0}}
.bar .pa{{background:var(--green-soft);border-right:1px solid var(--ink)}}
.bar .pd{{background:var(--draw-soft);border-right:1px solid var(--ink)}}
.bar .pb{{background:var(--verm-soft)}}
.scale{{display:flex;justify-content:space-between;font-family:var(--mono);font-size:.6rem;color:var(--ink-soft);margin:3px 0 0 82px}}
.section-sep{{display:flex;align-items:center;gap:10px;margin:18px 0 10px;width:100%}}
.section-sep-line{{flex:1;border-top:2px solid var(--ink);}}
.section-sep-label{{font-family:var(--mono);font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink);white-space:nowrap;padding:0 4px}}
.bar-sep{{height:0;border-top:2px solid var(--ink);margin:8px 0 5px 82px}}
footer{{margin-top:40px;border-top:1px solid var(--hairline);padding-top:12px;
font-family:var(--mono);font-size:.68rem;color:var(--ink-soft)}}
</style></head><body><div class=wrap>
<p class=kicker>The Record · experimental</p>
<h1>Model Lab</h1>
<p class=lede>{_esc(lede)}</p>
<h2>Scoreboard</h2>
<div class=cards>
{"".join(card_html)}
</div>
<p class=lede>Boxed card = lowest RPS (sharpest). Gaps within ~0.005 RPS are noise at this sample size — wait for MD2 results (~{n_matches + 24} matches) before drawing conclusions.</p>
<h2>Match by match</h2>
<p style="font-family:var(--mono);font-size:.66rem;color:var(--ink-soft);margin:0 0 10px">Green = left-team win · grey = draw · red = right-team win</p>
{"".join(match_html)}
<footer>Generated {_esc(now)} · all models use ratings frozen at 2026-06-11 (no in-tournament leakage) · struct_variant bars are PROVISIONAL (n={n_matches} MD1-heavy slice; retune after MD2) · experimental view, not part of the published model · <a href=record.html>back to The Record</a> · <a href=index.html>standings</a></footer>
</div></body></html>"""

    Path(args.out).write_text(out, encoding="utf-8")
    print(args.out)
    for fc_i, (label, mean_rps, hits, n_valid, group) in enumerate(stats):
        if mean_rps is not None:
            tag = " [post-MD1]" if group == "post-md1" else ""
            print(f"  #{fc_i+1} {label}{tag}: RPS {mean_rps:.4f}, hits {hits}/{n_valid}")


if __name__ == "__main__":
    main()
