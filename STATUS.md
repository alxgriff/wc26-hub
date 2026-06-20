# WC26 Hub — Status (what's done, what's left)

*Last updated 2026-06-13. The running picture of where the project stands. For the
map read [ARCHITECTURE.md](ARCHITECTURE.md); for rationale read [DECISIONS.md](DECISIONS.md).*

## Shipped

All seven planned phases are live (history in [PLAN.md](PLAN.md)):

1. **Edition builder** (`build_edition.py`) — daily markdown from cards + standings + stakes.
2. **Result entry** (`enter_result.py`) — contract-safe.
3. **MD3 scenarios** (`scenarios.py`) — qualification-path enumerator.
4. **Predictor + ledger** (`predict.py`, `ledger.py`) — Elo+Futi consensus, Brier accountability.
5. **Odds & Best Bet** (`odds.py`) — edges, recorded picks, CLV.
6. **Static site** (`build_site.py`) — index, team/match pages, **bracket**, **record**, data.json.
7. **Sweat Factor** (`weather.py`) — WBGT heat + per-team climate.

Plus this session's additions: **2026 tiebreaker fix**, the **knockout bracket** (gated
"as it stands" projector + cascade layout + winner projection through the tree), the
**responsive Today slate**, and the **Dixon-Coles + knockout** model scaffolding (inert).

Tests: **372 green**. Daily automation live (3 GitHub Actions workflows). Deploys via
Cloudflare Pages on push.

## Open items

### Model (`predict.py` / MODEL_IMPROVEMENTS.md)
- **DONE 2026-06-19 — Elo auto-rolls forward.** `scripts/update_elo.py` rolls the VERIFIED
  6/11 anchor through played WC results (K=60) into a gitignored `..._CURRENT.csv` the model
  prefers; runs nightly in daily-build + closing-odds (forward-only, deterministic, fail-soft).
  Closes MODEL_IMPROVEMENTS §3.2. NB it does NOT fix the USA<Australia gap — that's structural
  (host effect), not staleness (post-roll USA rk24 still < AUS rk21).
- **DONE 2026-06-18 — Futi tilt + 6/18 ratings.** Investigation of "Futi-only vs hybrid"
  (`scripts/eval_blend.py`) → fixed Futi tilt `w_futi 1.0→1.5` (f=0.60) + ingest the
  post-MD1 6/18 futi.live ratings, forward-only (no MD1 leakage). See DECISIONS.md.
- **NEXT (scoped, follow-up) — market consensus as a 3rd source** (`MODEL_IMPROVEMENTS.md`
  §3.3). On 24 played games market-only Brier 0.60 beats model-only 0.67; integrate as a
  market-tilted W/D/L overlay (fixed weight, not fit). `scripts/backtest_market.py` validated
  the methodology on 26k club matches (market dominates a bare Elo; blend flat-and-market-heavy).
- **Edge experiment — Deserved-Result Divergence** (`scripts/edge_drd.py`). Since the market
  beats a strength model, edges must come from orthogonal signal: Futi (process/EPV) vs Elo
  (results/reputation), where the market sits near reputation and hasn't priced the process.
  Tags qualifying picks to a paper log (`data/edge_drd_log.csv`) and reports **CLV-by-tag** —
  the clean judge (real process edge → close moves to us; our over-favouring → close moves away).
  First read: blanket signal ~0, filtered "unpriced divergence" subset hints at value (n=4, anecdote).
  Forward CLV is the verdict. Could wire `log`+`report` into the `closing-odds` job. Next tags to
  build: heat→totals (`weather.py`), MD3 motivation (`scenarios.py`).
- **Fit the inert levers against live results.** ρ, per-host HFA (`Config.hfa_by_host`),
  and the knockout `c`/reach-shootout calibration (Tier 2.5) all ship dormant pending a
  fit — re-run `fit_rho.py`/`fit_hfa.py` if a fuller λ model (Maher MLE) or accumulated
  results warrant. *MODEL_IMPROVEMENTS.md §2.2, §2.4, §2.5.*
- **Tier 2 corpus / backtest harness (2.1–2.3)** — needs Kaggle datasets the sandbox
  can't fetch (`data/History/*.csv` is gitignored; re-fetch in `History/DATA_QUALITY.md`).
- **Tier 3 — investigate-only, do NOT mechanize:** goals-total-vs-gap (the κ·|sup| patch is
  rejected; consider overdispersion/Maher via backtest) and within-tournament Elo updating.
- **Tier 4 — deferred:** second sharp book for CLV benchmark; shot-level xG layer;
  automated injury/lineup adjustment.

### Knockout bracket
- **Daily-edition hook not yet landed** — the bracket is on the site (`docs/bracket.html`)
  with winner projection, but `build_edition` doesn't yet embed it. `render_markdown` is
  ready. *DECISIONS.md "Knockout bracket"; commit `5f02e5a` follow-up note.*
- **Real knockout results** — `bracket.feed(results=…)` is the override hook; wire it once
  knockout fixtures/results exist (the knockout **stage** is a separate later project per
  CLAUDE.md; `fixtures.csv` is group-only).
- **Per-host HFA in KO ties** — currently neutral venue (no KO venue→country map in repo).

### Odds (`odds.py`)
- **Draw-no-bet** is computed (`predict.py` `dnb_a`) but not snapshotted/recorded.
- **Power/Shin de-vig** — multiplicative is the shipped default; power/Shin is an optional upgrade.
- Bulk June-12 `phase=closing` rows are intentionally **not** re-tagged (settle-time window
  already neutralizes them for CLV).

### Scenarios (`scenarios.py`)
- **Full head-to-head-within-tied-clusters under unknown margins is deferred.** The MD3
  margin label no longer over-claims, but PLAN.md asks for a **stronger-model review before
  June 24** (the trickiest logic in the project). *PLAN.md Phase 3.*

### Weather / Sweat Factor (`weather.py`)
- **CONFIG weights + normalization bounds need sign-off** before leaning on them publicly;
  WBGT **solar term deferred to v2**. (Baselines already replaced with Open-Meteo archive
  values.) *PLAN.md Phase 7.*

### Verification (recurring)
- **June 20 F4 (Tunisia–Japan) kickoff** — listed 12:00 AM ET June 21 / 10 PM June 20
  Monterrey; **re-verify before that edition publishes**. *CLAUDE.md "Verification rules".*
- **MD2/MD3 card injury/selection notes** were baked June 11 — refresh from current news
  before each edition; anything tagged "(verify before use)" must be web-verified or cut.

### Placeholders by contract (not bugs)
- "The Call" / "Odds & Best Bet" stay in `*[…]*` placeholder for any match with no logged
  snapshot — never fetch-and-guess.

## How to pick up next session
1. Read CLAUDE.md (contracts) → this file (where things stand) → ARCHITECTURE.md if you need the map.
2. Run `python -m unittest discover -s tests` (expect 372 green).
3. Check the recurring verification items above against today's date before publishing.
