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

2. Data invariants (the actual stall detectors) — run:

```bash
python - <<'EOF'
import sys, csv
from datetime import date, datetime, timezone
from pathlib import Path
sys.path.insert(0, "scripts")
import knockout as ko
today = datetime.now(timezone.utc).date()
matches = ko.load_knockout(Path("data/knockout.csv"))
bad = 0
for km in matches:
    d = date.fromisoformat(km.date_et)
    if d < today and not km.is_played and km.participants_known:
        print(f"STALL M{km.match_no}: {km.team_a} vs {km.team_b} played {km.date_et}, no result"); bad += 1
    if km.is_played and km.decided_by in ("extra_time", "penalties") and km.reg_score is None:
        print(f"STALL M{km.match_no}: ET/pens tie missing 90' reg score (bets can't settle)"); bad += 1
    if km.participants_known and not km.is_played and abs((d - today).days) <= 3 \
            and not Path(f"cards/ko/M{km.match_no}.md").exists():
        print(f"GAP  M{km.match_no}: resolved, kicks {km.date_et}, NO card"); bad += 1
print("data invariants OK" if not bad else f"{bad} issue(s)")
EOF
```

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

If step 2 keeps finding the same class of stall, promote that check into a
`scripts/health.py` + a hard CI step — a skill catches it when someone runs it; CI
catches it every morning.
