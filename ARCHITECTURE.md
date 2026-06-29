# WC26 Hub — Architecture (what lives where)

*Last updated 2026-06-13. The map of the codebase: modules, data flow, files, and
how it ships. For the rules/contracts read [CLAUDE.md](CLAUDE.md); for why things
are the way they are read [DECISIONS.md](DECISIONS.md); for what's left read
[STATUS.md](STATUS.md).*

## What this is

A daily 2026 World Cup **group-stage** guide + predictor. One markdown edition per
ET date (June 11–27) and a static HTML site, both generated from a single source of
truth (`data/fixtures.csv`) by pure-Python-stdlib scripts. No framework, no build
step, no JS in the output. Push to `main` deploys the site (Cloudflare Pages watches
`docs/`).

## The pipeline

```
data/fixtures.csv ──> standings.py ──┬─> scenarios.py     (MD3 qualification paths)
 (single source       (ranking,      ├─> bracket.py       (R32→Final, gated; + Annex C)
  of truth)            2026 tiebreak) └─> predict.py       (W/D/L + scoreline; data/Ratings/)

 ledger.py        (predictions_log.csv — pre-kickoff calls + Brier)        ┐
 odds.py          (odds_log.csv, picks_log.csv — edges, picks, CLV)        │
 weather.py       (weather_log.csv — heat/Sweat Factor)                    ├─> build_edition.py ─> editions/YYYY-MM-DD.md
 site_content.py  (cards/*.md, kb/ guide → profiles + card prose)          │
 stakes_blurb.py / fetch_news.py (Sonnet-written morning blurb / The Wire) ┘
                                                                           └─> build_site.py ────> docs/ (index, bracket,
                                                                                                          record, teams/,
                                                                                                          matches/, data.json)
                                                                                                   push main = Cloudflare deploy
```

Predictions/odds/weather feed the assemblers through **defensive adapters** in
`build_site.py` (`load_predictor`, `load_odds_engine`, `load_weather_engine`,
`load_knockout_resolver`): if a model/API is unavailable the section degrades to a
flagged placeholder — the build never breaks and nothing is invented.

## Modules (`scripts/`)

Ranking/qualification math lives **only** in `standings.py` — everything else imports
it, so there is no second implementation to drift.

### Core libraries (no repo imports, or import only `standings`)
| Module | Role | Key API | CLI |
|---|---|---|---|
| `standings.py` | Group ranking engine; FIFA-2026 tiebreakers (H2H before overall GD); best-8 thirds | `load_fixtures`, `compute_standings`, `to_dict`, `render_markdown`; dataclasses `Match/TeamRow/GroupTable/Standings` | `standings.py` |
| `site_content.py` | Parsers: KB team profiles, match cards (3 formats), constrained markdown→HTML | `parse_kb`, `parse_card`, `md_to_html`, `slugify` | — (library) |
| `scenarios.py` | MD3 outcome enumerator (9 combos → top2/third/out/margin-dependent) | `enumerate_scenarios`, `render_match_stakes` | `scenarios.py A` |
| `predict.py` | Match predictor: Elo+Futi consensus → Poisson matrix → W/D/L, score, O/U, BTTS; knockout resolver | `load_ratings`, `predict_match`, `resolve_knockout`, `blend_wdl`; `Config`, `Prediction`, `KnockoutPrediction` | `predict.py <id\|teams>` |
| `bracket.py` | Knockout projector R32→Final; gated; Annex C third-place lookup; winner propagation | `load_annex_c`, `project`, `feed`, `to_dict`, `render_markdown`; `R32_TEMPLATE`, `BRACKET_TREE` | `bracket.py` |
| `knockout.py` | Knockout data contract (matches 73–104): loader/validator, R32+feeder team resolver, penalty-aware result entry | `load_knockout`, `materialize_teams`, `enter_ko_result`, `results_dict`, `slot_labels`; `KnockoutMatch` | `knockout.py --resolve\|--enter` |
| `knockout_cards.py` | Sonnet-grounded knockout match cards (KB profiles + facts + Wire), fail-soft, auto-published to `cards/ko/` | `generate_card`, `build_facts`, `load_ko_card` | `knockout_cards.py [DATE]` |

