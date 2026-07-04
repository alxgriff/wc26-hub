# WC26 Daily Hub

**Project docs** (read in this order for a fast start):
- [ARCHITECTURE.md](ARCHITECTURE.md) — what lives where: modules, data flow, files, CI/deploy.
- [STATUS.md](STATUS.md) — what's shipped and what's still open.
- [DECISIONS.md](DECISIONS.md) — the load-bearing choices and why.
- [PLAN.md](PLAN.md) — original phased plan (all phases shipped) + the daily ops checklist.
- [MODEL_IMPROVEMENTS.md](MODEL_IMPROVEMENTS.md) — the detailed predictor roadmap.
- [TOTALS_FIX_EXPLAINER.md](TOTALS_FIX_EXPLAINER.md) — plain-English explainer of the June 14 Maher-form totals fix (for non-specialists).

This file (CLAUDE.md) holds the **data contracts and rules** — they override everything else.

Daily 2026 World Cup group-stage guide + game predictor. One edition per ET
date (June 11–27), built from pre-baked match cards + computed standings +
model predictions. Group stage **and** knockout: the knockout stage (Round of 32 →
Final, matches 73–104) ships as `data/knockout.csv` + `scripts/knockout.py` and takes
over the site/edition once the groups finish (see the knockout data contract below).

## Data contracts (do not violate)

### data/fixtures.csv — single source of truth for matches
Columns: match_id, group, matchday, date_et, kickoff_et_24h, kickoff_et,
team_a, team_b, stadium, city, country, tv_us, score_a, score_b, status, notes
- match_id: group letter + 1–6 (A1–L6). MD1 = ids 1–2, MD2 = 3–4, MD3 = 5–6.
- status: "scheduled" | "played". score_a/score_b empty until played.
- date_et is the **ET calendar date** of kickoff. Three games kick off at
  12:00 AM ET (D2, J2, F4): they belong editorially to the **previous day's
  edition** (🌙 late-cap convention) even though date_et is the next day.
- Never edit team names, kickoff times, or match_ids without explicit ask.

### Team-name canon — all joins are exact-string on these names
Use exactly: "United States", "South Korea", "South Africa", "Bosnia and
Herzegovina", "Côte d'Ivoire", "Türkiye", "DR Congo", "Cape Verde",
"New Zealand", "Saudi Arabia", "Czechia", "Curaçao". Any ratings/odds file
the user supplies must be normalized to this canon before joining; if a
name doesn't match, stop and report it rather than fuzzy-matching silently.

### data/ratings.csv — Phase 2 (user-supplied models + Opta aggregate)
Preferred schema (match-level): match_id, p_home, p_draw, p_away, source, asof
Fallback schema (team-level): team, rating, source, asof — in this case
scripts/predict.py converts rating gaps to W/D/L probabilities via a
Poisson layer (rating gap → expected goals per side → score matrix → W/D/L).
Aggregation across sources: simple average of probabilities unless the user
specifies weights. Probabilities must sum to 1.0 ± 0.001 per match.

### data/knockout.csv — knockout stage (matches 73–104); fixtures.csv stays the GROUP SSOT
Columns: match_no, round, date_et, kickoff_et_24h, kickoff_et, stadium, city, country,
tv_us, team_a, team_b, score_a, score_b, score_a_reg, score_b_reg, decided_by, winner, status, notes
- score_a/score_b are the 90'+ET **aggregate** (authoritative for advancement via `winner`);
  score_a_reg/score_b_reg are the **90-minute (regulation)** score on which the 90' bet markets
  (totals / handicap / BTTS) settle. A regulation-decided game's reg == final (may be left blank
  and is derived); a tie that reaches extra time needs reg filled — `fetch_ko_reg_scores.py` pulls
  it from ESPN's keyless `fifa.world` feed (90' = sum of the first two half line-scores).
- match_no: the FIFA/bracket number 73–104 (R32 73–88, R16 89–96, QF 97–100, SF
  101–102, third-place 103, Final 104) — the SAME numbering `scripts/bracket.py` uses.
  Knockout fixtures CANNOT live in fixtures.csv (its match_id is locked to A1–L6).
- The SCHEDULE columns are static (entered once from the FIFA calendar; all stadiums
  join `venues.csv` canon). team_a/team_b are a MATERIALIZED VIEW of the bracket: blank
  until a tie resolves, then filled by `knockout.py --resolve` (R32 from locked group
  positions, later rounds from played feeders). `bracket.py` owns the structure; a guard
  test forbids drift. Never hand-edit match_no/schedule without explicit ask.
- A knockout match cannot end level: unequal score ⇒ `decided_by` regulation|extra_time
  on the higher side; equal score ⇒ `decided_by` penalties with `winner` (A|B) the
  shootout winner — a shootout winner is NEVER inferred from the score. `winner` is
  authoritative for advancement (`bracket.feed` and the ledger read it, not the score).
