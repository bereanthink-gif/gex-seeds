#!/usr/bin/env python3
"""
GEX -> Pine Seeds publisher
===========================

Fetches option chains (CBOE free delayed-quotes API), computes daily GEX
levels per the GEXStream methodology, and writes them as Pine Seeds CSV
symbols so TradingView strategies can request them with request.security().

Methodology (from GEXStreamMethodology.txt):
    GEX per contract        = gamma * open_interest * 100
    Net GEX at a strike     = call GEX - put GEX
    GEX ratio               = total call GEX / (total call GEX + total put GEX)
    Zero gamma (flip)       = strike where CUMULATIVE net GEX crosses zero
                              (linear interpolation between bracketing strikes)
    Call wall               = strike with the largest call GEX
    Put wall                = strike with the largest put GEX
    Extras X1 / X2          = 2nd-largest positive / negative net-GEX strikes

Published symbols per underlying (levels in UNDERLYING price terms):
    <U>ZG  zero gamma      <U>CW  call wall      <U>PW  put wall
    <U>X1  2nd +GEX strike <U>X2  2nd -GEX strike
    <U>GR  GEX ratio x 100 (62.4 = calls 62.4% of total gamma)

Each CSV row: YYYYMMDDT,open,high,low,close,volume with o=h=l=c=level
(single-value encoding keeps every row valid against OHLC sanity checks).

Usage:
    python scripts/update_seeds.py --symbols QQQ,SPY,GLD
    python scripts/update_seeds.py --symbols QQQ --expiries near --dry-run
"""

import argparse
import datetime as dt
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
OPT_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
INFO_DIR = REPO_ROOT / "symbol_info"
# Pine Seeds caps a repo at 6,000 data elements total. 18 symbols x 300 rows
# = 5,400, leaving headroom. ~300 rows ≈ 14 months of daily history each.
MAX_ROWS = 300

SUFFIXES = {
    "ZG": "zero gamma flip level",
    "CW": "call wall (max call GEX strike)",
    "PW": "put wall (max put GEX strike)",
    "X1": "2nd largest positive net-GEX strike",
    "X2": "2nd largest negative net-GEX strike",
    "GR": "GEX ratio x 100",
}


def _bs_gamma(s: float, k: float, t: float, iv: float, r: float = 0.045) -> float:
    if s <= 0 or k <= 0 or t <= 0 or iv <= 0:
        return 0.0
    sq = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * iv * iv) * t) / sq
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2.0 * math.pi) * s * sq)


def _gamma_flip(contracts: list, spot: float, span: float = 0.10, steps: int = 80):
    """Net dealer gamma profile over a spot grid; return zero-crossing nearest spot."""
    if not contracts or spot <= 0:
        return None
    lo, hi = spot * (1 - span), spot * (1 + span)
    step = (hi - lo) / steps
    grid, prof = [], []
    for i in range(steps + 1):
        s = lo + i * step
        tot = 0.0
        for k, t, iv, sign, oi in contracts:
            tot += sign * _bs_gamma(s, k, t, iv) * oi * 100.0
        grid.append(s)
        prof.append(tot)
    crossings = []
    for i in range(1, len(prof)):
        a, b = prof[i - 1], prof[i]
        if a < 0 <= b or a >= 0 > b:
            frac = -a / (b - a) if b != a else 0.0
            crossings.append(grid[i - 1] + frac * step)
    if not crossings:
        return None
    return min(crossings, key=lambda x: abs(x - spot))


