"""backtest_market.py — does blending the market consensus into a ratings model help, and at what weight?

Spun out of MODEL_IMPROVEMENTS.md §3.3. Our corpus (data/History/results.csv) has no odds,
so we cannot reverse-fit a market source on internationals directly. football-data.co.uk
gives a large, clean CLUB dataset with results AND closing odds (Avg* = consensus across
books; PS* = Pinnacle). We can't run the WC model on club teams, so we stand in a faithful
ANALOGUE of it — a rolled Elo turned into a Poisson W/D/L exactly like predict.py — and ask
the structural question the WC market overlay turns on:

    model-only   vs   market-only   vs   blend(w) = (1-w)*model + w*market

walk-forward / train-test split, scored by multiclass Brier, RPS (ordered), and log-loss,
with bootstrap CIs. The headline we want is the SHAPE: how much does the market beat the
model, where is the blend optimum, and — crucially — how FLAT is it (the forecast-combination
puzzle says a fixed sensible weight should be near-optimal and robust). The exact weight is a
CLUB number; we transfer the *methodology / robustness*, not the digits, to the WC overlay.

Usage:
    python scripts/backtest_market.py --download        # fetch league-seasons -> data/MarketHistory/
    python scripts/backtest_market.py                   # run the backtest on cached CSVs
"""
from __future__ import annotations
import argparse
import csv
import math
import random
import statistics as stats
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "data" / "MarketHistory"
BASE = "https://www.football-data.co.uk/mmz4281"

# big-5 + a few deep leagues; ~9 seasons each (1617..2425) -> ~15k matches with closing odds
LEAGUES = ["E0", "E1", "D1", "SP1", "I1", "F1", "N1", "P1"]
SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324", "2425"]

# ---- Elo roll -------------------------------------------------------------
K = 20.0
ELO_HFA = 65.0          # home edge baked into the Elo expectation during rolling
SEASON_REGRESS = 0.25   # between seasons, pull ratings 25% back to the league mean (1500)


def _expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** (-((ra + ELO_HFA) - rb) / 400.0))


def _gd_mult(gd: int) -> float:
    g = abs(gd)
    if g <= 1:
        return 1.0
    if g == 2:
        return 1.5
    return (11 + g) / 8.0


# ---- model: Elo -> Poisson W/D/L (mirrors predict.py architecture) --------
def model_wdl(elo_diff: float, theta: float, mu0: float, hfa: float,
              max_goals: int = 8) -> tuple[float, float, float]:
    sup = (elo_diff + hfa) / theta
    share = 1.0 / (1.0 + math.exp(-sup))
    lam_a, lam_b = mu0 * share, mu0 * (1 - share)
    pa = [math.exp(-lam_a) * lam_a ** i / math.factorial(i) for i in range(max_goals + 1)]
    pb = [math.exp(-lam_b) * lam_b ** j / math.factorial(j) for j in range(max_goals + 1)]
    ph = sum(pa[i] * pb[j] for i in range(max_goals + 1) for j in range(max_goals + 1) if i > j)
    pd = sum(pa[i] * pb[j] for i in range(max_goals + 1) for j in range(max_goals + 1) if i == j)
    paw = 1.0 - ph - pd
    return ph, pd, max(paw, 1e-9)


def devig(h: float, d: float, a: float) -> tuple[float, float, float]:
    inv = [1 / h, 1 / d, 1 / a]
    s = sum(inv)
    return inv[0] / s, inv[1] / s, inv[2] / s


# ---- scoring --------------------------------------------------------------
def outcome_idx(ftr: str) -> int:
    return {"H": 0, "D": 1, "A": 2}[ftr]


def brier(p, y):
    return sum((p[k] - (1 if k == y else 0)) ** 2 for k in range(3))


def logloss(p, y):
    return -math.log(max(1e-12, p[y]))


def rps(p, y):
    # ordered H,D,A; RPS = sum over k of (cumP - cumOutcome)^2, /(K-1)
    co = [1 if i >= y else 0 for i in range(3)]   # outcome CDF
    cp, cc, s = 0.0, 0.0, 0.0
    for k in range(3):
        cp += p[k]; cc += co[k]
        s += (cp - cc) ** 2
    return s / 2.0


