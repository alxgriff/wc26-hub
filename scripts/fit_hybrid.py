#!/usr/bin/env python3
"""Train the Groll-style XGBoost overlay on historical international results.

Mirrors fit_rho.py's discipline: FIT BROAD (all curated competitive matches
2010+, before holdout), VALIDATE NARROW (out-of-sample 2023+ deployment slice =
WC/Euro/Copa). DRY RUN by default; --write persists data/calibration/hybrid.ubj
+ hybrid.meta.json only if the validation gate passes. Same "data rejected it"
pattern as rho.

Pipeline:
  1. Curate (reuse fit_rho.load_curated — drop friendlies/NA, ≥2010).
  2. Roll a chronological Elo time series. For each match snapshot the
     PRE-update Elo for both sides into a feature row (the "as-of-match-date"
     correctness the plan doc flags).
  3. Synthesize structural features (p_a, p_draw, p_b, lam_a, lam_b, total,
     sup, texture) per historical match using the project's predict.Config /
     predict._wdl — same code path as the live model. No Futi history ⇒
     texture=0 at training time; predict-time gets the real value.
  4. Time-decay sample weights (Dixon-Coles τ ≈ 1.5y) over the training window.
  5. XGBoost multi:softprob, ~200 trees, depth 4, learning_rate 0.05.
  6. Evaluate vs the SAME structural baseline (any gain ⇒ purely the overlay).
  7. Verdict line + acceptance gate (mirrors fit_rho's structure).

Stdlib at import time. xgboost/numpy imported lazily inside the train/evaluate
functions so the daily-build path still runs without the deps installed.

CLI:
    python scripts/fit_hybrid.py [--start 2010-01-01] [--holdout 2023-01-01] [--write]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as stats
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fit_rho as fr   # noqa: E402  (load_curated — corpus curation has ONE definition)
import hybrid as hy    # noqa: E402  (FEATURES + TOURNAMENT_WEIGHTS + MAJOR_TOURNAMENTS)
import predict as pr   # noqa: E402  (_wdl + Config — structural math has ONE definition)

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "data" / "History" / "results.csv"
ARTIFACT = REPO / "data" / "calibration" / "hybrid.ubj"
META = REPO / "data" / "calibration" / "hybrid.meta.json"

WINDOW_START = "2010-01-01"      # same as fit_rho — last ~4 cycles, recent enough for the modern game
HOLDOUT_FROM = "2023-01-01"      # out-of-sample slice (same convention as fit_rho)
TAU_DAYS = 547.5                 # Dixon-Coles time-decay half-life ≈ 1.5 years (v1 default)
DAYS_PER_YEAR = 365.25
ELO_INITIAL = 1500.0             # standard cold-start; warm up via the first ~500 matches
HFA_ELO = 100.0                  # full-crowd HFA used in the rolling-Elo update rule
                                 #  — eloratings.net convention; gets the absolute Elo scale
                                 #  closer to int-football.net's verified values that the live
                                 #  model consumes at predict-time.
BURN_IN_MATCHES = 500            # drop the first N feature rows from training (cold-start noise)

# K factor by tournament tier (matches eloratings.net's published rules
# qualitatively — major finals carry the most update, friendlies are excluded).
K_BY_TIER = {"major": 60.0, "qualifier": 40.0, "minor": 30.0}


def tier_of(tournament: str) -> str:
    t = (tournament or "").strip()
    if t in hy.MAJOR_TOURNAMENTS:
        return "major"
    if "qualification" in t or "qualifier" in t:
        return "qualifier"
    return "minor"


def goal_diff_multiplier(diff: int) -> float:
    """eloratings.net convention: 1.0 / 1.5 / (11+|diff|)/8 for 1 / 2 / ≥3 goal margin."""
    d = abs(diff)
    if d <= 1:
        return 1.0
    if d == 2:
        return 1.5
    return (11.0 + d) / 8.0


def expected_home(elo_home: float, elo_away: float, neutral: bool) -> float:
    """Standard Elo expected score with eloratings.net HFA convention."""
    adv = 0.0 if neutral else HFA_ELO
    return 1.0 / (1.0 + 10 ** (-(elo_home + adv - elo_away) / 400.0))


def roll_elo(matches: list) -> list:
    """Replay the curated corpus chronologically; for each match snapshot the
    PRE-update Elo of both sides (no future leakage) and return a feature row.

    Each returned dict has the inputs needed to synthesize structural features
    and the label/weight info downstream consumers need:
        date, home, away, hs, as_, neutral, tournament,
        elo_home_pre, elo_away_pre, outcome (0=home win, 1=draw, 2=away)
    """
    elo: dict = {}
    rows = sorted(matches, key=lambda m: m["date"])      # safety: re-sort even if caller did
    # Assert chronological invariant (cheap, catches the leakage bug class).
    for i in range(1, len(rows)):
        assert rows[i]["date"] >= rows[i - 1]["date"], "rolling Elo requires chronological order"
    out: list = []
    for m in rows:
        h, a = m["home"], m["away"]
        elo_h = elo.get(h, ELO_INITIAL)
        elo_a = elo.get(a, ELO_INITIAL)
        out.append({
            "date": m["date"], "home": h, "away": a,
            "hs": m["hs"], "as_": m["as"], "neutral": m["neutral"], "tournament": m["tournament"],
            "elo_home_pre": elo_h, "elo_away_pre": elo_a,
            "outcome": 0 if m["hs"] > m["as"] else (1 if m["hs"] == m["as"] else 2),
        })
        # Standard Elo update with goal-diff multiplier
        K = K_BY_TIER[tier_of(m["tournament"])]
        G = goal_diff_multiplier(m["hs"] - m["as"])
        S_h = 1.0 if m["hs"] > m["as"] else (0.5 if m["hs"] == m["as"] else 0.0)
        E_h = expected_home(elo_h, elo_a, m["neutral"])
        delta = K * G * (S_h - E_h)
        elo[h] = elo_h + delta
        elo[a] = elo_a - delta
    return out


def synthesize_structural(row: dict, cfg: pr.Config) -> dict:
    """Reuse the live model's lambda/W-D-L math (predict._wdl + Config) on the
    rolling Elo. ``texture=0`` (no Futi history) — at predict-time the live
    Prediction supplies the real value and the booster will have learned to
    near-ignore it (plan doc's accepted v1 caveat)."""
    bonus_h = (0.0 if row["neutral"] else cfg.hfa)
    sup = (row["elo_home_pre"] + bonus_h - row["elo_away_pre"]) / cfg.theta
    texture = 0.0
    total = cfg.mu0 * math.exp(cfg.alpha * texture)
    share = 1.0 / (1.0 + math.exp(-sup))
    lam_a, lam_b = total * share, total * (1 - share)
    p_a, p_d, p_b = pr._wdl(lam_a, lam_b, cfg)
    return {"p_a": p_a, "p_draw": p_d, "p_b": p_b,
            "lam_a": lam_a, "lam_b": lam_b, "total": total,
            "sup": sup, "texture": texture}


def feature_vector(row: dict, cfg: pr.Config, feature_names=hy.FEATURES) -> list:
    """Build the feature vector for one historical match in ``feature_names``
    order. team_a = home (matches the live model's listed-first convention)."""
    s = synthesize_structural(row, cfg)
    side = 0 if row["neutral"] else +1   # home is at home; neutral has no side
    return hy._features(
        p_a=s["p_a"], p_draw=s["p_draw"], p_b=s["p_b"],
        lam_a=s["lam_a"], lam_b=s["lam_b"], total=s["total"],
        sup=s["sup"], texture=s["texture"],
        elo_a=row["elo_home_pre"], elo_b=row["elo_away_pre"],
        tournament_weight=hy.tournament_weight_for(row["tournament"]),
        is_neutral=(1 if row["neutral"] else 0),
        home_advantage_side=side,
        feature_names=feature_names,
    )


# ---------------------------------------------------------------- calibration (stdlib)

def pool_adjacent_violators(xs: list, ys: list, ws: list | None = None) -> list:
    """PAV monotone (non-decreasing) regression. ``xs`` are already sorted
    ascending. Returns the calibrated y value at each input x, in the SAME order
    as the inputs. Standard left-to-right merge with backward-check on each
    violation. O(n) amortized — each block is created/merged at most twice."""
    n = len(xs)
    if n == 0:
        return []
    if ws is None:
        ws = [1.0] * n
    # block = [start_idx, end_idx, sum_wy, sum_w]
    blocks = [[i, i, ws[i] * ys[i], ws[i]] for i in range(n)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][2] / blocks[i][3] > blocks[i + 1][2] / blocks[i + 1][3] + 1e-15:
            merged = [blocks[i][0], blocks[i + 1][1],
                      blocks[i][2] + blocks[i + 1][2],
                      blocks[i][3] + blocks[i + 1][3]]
            blocks[i] = merged
            del blocks[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    out = [0.0] * n
    for s, e, swy, sw in blocks:
        v = swy / sw
        for k in range(s, e + 1):
            out[k] = v
    return out


def fit_isotonic(x: list, y: list) -> list:
    """One-vs-rest isotonic calibration curve. ``x`` are booster probabilities
    for one class, ``y`` are 0/1 indicators of that class actually occurring.
    Returns a list of (x_breakpoint, y_calibrated) pairs (sorted ascending)
    suitable for hybrid.apply_isotonic. Collapsed to breakpoint changes so the
    serialized form is compact (typically a few dozen breakpoints for thousands
    of training rows)."""
    if not x:
        return []
    order = sorted(range(len(x)), key=lambda i: x[i])
    xs = [x[i] for i in order]
    ys = [y[i] for i in order]
    fitted = pool_adjacent_violators(xs, ys)
    # collapse to breakpoints where the calibrated value changes
    pts = [(xs[0], fitted[0])]
    for i in range(1, len(xs)):
        if fitted[i] != fitted[i - 1]:
            pts.append((xs[i], fitted[i]))
    if pts[-1][0] != xs[-1]:
        pts.append((xs[-1], fitted[-1]))
    return [list(p) for p in pts]    # JSON-friendly tuples


def fit_platt(x: list, y: list, max_iter: int = 100, tol: float = 1e-7) -> list:
    """Two-parameter logistic σ(A·x + B) fitted by Newton-Raphson on the
    cross-entropy of (x, 0/1 y). Returns [A, B]. Anti-overfitting prior
    matches Platt 1999: smooth targets to (n+1)/(n+2) and 1/(n-+2) to keep
    Newton stable on imbalanced classes."""
    n = len(x)
    if n == 0:
        return [1.0, 0.0]
    n_pos = sum(1 for v in y if v > 0.5)
    n_neg = n - n_pos
    t_pos = (n_pos + 1.0) / (n_pos + 2.0)
    t_neg = 1.0 / (n_neg + 2.0)
    t = [t_pos if v > 0.5 else t_neg for v in y]

    A, B = 0.0, math.log((n_neg + 1.0) / (n_pos + 1.0))
    for _ in range(max_iter):
        # Compute gradient + Hessian of -Σ [t_i log σ(z_i) + (1-t_i) log(1-σ(z_i))]
        gA = gB = HAA = HAB = HBB = 0.0
        for xi, ti in zip(x, t):
            z = A * xi + B
            # clip z for numerical stability
            z = max(-500.0, min(500.0, z))
            p = 1.0 / (1.0 + math.exp(-z))
            d = p - ti
            wpq = p * (1.0 - p)
            gA += d * xi
            gB += d
            HAA += wpq * xi * xi
            HAB += wpq * xi
            HBB += wpq
        # Newton step: H · Δ = -g; 2x2 inverse
        det = HAA * HBB - HAB * HAB
        if abs(det) < 1e-15:
            break
        dA = -(HBB * gA - HAB * gB) / det
        dB = -(-HAB * gA + HAA * gB) / det
        A += dA
        B += dB
        if abs(dA) + abs(dB) < tol:
            break
    return [A, B]


def time_decay_weights(rows: list, ref_date: str, tau_days: float = TAU_DAYS) -> list:
    """w_i = exp(-(t_ref - t_i) / τ). The most recent training match gets w=1.0;
    a match τ days earlier gets w ≈ 0.37. Dixon-Coles canonical recency knob."""
    t_ref = datetime.strptime(ref_date, "%Y-%m-%d").date()
    out: list = []
    for r in rows:
        t = datetime.strptime(r["date"], "%Y-%m-%d").date()
        out.append(math.exp(-max((t_ref - t).days, 0) / tau_days))
    return out


# ---------------------------------------------------------------- metrics (stdlib)

def rps(p_a: float, p_draw: float, p_b: float, outcome: int) -> float:
    """Ranked Probability Score for ordered W/D/L. Range [0, 1]; lower is better.
    Respects the W/D/L ordering (a "wrong by one" is penalised less than "wrong
    by two"), which is why Groll/Ley papers report it for international football."""
    f1, f2 = p_a, p_a + p_draw
    o1 = 1.0 if outcome == 0 else 0.0
    o2 = 1.0 if outcome <= 1 else 0.0
    return 0.5 * ((f1 - o1) ** 2 + (f2 - o2) ** 2)


def multiclass_brier(p_a: float, p_draw: float, p_b: float, outcome: int) -> float:
    """MSE of the 3-vector vs the one-hot outcome. Same convention as the
    project's prediction ledger (CLAUDE.md)."""
    target = [0.0, 0.0, 0.0]
    target[outcome] = 1.0
    return sum((p - t) ** 2 for p, t in zip((p_a, p_draw, p_b), target))


def logloss(p_a: float, p_draw: float, p_b: float, outcome: int) -> float:
    p = (p_a, p_draw, p_b)[outcome]
    return -math.log(max(p, 1e-15))


def evaluate_arrays(probs: list, outcomes: list) -> dict:
    """Mean RPS / logloss / Brier over a slice. ``probs`` is a list of
    (p_a, p_draw, p_b) triples; ``outcomes`` parallel list of 0/1/2 labels."""
    n = len(outcomes)
    if n == 0:
        return {"n": 0, "rps": float("nan"), "logloss": float("nan"), "brier": float("nan")}
    r = sum(rps(*p, o) for p, o in zip(probs, outcomes)) / n
    ll = sum(logloss(*p, o) for p, o in zip(probs, outcomes)) / n
    br = sum(multiclass_brier(*p, o) for p, o in zip(probs, outcomes)) / n
    return {"n": n, "rps": r, "logloss": ll, "brier": br}


# ---------------------------------------------------------------- training (lazy deps)

def _ensure_deps():
    try:
        import numpy  # noqa: F401
        import xgboost  # noqa: F401
    except ImportError as e:
        raise SystemExit(
            "fit_hybrid.py requires xgboost + numpy. Install: pip install xgboost numpy\n"
            f"(import failed: {e})")


def train_booster(rows_train: list, cfg: pr.Config, *,
                  feature_names=hy.FEATURES, tau_days: float = TAU_DAYS,
                  max_depth: int = 4, n_rounds: int = 200,
                  reg_lambda: float = 1.0):
    """Fit XGBoost on the training rows. Returns (booster, feature_names)."""
    _ensure_deps()
    import numpy as np
    import xgboost as xgb

    names = list(feature_names)
    X = np.asarray([feature_vector(r, cfg, feature_names=names) for r in rows_train], dtype=float)
    y = np.asarray([r["outcome"] for r in rows_train], dtype=int)
    # weights: time decay, with reference = the most recent training match
    ref = max(r["date"] for r in rows_train)
    w = np.asarray(time_decay_weights(rows_train, ref, tau_days=tau_days), dtype=float)

    dtrain = xgb.DMatrix(X, label=y, weight=w, feature_names=names)
    params = {
        "objective": "multi:softprob", "num_class": 3,
        "eval_metric": "mlogloss",
        "max_depth": max_depth, "eta": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "reg_lambda": reg_lambda, "min_child_weight": 10.0,
        "tree_method": "hist",
        "verbosity": 0,
    }
    booster = xgb.train(params, dtrain, num_boost_round=n_rounds)
    return booster, names


def booster_predict_proba(booster, rows: list, cfg: pr.Config, *,
                          feature_names=hy.FEATURES) -> list:
    import numpy as np
    import xgboost as xgb
    if not rows:
        return []
    names = list(feature_names)
    X = np.asarray([feature_vector(r, cfg, feature_names=names) for r in rows], dtype=float)
    dm = xgb.DMatrix(X, feature_names=names)
    P = booster.predict(dm)
    return [(float(p[0]), float(p[1]), float(p[2])) for p in P]


def structural_probs(rows: list, cfg: pr.Config) -> list:
    """Same probabilities the structural features carry into the booster — the
    correct A/B baseline (gain = pure overlay contribution)."""
    out = []
    for r in rows:
        s = synthesize_structural(r, cfg)
        out.append((s["p_a"], s["p_draw"], s["p_b"]))
    return out


# ---------------------------------------------------------------- main

FEATURE_SETS = {"full": hy.FEATURES_FULL, "lean": hy.FEATURES_LEAN,
                "minimal": hy.FEATURES_MINIMAL}


def _apply_iso_triple(per_class: list, triple: tuple) -> tuple:
    """At-eval-time isotonic application; reuses hybrid.apply_isotonic so the
    train-time and predict-time math are byte-identical (single source of truth)."""
    ca = hy.apply_isotonic(per_class[0], triple[0])
    cd = hy.apply_isotonic(per_class[1], triple[1])
    cb = hy.apply_isotonic(per_class[2], triple[2])
    z = ca + cd + cb
    return (triple if z <= 0 else (ca / z, cd / z, cb / z))


def _apply_platt_triple(per_class: list, triple: tuple) -> tuple:
    ca = hy.apply_platt(tuple(per_class[0]), triple[0])
    cd = hy.apply_platt(tuple(per_class[1]), triple[1])
    cb = hy.apply_platt(tuple(per_class[2]), triple[2])
    z = ca + cd + cb
    return (triple if z <= 0 else (ca / z, cd / z, cb / z))


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit the XGBoost hybrid overlay.")
    ap.add_argument("--corpus", type=Path, default=CORPUS)
    ap.add_argument("--start", default=WINDOW_START)
    ap.add_argument("--holdout", default=HOLDOUT_FROM)
    ap.add_argument("--feature-set", choices=sorted(FEATURE_SETS), default="full",
                    help="which FEATURES_* tuple from hybrid.py to fit on (default: full)")
    ap.add_argument("--tau-years", type=float, default=TAU_DAYS / DAYS_PER_YEAR,
                    help="time-decay half-life in years (default: 1.5)")
    ap.add_argument("--max-depth", type=int, default=4,
                    help="XGBoost tree depth (default: 4)")
    ap.add_argument("--n-rounds", type=int, default=200,
                    help="XGBoost num_boost_round (default: 200)")
    ap.add_argument("--reg-lambda", type=float, default=1.0,
                    help="XGBoost L2 regularization on weights (default: 1.0)")
    ap.add_argument("--major-only", action="store_true",
                    help="train ONLY on major tournaments (WC/Euro/Copa) — tests "
                         "the regime-transfer hypothesis at the cost of sample size")
    ap.add_argument("--calibrate", choices=("none", "isotonic", "platt"), default="none",
                    help="post-hoc probability calibration on a HOLDOUT-OF-TRAIN "
                         "slice (default: none). Standard fix for tree-booster over-confidence.")
    ap.add_argument("--calibration-from", default="2022-01-01",
                    help="cutoff date: matches >= this go into the calibration "
                         "set, matches < this train the booster (default: 2022-01-01)")
    ap.add_argument("--output-dir", type=Path, default=ARTIFACT.parent,
                    help="where to write artifact + meta + fit.log "
                         "(default: data/calibration — production location)")
    ap.add_argument("--name", default=None,
                    help="experiment_name in meta (default: basename of output-dir)")
    ap.add_argument("--label", default=None,
                    help="meta `source` label (default: auto from feature-set + tau)")
    ap.add_argument("--write", action="store_true",
                    help="persist artifact + meta if the deployment-slice gate passes")
    ap.add_argument("--force", action="store_true",
                    help="with --write: persist EVEN IF the bar fails (experiments only)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if not args.corpus.exists():
        print(f"error: corpus not found at {args.corpus}\n"
              "  Get it (public CC0): curl -sSL -o data/History/results.csv \\\n"
              "    https://raw.githubusercontent.com/martj42/international_results/master/results.csv",
              file=sys.stderr)
        return 1

    # 1. curate (reuses fit_rho's definition — drop friendlies/NA, ≥ start)
    matches = fr.load_curated(args.corpus, args.start)
    if len(matches) < 1000:
        print(f"error: only {len(matches)} curated matches since {args.start} — too few", file=sys.stderr)
        return 1

    # 2. roll Elo chronologically; snapshot pre-update values into feature rows
    rows = roll_elo(matches)
    rows = rows[BURN_IN_MATCHES:]   # drop cold-start noise
    train_rows = [r for r in rows if r["date"] < args.holdout]
    test_rows = [r for r in rows if r["date"] >= args.holdout]
    if args.major_only:
        train_rows = [r for r in train_rows if r["tournament"] in hy.MAJOR_TOURNAMENTS]
        # holdout stays broad — we still report all-competitive AND major slices
    min_train = 300 if args.major_only else 1000
    if len(train_rows) < min_train or len(test_rows) < 200:
        print(f"error: train/holdout split too thin ({len(train_rows)}/{len(test_rows)}, "
              f"min_train={min_train})", file=sys.stderr)
        return 1

    cfg = pr.Config()    # default constants used by the live model
    feature_set_name = args.feature_set
    feature_names = list(FEATURE_SETS[feature_set_name])
    tau_days = args.tau_years * DAYS_PER_YEAR

    # Calibration split (a no-op when --calibrate=none): hold OUT a recent slice
    # of training matches that the BOOSTER doesn't see, fit the calibrator there.
    # In-sample calibration would be silent overfitting (tree boosters are
    # near-perfect on their training data — the calibration curve degenerates).
    calib_rows: list = []
    if args.calibrate != "none":
        calib_rows = [r for r in train_rows if r["date"] >= args.calibration_from]
        train_rows = [r for r in train_rows if r["date"] < args.calibration_from]
        if len(calib_rows) < 300:
            print(f"error: calibration slice too thin "
                  f"({len(calib_rows)} matches >= {args.calibration_from})",
                  file=sys.stderr)
            return 1

    print(f"corpus: {len(matches)} curated, {len(rows)} after burn-in "
          f"({args.start}..{max(r['date'] for r in rows)})")
    print(f"split:  train {len(train_rows)} (<{args.holdout}) / holdout {len(test_rows)} (>={args.holdout})"
          f"{' [MAJOR-ONLY training]' if args.major_only else ''}")
    if args.calibrate != "none":
        print(f"        calibration {len(calib_rows)} ({args.calibration_from}..<{args.holdout})")
    print(f"config: feature-set={feature_set_name} ({len(feature_names)} features), "
          f"tau={args.tau_years:.2f}y, depth={args.max_depth}, n_rounds={args.n_rounds}, "
          f"reg_lambda={args.reg_lambda}, calibrate={args.calibrate}")
    print(f"output-dir: {args.output_dir}")

    # 3. train booster (lazy deps)
    print("training XGBoost overlay …")
    booster, _ = train_booster(train_rows, cfg,
                               feature_names=feature_names, tau_days=tau_days,
                               max_depth=args.max_depth, n_rounds=args.n_rounds,
                               reg_lambda=args.reg_lambda)

    # 3b. fit calibrator on the held-back calibration slice
    calibration_payload = None
    apply_calibration = None
    if args.calibrate != "none":
        print(f"fitting {args.calibrate} calibration on {len(calib_rows)} matches …")
        cal_raw = booster_predict_proba(booster, calib_rows, cfg, feature_names=feature_names)
        cal_outcomes = [r["outcome"] for r in calib_rows]
        per_class = []
        for c in (0, 1, 2):
            xs = [t[c] for t in cal_raw]
            ys = [1.0 if o == c else 0.0 for o in cal_outcomes]
            per_class.append(fit_isotonic(xs, ys) if args.calibrate == "isotonic"
                             else fit_platt(xs, ys))
        calibration_payload = {"method": args.calibrate, "per_class": per_class}
        if args.calibrate == "isotonic":
            apply_calibration = lambda triple: _apply_iso_triple(per_class, triple)
        else:
            apply_calibration = lambda triple: _apply_platt_triple(per_class, triple)

    # 4. evaluate on holdout: hybrid vs structural, on both the broad
    #    competitive slice and the major-tournament deployment slice
    print("evaluating on holdout …")
    hyb_raw = booster_predict_proba(booster, test_rows, cfg, feature_names=feature_names)
    hyb_probs = [apply_calibration(t) for t in hyb_raw] if apply_calibration else hyb_raw
    str_probs = structural_probs(test_rows, cfg)
    outcomes = [r["outcome"] for r in test_rows]

    def slice_metrics(label: str, mask: list) -> tuple:
        idxs = [i for i, keep in enumerate(mask) if keep]
        h = evaluate_arrays([hyb_probs[i] for i in idxs], [outcomes[i] for i in idxs])
        s = evaluate_arrays([str_probs[i] for i in idxs], [outcomes[i] for i in idxs])
        rps_delta = h["rps"] - s["rps"]
        rps_pct = 100.0 * rps_delta / s["rps"] if s["n"] else 0.0
        print(f"  {label:24s} n={h['n']:>5d}  "
              f"RPS {s['rps']:.5f} -> {h['rps']:.5f}  delta {rps_delta:+.5f} ({rps_pct:+.2f}%)  "
              f"logloss {s['logloss']:.4f} -> {h['logloss']:.4f}  "
              f"Brier {s['brier']:.4f} -> {h['brier']:.4f}")
        return h, s

    # broad: every competitive holdout match
    h_all, s_all = slice_metrics("all competitive", [True] * len(test_rows))
    # deployment: major tournaments only — the headline gate
    major_mask = [r["tournament"] in hy.MAJOR_TOURNAMENTS for r in test_rows]
    h_maj, s_maj = slice_metrics("major tournaments", major_mask)
    # per-year diagnostic on the deployment slice (noise estimate)
    years = sorted({r["date"][:4] for r in test_rows if r["tournament"] in hy.MAJOR_TOURNAMENTS})
    year_deltas = []
    for y in years:
        mask = [r["date"][:4] == y and r["tournament"] in hy.MAJOR_TOURNAMENTS for r in test_rows]
        idxs = [i for i, k in enumerate(mask) if k]
        if not idxs:
            continue
        h = evaluate_arrays([hyb_probs[i] for i in idxs], [outcomes[i] for i in idxs])
        s = evaluate_arrays([str_probs[i] for i in idxs], [outcomes[i] for i in idxs])
        if h["n"] >= 10:    # only report when the sample is non-trivial
            print(f"    {y}: n={h['n']:>3d}  RPS {s['rps']:.4f} -> {h['rps']:.4f}  delta {h['rps']-s['rps']:+.4f}")
            year_deltas.append(h["rps"] - s["rps"])

    # 5. acceptance: same shape as fit_rho — strict improvement on the
    #    deployment slice, on both RPS and logloss. The per-year std is a
    #    diagnostic noise estimate; full block-resample is a v2 upgrade.
    improves_major = (h_maj["n"] > 0 and h_maj["rps"] < s_maj["rps"] and
                      h_maj["logloss"] < s_maj["logloss"])
    noise_std = stats.stdev(year_deltas) if len(year_deltas) >= 2 else float("nan")
    margin = (s_maj["rps"] - h_maj["rps"]) if h_maj["n"] else float("nan")

    print()
    print(f"verdict: deployment-slice hybrid {'IMPROVES' if improves_major else 'does NOT improve'} "
          f"vs structural (RPS margin {margin:+.5f}; per-year noise σ {noise_std:.5f}).")

    if args.write:
        if not improves_major and not args.force:
            print("\nNOT writing artifact — acceptance bar failed. Hybrid stays inert.")
            print("  (pass --force to write anyway; use --output-dir for experiment runs)")
            return 0
        today = datetime.now(timezone.utc).date().isoformat()
        label = args.label or f"xgb-{feature_set_name}-tau{args.tau_years:g}"
        out_dir = args.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        art_path = out_dir / "hybrid.ubj"
        meta_path = out_dir / "hybrid.meta.json"
        booster.save_model(str(art_path))
        meta = {
            "experiment_name": args.name or out_dir.name,
            "source": f"{label} fit {today}",
            "asof": today,
            "feature_set": feature_set_name,
            "feature_names": feature_names,
            "major_only_training": bool(args.major_only),
            "n_train": len(train_rows), "n_holdout": len(test_rows),
            "n_holdout_major": h_maj["n"],
            "rps_struct_all": s_all["rps"],   "rps_hybrid_all": h_all["rps"],
            "rps_struct_major": s_maj["rps"], "rps_hybrid_major": h_maj["rps"],
            "logloss_struct_all": s_all["logloss"], "logloss_hybrid_all": h_all["logloss"],
            "logloss_struct_major": s_maj["logloss"], "logloss_hybrid_major": h_maj["logloss"],
            "brier_struct_all": s_all["brier"], "brier_hybrid_all": h_all["brier"],
            "brier_struct_major": s_maj["brier"], "brier_hybrid_major": h_maj["brier"],
            "noise_std_per_year": noise_std,
            "acceptance_passed": bool(improves_major),
            "forced_write": bool(args.force and not improves_major),
            "calibration": calibration_payload,
            "n_calibration": len(calib_rows),
            "params": {"objective": "multi:softprob", "num_round": args.n_rounds,
                       "max_depth": args.max_depth, "eta": 0.05,
                       "reg_lambda": args.reg_lambda,
                       "tau_days": tau_days, "tau_years": args.tau_years,
                       "hfa_elo_update": HFA_ELO, "burn_in_matches": BURN_IN_MATCHES},
            "corpus": "martj42/international_results (CC0)",
            "window": [args.start, max(r["date"] for r in rows)],
        }
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        rel_art = art_path.relative_to(REPO) if art_path.is_relative_to(REPO) else art_path
        rel_meta = meta_path.relative_to(REPO) if meta_path.is_relative_to(REPO) else meta_path
        print(f"\nwrote {rel_art} and {rel_meta}")
        print(f"  source = {meta['source']}")
        if args.force and not improves_major:
            print("  WARNING: artifact written despite failing the acceptance bar (--force).")
    else:
        print("\n(dry run — pass --write to persist the artifact if it cleared the bar)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