### Accountability + odds
| Module | Role | Key API | CLI |
|---|---|---|---|
| `ledger.py` | Prediction ledger: log W/D/L pre-kickoff, grade played matches (Brier) | `log_slate`, `upsert_prediction`, `grade`, `cumulative_line`, `brier`, `probs_valid` | `ledger.py log\|report` |
| `odds.py` | Market snapshot, multiplicative de-vig, edge vs consensus, recorded picks, CLV settle | `evaluate_match`, `best_bets`, `record_pick`, `settle_picks`, `consensus_probs`, `units_summary` | `odds.py fetch\|enter\|evaluate\|settle\|report` |

### Assemblers (orchestrators)
| Module | Role | Key API | CLI |
|---|---|---|---|
| `build_edition.py` | Daily markdown edition: cards + standings + stakes + calls/odds | `build_edition`, `read_rows`, `select_matches`, `extract_card`, `inject_*` | `build_edition.py YYYY-MM-DD` |
| `build_site.py` | Static site: index, team/match pages, bracket, record, data.json | `build_site`, `build_page`, `render_*` (incl. `render_slate`, `render_bracket_html`) | `build_site.py --date …` |

### Daily fetchers (network boundary; all fail-soft)
| Module | Role | CLI |
|---|---|---|
| `fetch_results.py` | Pull completed scores from The Odds API → `fixtures.csv` | `fetch_results.py [--dry-run]` |
| `fetch_ko_results.py` | Knockout scores → `knockout.csv` (decisive auto-entered; level/penalty reported for manual entry) | `fetch_ko_results.py [--dry-run]` |
| `fetch_ko_reg_scores.py` | Knockout 90-minute (regulation) score from ESPN's `fifa.world` feed → `knockout.csv` reg cols (basis for 90' bet settlement on extra-time ties) | `fetch_ko_reg_scores.py [--dry-run]` |
| `weather.py` | Open-Meteo forecast/actual → WBGT heat + per-team climate (Sweat Factor) | `weather.py --date\|--baselines\|--backfill` |
| `stakes_blurb.py` | Sonnet-written morning standfirst, grounded only in computed facts | `stakes_blurb.py [DATE]` |
| `fetch_news.py` | Sonnet+web UNVERIFIED injury/lineup digest → `news/` ("The Wire") | `fetch_news.py [DATE]` |

### Dev / one-off tools (not in the daily loop)
| Module | Role | CLI |
|---|---|---|
| `enter_result.py` | Contract-safe result entry into `fixtures.csv` (byte-preserving, refuses overwrite) | `enter_result.py A2 0-0 [--force]` |
| `parse_annex_c.py` | Generate `data/annex_c.csv` (FIFA Annex C 495-row third-place table) from Wikipedia | `parse_annex_c.py [file]` |
| `fit_rho.py` | Fit Dixon-Coles ρ on historical results, OOS-validate, write `calibration.json` | `fit_rho.py [--write]` |
| `fit_hfa.py` | Measure home-field advantage / host-strength scaling (diagnostic) | `fit_hfa.py` |

## Data files (`data/`)

Committed unless noted. CSVs carry a UTF-8 BOM (use `utf-8-sig`).