def paired_bootstrap(diff, B=4000, seed=7):
    rnd = random.Random(seed)
    n = len(diff)
    ms = sorted(sum(diff[rnd.randrange(n)] for _ in range(n)) / n for _ in range(B))
    return ms[int(0.025 * B)], ms[int(0.975 * B)]


# ---- data -----------------------------------------------------------------
def download() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    ok = miss = 0
    for lg in LEAGUES:
        for sn in SEASONS:
            dest = CACHE / f"{lg}_{sn}.csv"
            if dest.exists() and dest.stat().st_size > 1000:
                ok += 1
                continue
            url = f"{BASE}/{sn}/{lg}.csv"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=30).read()
                if len(data) > 1000:
                    dest.write_bytes(data)
                    ok += 1
                else:
                    miss += 1
            except Exception as e:
                print(f"  miss {lg}_{sn}: {e}")
                miss += 1
    print(f"download: {ok} cached, {miss} missing")


def _parse_date(s: str):
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            import datetime
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def load_matches(odds_cols=("Avg", "B365", "PS")) -> list[dict]:
    """Rolled-Elo matches with pre-match ratings + de-vigged consensus, sorted by date."""
    rows_by_league: dict[str, list[dict]] = {}
    for f in sorted(CACHE.glob("*.csv")):
        lg = f.stem.split("_")[0]
        with f.open(encoding="latin-1", newline="") as fh:
            for r in csv.DictReader(fh):
                d = _parse_date(r.get("Date", ""))
                ft = (r.get("FTR") or "").strip()
                if not d or ft not in ("H", "D", "A"):
                    continue
                odds = None
                for pre in odds_cols:                    # first odds source that's fully present
                    try:
                        h, dr, a = (float(r[pre + "H"]), float(r[pre + "D"]), float(r[pre + "A"]))
                        if min(h, dr, a) > 1.0:
                            odds = (h, dr, a)
                            break
                    except (KeyError, ValueError, TypeError):
                        continue
                if odds is None:
                    continue
                try:
                    fthg, ftag = int(r["FTHG"]), int(r["FTAG"])
                except (KeyError, ValueError):
                    continue
                rows_by_league.setdefault(lg, []).append({
                    "date": d, "lg": lg, "home": r["HomeTeam"].strip(), "away": r["AwayTeam"].strip(),
                    "ftr": ft, "gd": fthg - ftag, "odds": odds, "season": f.stem.split("_")[1]})
    # roll Elo within each league, chronological, regress between seasons
    out = []
    for lg, rows in rows_by_league.items():
        rows.sort(key=lambda x: x["date"])
        elo: dict[str, float] = {}
        cur_season = None
        for m in rows:
            if m["season"] != cur_season:
                cur_season = m["season"]
                for t in elo:                            # mean-regress at season boundary
                    elo[t] = 1500 + (elo[t] - 1500) * (1 - SEASON_REGRESS)
            ra = elo.get(m["home"], 1500.0); rb = elo.get(m["away"], 1500.0)
            m["elo_diff"] = ra - rb
            m["warm"] = (m["home"] in elo and m["away"] in elo)
            # update
            exp_h = _expected(ra, rb)
            score_h = 1.0 if m["ftr"] == "H" else (0.5 if m["ftr"] == "D" else 0.0)
            delta = K * _gd_mult(m["gd"]) * (score_h - exp_h)
            elo[m["home"]] = ra + delta
            elo[m["away"]] = rb - delta
        out.extend(rows)
    out.sort(key=lambda x: x["date"])
    return [m for m in out if m["warm"]]                 # drop cold-start (first sighting) matches


# ---- fit + evaluate -------------------------------------------------------
def fit_model_params(train: list[dict]) -> tuple[float, float, float]:
    """Grid-search (theta, mu0, hfa) minimizing train multiclass log-loss. Few params,
    mirrors predict.py — a fair, low-overfit structural competitor to the market."""
    best, arg = 9e9, None
    for theta in (120, 160, 200, 240, 300):
        for mu0 in (2.4, 2.6, 2.8):
            for hfa in (40, 70, 100):
                ll = 0.0
                for m in train:
                    p = model_wdl(m["elo_diff"], theta, mu0, hfa)
                    ll += logloss(p, outcome_idx(m["ftr"]))
                if ll < best:
                    best, arg = ll, (theta, mu0, hfa)
    return arg


