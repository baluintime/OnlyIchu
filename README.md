# OnlyIchu

Automated **Ichimoku Cloud breakout options strategy** for Indian index derivatives on
**Upstox**, with **paper trading** (simulated fills on live market data) and **live
trading** (real orders) modes.

Implements the multi-timeframe execution protocol from
`ichimoku_cloud_options_strategy.pdf`:

- Ichimoku Cloud (Tenkan 9 / Kijun 26 / Senkou B 52 / displacement 26) evaluated
  **strictly on candle closes** — in-progress candles are ignored.
- **Two independent pipelines per index**: 1-minute and 5-minute, each with its own
  calculations, entries and exits.
- **LONG**: close strictly above Tenkan, Kijun, Span A, Span B (and the whole cloud)
  → buy an **ITM Call** at the open of the next candle.
  Exit the moment a close drops below **any single** level.
- **SHORT**: close strictly below all levels → buy an **ITM Put** at the next open.
  Exit the moment a close rises above **any single** level.
- **Option selection**: delta **0.65–0.75** (target 0.70) from the Upstox option
  chain greeks, nearest weekly/0DTE expiry, marketable **limit orders** with a
  narrow tolerance.

## Index universe

| Config name | Index | Options |
|---|---|---|
| NIFTY | Nifty 50 | weekly/monthly contracts |
| BANKNIFTY | Nifty Bank | monthly contracts |
| FINNIFTY | Nifty Financial Services | monthly contracts |
| MIDCPNIFTY | Nifty Midcap Select | monthly contracts |
| SMALLCAP | Nifty Smallcap 50 | **none listed on NSE** |
| LARGECAP | Nifty 100 | **none listed on NSE** |

> NSE only lists index options on Nifty, Bank Nifty, FinNifty, Midcap Select (and
> Nifty Next 50). Smallcap/largecap indices have **no option contracts**, so they
> are **signal-only**: their Ichimoku state and breakout signals are computed from
> real market data and shown on the dashboard, but no trade is ever placed for
> them (in either mode). Only real exchange-listed contracts are traded.
> Everything is configurable in `config.yaml`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your Upstox app credentials
```

Create an app at <https://account.upstox.com/developer/apps> and put its API key,
secret, and redirect URI in `.env`.

### Daily login (Upstox tokens expire every day ~3:30 AM IST)

```bash
python -m onlyichu login
```

Opens an OAuth URL; after approving, paste the `code` query parameter back into the
prompt. The access token is stored in `~/.onlyichu/credentials.json`.

### Instrument keys are auto-validated

Upstox index keys are exact strings with inconsistent casing (e.g.
`NSE_INDEX|Nifty 50` but `NSE_INDEX|NIFTY MID SELECT`). On startup, `run`, `web`
and `backtest` download Upstox's public instrument master, validate every
configured key, and auto-correct wrong ones by name match (a warning shows the
corrected key to put in `config.yaml`). An unresolvable index is disabled with
suggestions instead of erroring forever. To search keys manually:

```bash
python -m onlyichu instruments --index-only --search "smlcap"
```

## Run

```bash
# Paper trading (default; simulated fills at live LTP with slippage)
python -m onlyichu run --mode paper

# Live trading — places REAL orders with REAL money
python -m onlyichu run --mode live
```

Both modes need a valid access token (paper mode uses real market data). The engine:

1. warms up indicators from historical + intraday 1m candles,
2. polls Upstox for completed 1-minute candles, aggregates 5-minute candles locally,
3. runs both Ichimoku pipelines per index on every candle close,
4. selects an ITM option (delta 0.65–0.75, nearest expiry) and executes,
5. enforces the entry cutoff, daily trade caps, daily loss limit, and force
   square-off (defaults: no entries after 15:00, square-off 15:15 IST).

### Live web dashboard

```bash
python -m onlyichu web            # http://127.0.0.1:8080
```

A rich, animated, auto-refreshing page (no charts): it fetches historical +
intraday 1-minute candles from Upstox, computes the Ichimoku Cloud server-side
for every index on both the 1m and 5m timeframes, and shows per pipeline the
live signal (LONG / SHORT / NEUTRAL with glow animations), price position vs
each of the four levels (▲/▼ with distance % and meter bars), the Kumo
boundaries and bull/bear state, LTP with day change, market status, and the
account strip (cash, PnL, open positions). The browser polls `/api/dashboard`
every `web.refresh_seconds` (default 10s) with a countdown ring; values flash
green/red as they change. Host/port/refresh are set under `web:` in
`config.yaml` or via `--host/--port`.

**Paper/live switching from the page:** the *Trading engine* bar has a
PAPER | LIVE toggle plus START/STOP and SQUARE OFF ALL buttons. START in paper
mode begins simulated trading immediately; switching the toggle to LIVE and
pressing START opens a confirmation dialog where you must type `LIVE` — only
then are real orders enabled. To change mode while running, STOP first (open
positions are left untouched), pick the other mode, and START again. The
status pill shows which engine is running; the strip below mirrors the running
engine's positions and realized PnL.

### Other commands

```bash
python -m onlyichu status              # paper account: cash, PnL, open positions
python -m onlyichu backtest --days 10  # replay history through the signal logic
```

The backtest is an index-level approximation (points × target delta), useful for
checking signal frequency and direction — it does not model option premiums/theta.

## Configuration

Everything lives in `config.yaml` — mode, timeframes, Ichimoku periods, delta band,
order type/tolerance, lots per trade, risk caps, session times, and the index list.
Trade history is appended to `state/trades_paper.csv` / `state/trades_live.csv`;
paper account state persists across restarts in `state/paper_state.json`.

## Project layout

```
onlyichu/
  auth.py        Upstox OAuth login + token storage
  upstox_api.py  REST client (candles, quotes, option chain, orders)
  candles.py     candle series + 1m→5m aggregation
  ichimoku.py    Ichimoku math + strict close-based entry/exit rules
  strategy.py    per-index/per-timeframe pipelines emitting signals
  options.py     ITM strike selection by delta from the option chain
  broker.py      PaperBroker (simulated) and LiveBroker (real orders)
  engine.py      polling loop, session windows, risk guards, square-off
  backtest.py    historical replay of the signal logic
  web.py         Flask dashboard: candle fetch + Ichimoku snapshot API
  templates/     animated auto-refreshing dashboard page
  cli.py         login / instruments / run / web / backtest / status
```

## ⚠️ Disclaimer

This software is for educational purposes. Index options are highly leveraged
instruments; 1-minute breakout systems trade frequently and can lose money quickly
through spreads, slippage and theta. **Test thoroughly in paper mode first.** You are
solely responsible for any orders placed by live mode. Not investment advice.