- Results: `fetch_ko_results.py` runs two passes. A keyless ESPN pass reads completed
  ties from the `fifa.world` scoreboard: penalty ties are auto-entered with the shootout
  winner from ESPN's explicit `shootoutScore` (still never inferred from the 90'+ET
  score; a level tie ESPN shows no tally for falls back to a manual `knockout.py
  --enter` report), decisive ties the Odds API window expired past are swept up, and AET
  is read from ESPN's status. Then the Odds API pass enters remaining DECISIVE results.
  Same exact-canon join + never-invent rules.

## Tournament rules (encoded in standings/scenario scripts)
- 12 groups of 4, single round-robin. Top 2 per group + 8 best third-placed
  teams advance to a Round of 32.
- Group tiebreakers, in order (2026 FIFA, Euro-style — verified 2026-06-13 against
  the FWC2026 Regulations, May 2025): points; then HEAD-TO-HEAD among the tied teams
  (its points, then GD, then goals scored, reapplied to any still-level subset); then
  overall goal difference; overall goals scored; fair-play conduct; FIFA Men's World
  Ranking (most recent, then progressively older). NB head-to-head comes BEFORE
  overall GD — this CHANGED from the pre-2026 order, and there is no "drawing of lots".
- Third-place ranking across groups (no head-to-head — different groups): points,
  overall GD, goals scored, fair-play conduct, FIFA World Ranking.
- FIFA World Ranking is not modelled in-repo: a residual tie after fair play is
  flagged provisional (shown alphabetically), never silently resolved.
- MD3 games within a group kick off simultaneously.

## Daily edition workflow (scripts/build_edition.py output)
1. Update fixtures.csv with yesterday's results (status → played, scores in).
2. Run standings (group tables + live third-place table).
3. Pull today's match cards from cards/md{1,2,3}.md by match_id.
4. Fill live sections: **Stakes** (from standings/scenarios), **The Call**
   (from predictions once Phase 2 is live), **Odds & Best Bet** (Phase 3).
5. Recap section: yesterday's predictions vs results — Brier score running
   ledger, plus units and CLV once betting goes live.
6. Write to editions/YYYY-MM-DD.md.

## Verification rules
- Cards were pre-baked June 11. **Injury/selection notes must be refreshed
  from current news before publishing** — MD2/MD3 cards say so in their
  headers. Anything tagged "(verify before use)" must be web-verified or cut.
- Tunisia–Japan (F4) kickoff was flagged for re-verification (listed as
  12:00 AM ET June 21 / 10 PM June 20 Monterrey). Confirm before that edition.
- The kb/ guide is the tactical source of truth as of June 11; prefer it for
  squad/system facts, and the web for anything after that date.

## Prediction accountability
- Every published prediction gets logged: match_id, p_home/p_draw/p_away,
  predicted score, timestamp. Brier score = sum of squared errors of the W/D/L
  probability vector vs the 1/0/0 outcome vector (multiclass Brier, range 0–2;
  0.667 = coin-flip baseline); report cumulative + per-day.

## Phase 3 — Odds & Best Bet methodology
- At publish time, snapshot the market: 3-way moneyline (1X2) and total goals
  O/U, plus Asian handicap (spreads) and both-teams-to-score where quoted. Log
  to data/odds_log.csv: match_id, market, selection, line, odds (decimal),
  source, phase, timestamp. The 1X2 edge is vs the published consensus;
  totals/handicap/BTTS are model-priced from the score matrix (the overlay
  covers W/D/L only). Draw-no-bet is computed but not yet snapshotted/recorded.
- **Odds source: prefer DraftKings, fall back to other US books** (`odds.py fetch
  --bookmaker`, user-set June 14 as single-book, revised June 16 to prefer-else-
  fallback). The fetch pulls the WHOLE US region (same quota — one region) and, per
  selection, logs DraftKings alone when it quotes the line (so the de-vigged implied
  is the line you can really take), else the median/best of the books that do. This
  is because DraftKings supplies h2h via the API but NOT totals/spreads — so h2h is
  DK while totals/spreads fall back to the other US books (betmgm/bovada/…), keeping
  them covered. `--bookmaker all` = no preference (best of all). Provenance is
  labelled honestly (`snapshot_source_label`): "DraftKings where quoted, else best of
  N US books" for the mix; never claim a sourcing the log lacks.
- De-vig the 1X2: implied_i = (1/odds_i) / Σ(1/odds_j) (multiplicative
  method; power or Shin method optional upgrade later).