def evaluate(matches: list[dict], split=0.6, grid=None) -> None:
    grid = grid or [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]
    n = len(matches)
    cut = int(n * split)
    train, test = matches[:cut], matches[cut:]
    theta, mu0, hfa = fit_model_params(train)
    print(f"\nfit on {len(train)} train matches: theta={theta} mu0={mu0} hfa={hfa}")
    print(f"evaluating on {len(test)} held-out matches (chronological split @ {matches[cut]['date']})\n")

    # per-match model & market triples on the test set
    rows = []
    for m in test:
        y = outcome_idx(m["ftr"])
        md = model_wdl(m["elo_diff"], theta, mu0, hfa)
        mk = devig(*m["odds"])
        rows.append((md, mk, y))

    def metrics(pfun):
        b = [brier(pfun(md, mk), y) for md, mk, y in rows]
        r = [rps(pfun(md, mk), y) for md, mk, y in rows]
        l = [logloss(pfun(md, mk), y) for md, mk, y in rows]
        return stats.mean(b), stats.mean(r), stats.mean(l), b

    base_b = metrics(lambda md, mk: md)[3]   # model-only Brier, paired baseline
    mkt_b = metrics(lambda md, mk: mk)[3]
    print(f"  {'w_mkt':>6} {'Brier':>8} {'RPS':>8} {'LogLoss':>8}   {'dBrier vs model':>16}  {'dBrier vs market':>17}")
    best = (9e9, None)
    for w in grid:
        mb, mr, ml, bl = metrics(lambda md, mk: tuple((1 - w) * md[k] + w * mk[k] for k in range(3)))
        if mb < best[0]:
            best = (mb, w)
        dvm = stats.mean([x - y for x, y in zip(bl, base_b)])     # blend - model (<0 better than model)
        dvk = stats.mean([x - y for x, y in zip(bl, mkt_b)])      # blend - market (<0 better than market)
        tag = "  <- model" if w == 0 else ("  <- market" if w == 1 else ("  <- 50/50" if w == 0.5 else ""))
        print(f"  {w:>6.2f} {mb:>8.4f} {mr:>8.4f} {ml:>8.4f}   {dvm:>+16.4f}  {dvk:>+17.4f}{tag}")
    # CIs at the equal blend and at market-only, vs model
    half = [brier(tuple(0.5 * md[k] + 0.5 * mk[k] for k in range(3)), y) for md, mk, y in rows]
    d_half = [x - y for x, y in zip(half, base_b)]
    d_mkt = [x - y for x, y in zip(mkt_b, base_b)]
    print(f"\n  market-only vs model:  dBrier {stats.mean(d_mkt):+.4f}  95%CI {paired_bootstrap(d_mkt)}")
    print(f"  50/50 blend vs model:  dBrier {stats.mean(d_half):+.4f}  95%CI {paired_bootstrap(d_half)}")
    print(f"  best blend at w_mkt={best[1]} (Brier {best[0]:.4f}); "
          f"flatness: Brier(0.5)={metrics(lambda md,mk: tuple(0.5*md[k]+0.5*mk[k] for k in range(3)))[0]:.4f} "
          f"vs Brier(best)={best[0]:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--odds", default="Avg", help="odds source: Avg (consensus), PS (Pinnacle), B365")
    args = ap.parse_args()
    if args.download:
        download()
        return
    if not any(CACHE.glob("*.csv")):
        print("no cached data — run with --download first")
        return
    matches = load_matches(odds_cols=(args.odds, "Avg", "B365", "PS"))
    print(f"loaded {len(matches)} warm matches across {len(set(m['lg'] for m in matches))} leagues, "
          f"{matches[0]['date']}..{matches[-1]['date']}  (odds: {args.odds} consensus)")
    evaluate(matches, split=args.split)


if __name__ == "__main__":
    main()
