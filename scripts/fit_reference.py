#!/usr/bin/env python3
"""Persist the reference XGBoost model (from /Users/bnowak/dev/world_cup_predictions)
as a loadable artifact for the --reference CLI flag.

The reference repo trains-on-every-invocation by design (single-file teaching
project). We pin it to the wc26-hub pattern instead: train ONCE with cutoff at
tournament start, save the booster to data/calibration/reference.ubj, write a
meta sidecar, and never touch it again unless the user refits.

No look-ahead: training data is locked at TRAIN_START..TOURNAMENT_START. That's
the same data freeze our structural ratings (Elo + Futi) carry — a fair A/B/C.

Stdlib at import time; xgboost/pandas/sklearn imported only inside main(). The
daily-build path still runs on stdlib if the deps are missing — the artifact
just doesn't get rewritten.

Usage:
    python scripts/fit_reference.py [--cutoff 2026-06-11] [--write]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = REPO / "data" / "calibration" / "reference.ubj"
META = REPO / "data" / "calibration" / "reference.meta.json"

REFERENCE_REPO = Path(os.environ.get(
    "WC26_REFERENCE_REPO", "/Users/bnowak/dev/world_cup_predictions"))
TOURNAMENT_START = "2026-06-11"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Persist the reference XGBoost model.")
    ap.add_argument("--cutoff", default=TOURNAMENT_START,
                    help="train through this date (exclusive). Default: tournament start.")
    ap.add_argument("--write", action="store_true",
                    help="persist the artifact + meta. Without --write, prints validation only.")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not REFERENCE_REPO.exists():
        print(f"error: reference repo not found at {REFERENCE_REPO}", file=sys.stderr)
        print("  Set WC26_REFERENCE_REPO env var if it's elsewhere.", file=sys.stderr)
        return 1
    try:
        import xgboost  # noqa: F401
        import pandas   # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as e:
        print(f"error: missing deps for the reference model ({e})", file=sys.stderr)
        return 1

    sys.path.insert(0, str(REFERENCE_REPO))
    import predict_today as ref

    print(f"loading reference corpus from {REFERENCE_REPO} …")
    results = ref.load_results()
    dataset, final_elo = ref.build_dataset(results)
    print(f"  {len(dataset)} matches in dataset ({dataset['date'].min().date()}..{dataset['date'].max().date()})")
    print(f"  {len(final_elo)} teams in final Elo table")

    print(f"training XGBoost (cutoff {args.cutoff}) …")
    train, val = ref.split_by_date(dataset, ref.TRAIN_START, ref.VAL_START, args.cutoff)
    model, X_val, y_val = ref.train_model(train, val)
    print(f"  trained on {len(train)} matches; held-back validation slice n={len(val)}")
    ref.evaluate(model, X_val, y_val)

    if args.write:
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(str(ARTIFACT))
        today = datetime.now(timezone.utc).date().isoformat()
        meta = {
            "source": f"ref-xgb (Elo+form+rest+H2H) fit {today}",
            "asof": today,
            "trained_through": args.cutoff,
            "n_train": int(len(train)),
            "n_validation": int(len(val)),
            "features": list(ref.FEATURES),
            "ref_repo_path": str(REFERENCE_REPO),
            "tournament_weight": int(ref.MATCH_WEIGHT),
            "match_neutral": bool(ref.MATCH_NEUTRAL),
            "params": {
                "n_estimators": 600, "learning_rate": 0.05, "max_depth": 5,
                "subsample": 0.85, "colsample_bytree": 0.85, "reg_lambda": 1.0,
                "early_stopping_rounds": 50,
            },
            "corpus": "martj42/international_results (CC0) — same as wc26-hub",
        }
        META.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {ARTIFACT.relative_to(REPO)} and {META.relative_to(REPO)}")
        print(f"  source = {meta['source']}")
    else:
        print("\n(dry run — pass --write to persist data/calibration/reference.{ubj,meta.json})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