| File | Purpose | Written by |
|---|---|---|
| `fixtures.csv` | **Single source of truth** — schedule, scores, status (see CLAUDE.md for the column contract) | `enter_result`/`fetch_results` (in place) |
| `discipline.csv` | Fair-play conduct points (tiebreaker input) | human |
| `Ratings/*.csv` | Pre-tournament ratings (Elo VERIFIED, Futi, Opta context, Market); audited in `Ratings/DATA_QUALITY.md` | human |
| `ratings.csv`, `team_strength.csv` | Consensus rating outputs | `predict.py --build-ratings` |
| `annex_c.csv` | 495-row R32 third-place assignment table | `parse_annex_c.py` |
| `knockout.csv` | Knockout stage (matches 73–104): static schedule + materialized teams + results | `knockout.py`/`fetch_ko_results.py` |
| `ko_predictions_log.csv` | Pre-kickoff knockout advance calls + 2-class Brier (immutable) | `ledger.py log-ko` |
| `predictions_log.csv` | Pre-kickoff W/D/L calls + predicted score (immutable) | `ledger.py` |
| `odds_log.csv` | Market snapshots + closing lines (append-only) | `odds.py` |
| `picks_log.csv` | Recorded picks + units + CLV | `odds.py` |
| `venues.csv`, `team_climate.csv`, `weather_log.csv` | Sweat Factor inputs/log | static / `weather.py` |
| `calibration.json` | Fitted ρ / host-HFA (DORMANT — not written; mechanism inert) | `fit_rho.py --write` |
| `History/results.csv` | Historical international corpus (martj42 CC0) — **gitignored**; provenance in `History/DATA_QUALITY.md` | manual download |
| `.odds_api_key` | The Odds API key — **gitignored**, never logged | human |

## Directories
| Dir | Contents |
|---|---|
| `scripts/` | all Python (above) |
| `tests/` | unit tests + `fixtures/site_snapshot/` (frozen June-12 state for hermetic tests) |
| `templates/` | `page.html`, `match.html`, `team.html`, `record.html`, `bracket.html`, `site.css` (inlined per page) |
| `cards/` | pre-baked match cards `md1/md2/md3.md` + `template.md` (baked June 11; MD2/MD3 injury notes need day-of verification) |
| `kb/` | `2026_fifa_world_cup_guide.md` — tactical source of truth (as of June 11) |
| `editions/` | published daily markdown (committed) |
| `docs/` | **site output** — what Cloudflare serves; committed |
| `news/` | UNVERIFIED auto-gathered digests (gated; human-reviewed) |

## Tests, CI, deploy
- **Tests:** `python -m unittest discover -s tests` (544 green, ~10s). *Do not* use the
  dotted-path form — there's no `tests/__init__.py`. Hermetic site tests build from
  `tests/fixtures/site_snapshot/` with an injected clock, so live data drift can't
  redden them.
- **`.github/workflows/`:** `ci.yml` (per-push: tests + smoke build) · `daily-build.yml`
  (07:00 ET cron `0 11 12-28 6 *` + dispatch — the morning publish) · `closing-odds.yml`
  (4×/day, 11:30 AM / 6:30 / 8:30 / 11:30 PM ET — one fetch tagged BOTH snapshot+closing
  (`fetch --phase both`) for CLV **and** to re-record edges DraftKings posts after the
  7 AM run; plus intra-day results + settle + rebuild between game blocks, so scores/
  record update same-day; no pre-mutation test gate there, since mid-day played-game data
  would false-red the point-in-time tests — the build is the gate). `daily-build` +
  `closing-odds` share concurrency group `wc26-publish` (queue, never cancel).
- **Card freshness:** a best bet whose market line is older than the 12h recording gate is
  shown as a "Model lean · stale line" (not "Best bet"), so the card never recommends a
  bet the ledger won't record (`load_odds_engine` flags stale markets; `render_market` demotes).
- **Daily publish order:** test gate (hard, **pre-mutation**) → log slate → fetch
  results → settle picks → snapshot odds → evaluate/record → weather → stakes blurb →
  build site (hard) + edition → smoke-check (hard) → commit `data/ docs/ editions/` →
  health gate. Data steps are fail-soft; tests/build/smoke are hard gates.
- **Deploy:** push to `main` → Cloudflare Pages rebuilds from `docs/` (no build command,
  `docs/` is the output root). Push **is** the deploy.