- Edge_i = model_p_i − implied_i. Display threshold 3 percentage points;
  **recorded picks**: up to 3 per match, the best selection per distinct
  market, each clearing a **4-point recording bar** (user-set June 12 at 5pp,
  lowered to 4pp June 14 to gather more model-performance data once the Maher
  totals fix left the model well-calibrated — a measured experiment, revert if
  the [4,5)pp band shows negative CLV) and a sanity
  ceiling. The sanity ceiling is **market-aware** (user-set June 14): 1X2 is
  checked against the published consensus, so it keeps the 15-point ceiling;
  totals/spreads/BTTS are model-priced from the same score matrix that makes the
  edge (no independent cross-check), so they clear a **stricter 8-point ceiling**
  — a large self-priced edge is far more likely our miscalibration than market
  error (see the Germany–Curaçao total-goals saturation case). Same-match picks
  are correlated — disclose it
  wherever they're shown. Otherwise output "No bet" — a legitimate,
  expected result.
- Track closing line value: log closing odds for every pick; CLV = closing
  implied minus snapshot implied on our selection. Report units (flat 1u
  stakes unless told otherwise) and CLV alongside Brier in recaps.
- Never invent odds. If no snapshot was provided/fetched, the section stays
  in placeholder state.
- **Knockout betting (matches 73–104):** the market is **advance** ("to qualify"), 2-way
  (no draw). The model price is `predict.resolve_knockout`'s p_advance (incl. extra time +
  a coin-flip shootout). The Odds API carries **no** to-qualify market, so the market price
  is **auto-derived from the fetched 90-minute 3-way h2h** (the same one US-region pull):
  de-vig the 90' h/d/a, then route the draw mass through the SAME ET+shootout layer —
  `market_adv_a = q_h90 + q_d90·(et_a + et_d·0.5)`. This is a **model-vs-market READ** (a
  vig-free fair probability, NOT a price you can take), shown on the card but **NEVER
  recorded**: the ET layer is the model's on both sides, so the edge is just the 90'
  disagreement on the advance axis, and with no real line there's no CLV. (A genuinely
  quoted 2-way advance line, via manual `odds.py enter M.. advance H,A`, WOULD be recorded —
  model-priced, 8pp ceiling, settled from `knockout.csv`'s `winner`, penalty-aware.) The fetched
  **90-minute** totals / Asian-handicap / BTTS lines ARE recorded too (model-priced from the 90'
  score model, same 4pp bar / 8pp ceiling as group games): they **settle on the regulation (90')
  score** — a regulation game's is its final, an extra-time game's is fetched from ESPN into
  `knockout.csv`'s reg columns; `settle_picks` reads `KnockoutMatch.reg_score` and leaves a pick
  open until the 90' score is known (extra-time goals never count toward these markets). Only a
  *derived* advance read (no quoted price) stays display-only.
  Accountability is separate: pre-kickoff advance calls are logged to
  `data/ko_predictions_log.csv` and graded with a **2-class Brier** (0 best · 0.5 coin-flip
  · 2 worst) against the advancing side.

## Phase 7 — Sweat Factor data contracts

### data/venues.csv — static, 16 rows, join key = `stadium` (exact string)
Columns: `stadium, city, lat, lon, roof, air_conditioned`
- `roof`: `open | retractable | canopy`
- `air_conditioned`: `true` only for venues with closeable, bowl-cooling roofs
  (AT&T Stadium, Mercedes-Benz Stadium, NRG Stadium). Canopy venues (Hard Rock,
  SoFi) treat pitch as open. BC Place retractable but not AC.
- Clamp rule: if `air_conditioned`, `wbgt_est` = `CONFIG["cc_wbgt"]` (21.0°C)
  and the page shows "Indoors — heat not a factor."

### data/team_climate.csv — static baselines, 48 rows, join key = canon team name
Columns: `team, baseline_lat, baseline_lon, baseline_wbgt, source, asof`
- `baseline_wbgt` computed once from Open-Meteo historical archive via
  `weather.py --baselines` (June–July mean WBGT over years 2022–2024). Do not
  recompute at build time. Initial values marked `source=estimate`; update to
  `source=openmeteo-archive` after running `--baselines`.
- v2 upgrade path: replace capital-city proxy with squad-weighted club-city blend.

### data/weather_log.csv — append/upsert log (like odds_log.csv)
Columns: `match_id, source, temp_c, rh_pct, wind_ms, solar_wm2, wbgt_est, climate_controlled, as_of`
- `source`: `forecast | actual`. Both rows may exist per match.
- Upsert key = `(match_id, source)`. Re-running a fetch updates the row, never double-logs.
- Fetch runs before the build (weather.py --date) — **never at build time**. If no
  row exists for a match, every section shows a clearly-marked placeholder.
- Attribution required in footer: "Weather by Open-Meteo (CC BY 4.0)."
- WBGT formula (BOM shade approx): `e = (rh/100)*6.105*exp(17.27*T/(237.7+T));
  wbgt = 0.567*T + 0.393*e + 3.94`. Climate-controlled venues clamp to 21.0°C.
- CONFIG (in `scripts/weather.py`) contains all tunable bounds; get sign-off
  before changing for first publication.

## Style
- Editions are markdown, prose-forward; match cards keep their 9-section
  format from cards/template.md. Don't editorialize injuries beyond sourced
  facts. Flag uncertainty rather than smoothing it over.