---
name: hub-health
description: Pipeline state check for the WC26 hub — catches silent-green stalls (missing results, unresolved ties, missing cards, unlogged advance calls) that fail-soft CI hides. Run at session start during the tournament, or whenever "the site looks frozen".
---

# Hub health check

The failure mode this exists for (2026-07-04 incident): every CI run GREEN for 5 days
while three shootout results sat unentered — fail-soft steps + "0 results entered" looks
healthy. The site quietly froze: R16 ties unresolved, cards ungenerated, advance calls
at risk of being missed. Nothing in the logs is red; you have to check the DATA.

## Steps

1. `git pull --ff-only` FIRST. The local checkout is usually behind the bot's automated
   commits — diagnosing a stale checkout wastes the whole session (it looked like 3 days
   of failed builds; it was an un-pulled repo).

2. Data invariants (the actual stall detectors): `python scripts/health.py`
   (exit 1 + STALL/GAP lines = silently stalled; each line names the fix command).
   CI runs the same script in both workflows' health gates since 2026-07-04, so a
   stall also shows as a red run tagged `health(SILENT-STALL)` — but run it locally
   anyway, it's instant. `--today YYYY-MM-DD` simulates a future date.

3. Today's accountability, BEFORE first kickoff: `python scripts/ledger.py log-ko <today>`
   (idempotent; a missed pre-kickoff advance call is unrecoverable — no backfill, ever).

4. `gh run list --limit 8` — but remember green ≠ healthy; check step 2 regardless.
   If a run needs reading: `gh run view <id> --log | grep -iE "enter manually|STALL|error|remaining"`.
   Watch "API requests remaining this month" (Odds API ~500/mo free tier).

5. Fixes, in pipeline order: `python scripts/fetch_ko_results.py` (keyless ESPN pass
   handles shootouts; see CLAUDE.md knockout contract) → `knockout.py --resolve` →
   `fetch_ko_reg_scores.py` → cards (see the ko-cards skill) → `odds.py fetch` /
   `evaluate-ko <date> --record` / `settle` → `build_site.py --date <date>` →
   `build_edition.py <date>` → full test suite → commit (`git add data docs editions cards`,
   never `-A` for pipeline artifacts) → push (push = deploy).

New stall classes go into `scripts/health.py` (+ hermetic tests in
`tests/test_health.py` — synthetic matches only, never live-data assertions), not into
this skill: CI checks every run; a skill only checks when someone runs it.
