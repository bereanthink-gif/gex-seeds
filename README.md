# gex-seeds — daily GEX levels as TradingView Pine Seeds data

Publishes daily GEX/OI levels (zero gamma, call wall, put wall, extra ±GEX
strikes, GEX ratio) for **QQQ, SPY, GLD** as Pine Seeds symbols, so the
Auto Imbalance at Structure strategies load them automatically — no manual
input, and backtests get the historically-correct levels for each day.

- **Source**: CBOE free delayed-quotes API (per-contract gamma + open interest,
  no API key). Levels are computed pre-market from the latest OI.
- **Math**: GEXStream methodology — `GEX = gamma × OI × 100`, net per strike =
  calls − puts, zero gamma = cumulative-net crossing (interpolated),
  call/put wall = max call/put GEX strike.
- **Cadence**: GitHub Action runs ~08:35 ET each weekday, commits one row per
  symbol per day. TradingView ingests the repo as EOD data.

## Published symbols (per underlying U in QQQ/SPY/GLD)

| Ticker | Meaning |
|---|---|
| `U`ZG | zero gamma / flip level (underlying price) |
| `U`CW | call wall — max call GEX strike |
| `U`PW | put wall — max put GEX strike |
| `U`X1 | 2nd-largest positive net-GEX strike |
| `U`X2 | 2nd-largest negative net-GEX strike |
| `U`GR | GEX ratio × 100 (62.4 → calls hold 62.4% of total gamma) |

Every CSV row encodes one value as `o=h=l=c` so rows always pass OHLC
validation. Volume is 0.

## One-time setup

> **IMPORTANT (verified 2026-07-03):** TradingView has *suspended
> self-service creation* of new Pine Seeds repositories — the connected repo
> must be provisioned by TradingView. Email **pine.seeds@tradingview.com**
> requesting a Pine Seeds repo for your GitHub account (they quote ~1
> business day). Once they provision it, you fork it keeping the same name.

1. **Email pine.seeds@tradingview.com** (see above) and wait for the repo.
2. **Copy this project's contents** (`scripts/`, `.github/workflows/`,
   `data/`, `symbol_info/` — rename `symbol_info/gex-seeds.json` to
   `<provisioned_repo_name>.json`) into the provisioned repo and push.
   Their repo ships with "Check data" / "Upload data" workflows — leave
   "Check data" enabled per their docs (repo.md), and note their 6,000
   data-element cap (this project caps history at 300 rows x 18 symbols).
3. Seed it once manually: `python scripts/update_seeds.py --symbols QQQ,SPY,GLD`
   then commit `data/` + `symbol_info/`. (Run with `--dry-run` first to eyeball
   the levels against your trading-plan numbers.)
4. TradingView picks the data up as symbols named:
   `SEED_<your_github_username>_<repo_name>:QQQZG` etc.
   Search one in the TradingView symbol search to confirm ingestion (can take
   up to a day the first time).
5. In each AIAS strategy, set **GEX Source = Pine Seeds** and set the
   **Pine Seeds prefix** input to `SEED_<your_github_username>_<repo_name>`.

## Behavior notes

- The strategies fall back to the **manual GEX inputs per-level** whenever a
  seed value is missing (repo not ingested yet, TV sync late, holiday), and
  the diagnostics table shows which source is active.
- History accumulates one row per trading day — that's what makes GEX levels
  **backtestable**: each historical bar sees the levels that were true that day.
- `--expiries near` restricts the computation to the nearest expiration
  (0DTE-focused view); default `all` aggregates the whole chain.