def fetch_chain(sym: str) -> dict:
    url = CBOE_URL.format(sym=sym.upper())
    req = urllib.request.Request(url, headers={"User-Agent": "gex-seeds/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def compute_levels(payload: dict, expiries: str = "all", band: float = 0.25,
                   level_band: float = 0.05) -> dict:
    data = payload.get("data", {})
    options = data.get("options", [])
    spot = data.get("close") or data.get("current_price") or 0.0
    if not options:
        raise ValueError("no options in payload")

    # Optionally restrict to the nearest expiration
    if expiries == "near":
        dates = set()
        for o in options:
            m = OPT_RE.match(o.get("option", ""))
            if m:
                dates.add(m.group("date"))
        near = min(dates) if dates else None
        options = [o for o in options if near and near in o.get("option", "")]

    # Band-limit to strikes near spot: far-OTM LEAPS strikes carry stray
    # gamma that produces meaningless zero-gamma crossings way off-market.
    lo_k = spot * (1.0 - band) if spot else 0.0
    hi_k = spot * (1.0 + band) if spot else float("inf")

    call_gex = {}   # strike -> gamma exposure from calls
    put_gex = {}    # strike -> gamma exposure from puts
    contracts = []  # (strike, T_years, iv, +1 call / -1 put, oi) for the flip calc
    today = dt.date.today()
    for o in options:
        m = OPT_RE.match(o.get("option", ""))
        if not m:
            continue
        strike = int(m.group("strike")) / 1000.0
        if not (lo_k <= strike <= hi_k):
            continue
        gamma = o.get("gamma") or 0.0
        oi = o.get("open_interest") or 0
        iv = o.get("iv") or 0.0
        gex = gamma * oi * 100.0
        if oi <= 0:
            continue
        is_call = m.group("cp") == "C"
        if gex > 0:
            book = call_gex if is_call else put_gex
            book[strike] = book.get(strike, 0.0) + gex
        try:
            exp = dt.datetime.strptime(m.group("date"), "%y%m%d").date()
        except ValueError:
            continue
        t_years = (exp - today).days / 365.0
        if t_years > 0 and iv > 0:
            contracts.append((strike, t_years, iv, 1 if is_call else -1, oi))

    strikes = sorted(set(call_gex) | set(put_gex))
    if not strikes:
        raise ValueError("no strikes with open interest")

    net = {k: call_gex.get(k, 0.0) - put_gex.get(k, 0.0) for k in strikes}
    tot_call = sum(call_gex.values())
    tot_put = sum(put_gex.values())
    ratio = tot_call / (tot_call + tot_put) if (tot_call + tot_put) > 0 else 0.5

    # Zero gamma (gamma flip), computed properly: re-price every contract's
    # Black-Scholes gamma at hypothetical spot levels S across a grid around
    # spot, sum net dealer gamma (calls +, puts -) at each S, and find where
    # the profile crosses zero — the crossing nearest spot wins.
    zero_gamma = _gamma_flip(contracts, spot)
    if zero_gamma is None:
        # fallback: per-strike net GEX sign change nearest spot
        zero_gamma = spot
        best_d = float("inf")
        for a, b in zip(strikes, strikes[1:]):
            if net[a] < 0 <= net[b] or net[a] >= 0 > net[b]:
                mid = (a + b) / 2.0
                if abs(mid - spot) < best_d:
                    best_d, zero_gamma = abs(mid - spot), mid

    # Walls and extra strikes must be TRADEABLE levels: rank only strikes
    # within level_band of spot. Deep-OTM strikes carry huge OI (hedges,
    # lottery tickets) and would otherwise win the ranking with levels that
    # are useless intraday (e.g. an 800 "wall" with spot at 721).
    lo_lv = spot * (1.0 - level_band) if spot else 0.0
    hi_lv = spot * (1.0 + level_band) if spot else float("inf")
    call_lv = {k: v for k, v in call_gex.items() if lo_lv <= k <= hi_lv} or call_gex
    put_lv = {k: v for k, v in put_gex.items() if lo_lv <= k <= hi_lv} or put_gex
    strikes_lv = sorted(set(call_lv) | set(put_lv))
    net_lv = {k: call_lv.get(k, 0.0) - put_lv.get(k, 0.0) for k in strikes_lv}

    call_wall = max(call_lv, key=call_lv.get) if call_lv else 0.0
    put_wall = max(put_lv, key=put_lv.get) if put_lv else 0.0

    pos_sorted = sorted((k for k in strikes_lv if net_lv[k] > 0), key=lambda k: net_lv[k], reverse=True)
    neg_sorted = sorted((k for k in strikes_lv if net_lv[k] < 0), key=lambda k: net_lv[k])
    x1 = next((k for k in pos_sorted if k != call_wall), 0.0)
    x2 = next((k for k in neg_sorted if k != put_wall), 0.0)

    return {
        "ZG": round(zero_gamma, 2),
        "CW": round(call_wall, 2),
        "PW": round(put_wall, 2),
        "X1": round(x1, 2),
        "X2": round(x2, 2),
        "GR": round(ratio * 100.0, 2),
        "_spot": spot,
        "_total_call_gex": tot_call,
        "_total_put_gex": tot_put,
    }


def write_seed_csv(ticker: str, value: float, day: dt.date) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{ticker}.csv"
    stamp = day.strftime("%Y%m%dT")
    row = f"{stamp},{value},{value},{value},{value},0"
    rows = []
    if path.exists():
        rows = [ln for ln in path.read_text().splitlines() if ln.strip()]
        rows = [ln for ln in rows if not ln.startswith(stamp)]  # replace today's row
    rows.append(row)
    rows = rows[-MAX_ROWS:]
    path.write_text("\n".join(rows) + "\n")


def write_symbol_info(repo_name: str, underlyings: list) -> None:
    INFO_DIR.mkdir(parents=True, exist_ok=True)
    symbols, descriptions = [], []
    for u in underlyings:
        for suf, desc in SUFFIXES.items():
            symbols.append(f"{u}{suf}")
            descriptions.append(f"{u} {desc}")
    # Pine Seeds symbol_info spec: exactly these fields, arrays of equal length
    info = {
        "symbol": symbols,
        "description": descriptions,
        "pricescale": [100] * len(symbols),
    }
    (INFO_DIR / f"{repo_name}.json").write_text(json.dumps(info, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="QQQ,SPY,GLD")
    ap.add_argument("--expiries", choices=["all", "near"], default="all")
    ap.add_argument("--band", type=float, default=0.25,
                    help="only use strikes within this fraction of spot (default 0.25)")
    ap.add_argument("--level-band", type=float, default=0.05,
                    help="walls/extras must be within this fraction of spot (default 0.05)")
    ap.add_argument("--repo-name", default=REPO_ROOT.name,
                    help="Pine Seeds repo name (for symbol_info json filename)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    day = dt.date.today()
    underlyings = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    failures = []
    results = {}

    for u in underlyings:
        try:
            levels = compute_levels(fetch_chain(u), args.expiries, args.band, args.level_band)
        except Exception as e:  # keep going: one bad chain shouldn't kill the rest
            print(f"[FAIL] {u}: {e}", file=sys.stderr)
            failures.append(u)
            continue
        results[u] = levels
        print(f"[{u}] spot={levels['_spot']}  ZG={levels['ZG']}  CW={levels['CW']}  "
              f"PW={levels['PW']}  X1={levels['X1']}  X2={levels['X2']}  GR={levels['GR']}")
        if not args.dry_run:
            for suf in SUFFIXES:
                write_seed_csv(f"{u}{suf}", levels[suf], day)

    if not args.dry_run and results:
        write_symbol_info(args.repo_name, underlyings)
        # Paste-ready strings for the strategies' "Paste String" GEX source:
        # one line per underlying, ZG,CW,PW,X1,X2 in underlying price terms.
        lines = [f"{u}={lv['ZG']},{lv['CW']},{lv['PW']},{lv['X1']},{lv['X2']},{lv['GR']}"
                 for u, lv in results.items()]
        lines.append("# GR: " + " | ".join(f"{u} {lv['GR']}" for u, lv in results.items())
                     + f"  (updated {day.isoformat()})")
        (REPO_ROOT / "levels.txt").write_text("\n".join(lines) + "\n")

    return 1 if len(failures) == len(underlyings) else 0


if __name__ == "__main__":
    sys.exit(main())
