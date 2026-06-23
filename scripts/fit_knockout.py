"""fit_knockout.py — calibrate the knockout layer (MODEL_IMPROVEMENTS 2.5).

The 1.2 mechanism (resolve_knockout: ET caution + flat-0.5 shootout) shipped with hand-set
defaults. The 2026-06-21 diagnostic showed it UNDER-predicts extra time and shootouts (model
reach-ET 24% vs the historical ~32%; reach-shootout 13% vs ~20-25%). This fits three knobs to
those frequency bands, on a representative field (top-N teams by consensus strength, all
pairwise, neutral):

  ko_mu_factor — a SMALL goal cut so the knockout total lands at the historical ~2.4 (knockouts
                 are lower-scoring than groups).
  ko_rho       — a knockout-only Dixon-Coles draw-clustering term that lifts reach-ET at CONSTANT
                 goals (the right lever for caginess; a μ cut alone over-cuts goals and still
                 under-shoots). Capped at -0.20 for DC numerical stability.
  et_caution   — fit to the reach-shootout band given the above.

These are resolve_knockout-only — group predict_match is byte-identical. No knockout games have
been played yet, so the "validate-narrow" half of 2.5 (advance-prob reliability on held-out
knockout matches) awaits stage-labeled data; this fits to the published frequency bands only.

Usage:  python scripts/fit_knockout.py [--field 24] [--write]
"""
from __future__ import annotations
import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import predict as P
import eval_blend as EB

CALIBRATION = P.REPO_ROOT / "data" / "calibration.json"
TARGET_ET = 0.32
TARGET_SO = 0.22          # mid of the 0.20-0.25 band
TARGET_TOTAL = 2.4        # historical knockout goals/match


def field(n: int) -> list[str]:
    m = P.load_ratings()
    return sorted(m.teams, key=lambda t: -m.teams[t].strength)[:n]


def freqs(teams, mf, rho, c):
    cfg = EB.build_config(1.0, 1.5)
    cfg.ko_mu_factor, cfg.ko_rho, cfg.et_caution = mf, rho, c
    m = P.load_ratings(config=cfg)
    et, so, tot = [], [], []
    for i, a in enumerate(teams):
        for b in teams[i + 1:]:
            kp = P.resolve_knockout(m, a, b)
            et.append(kp.p_reach_et); so.append(kp.p_reach_shootout); tot.append(kp.reg.total * mf)
    return st.mean(et), st.mean(so), st.mean(tot)


def fit(teams):
    # 1) ko_mu_factor: hit the ~2.4 KO total (small cut)
    mf = min((round(m, 2) for m in [x / 100 for x in range(85, 101)]),
             key=lambda mf: abs(freqs(teams, mf, 0.0, 0.85)[2] - TARGET_TOTAL))
    # 2) ko_rho: lift reach-ET toward 0.32 at that μ (cap -0.20)
    rho = min((round(r, 2) for r in [-x / 100 for x in range(0, 21)]),
              key=lambda r: abs(freqs(teams, mf, r, 0.85)[0] - TARGET_ET))
    # 3) et_caution: hit the reach-SO band
    c = min((round(c, 2) for c in [x / 100 for x in range(50, 86)]),
            key=lambda c: abs(freqs(teams, mf, rho, c)[1] - TARGET_SO))
    return mf, rho, c


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field", type=int, default=24)
    ap.add_argument("--write", action="store_true", help="merge knobs into data/calibration.json")
    args = ap.parse_args()
    teams = field(args.field)
    base = freqs(teams, 1.0, 0.0, 0.85)
    mf, rho, c = fit(teams)
    et, so, tot = freqs(teams, mf, rho, c)
    print(f"baseline (inert): reach-ET {base[0]*100:.1f}%  reach-SO {base[1]*100:.1f}%  total {base[2]:.2f}")
    print(f"fit: ko_mu_factor={mf}  ko_rho={rho}  et_caution={c}")
    print(f" ->  reach-ET {et*100:.1f}% (target {TARGET_ET*100:.0f})  "
          f"reach-SO {so*100:.1f}% (target 20-25)  KO total {tot:.2f} (target {TARGET_TOTAL})")
    if args.write:
        cal = json.loads(CALIBRATION.read_text(encoding="utf-8")) if CALIBRATION.exists() else {}
        cal.update(ko_mu_factor=mf, ko_rho=rho, et_caution=c)
        CALIBRATION.write_text(json.dumps(cal, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {CALIBRATION.name}")
    else:
        print("(dry run — pass --write to persist; the values are already active in calibration.json)")


if __name__ == "__main__":
    main()
