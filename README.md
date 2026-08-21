# OnlyIchu

Automated **Ichimoku Cloud breakout options strategy** for Indian index derivatives on
**Upstox**, with **paper trading** (simulated fills on live market data) and **live
trading** (real orders) modes.

Implements the multi-timeframe execution protocol from
`ichimoku_cloud_options_strategy.pdf`:

- Ichimoku Cloud evaluated **strictly on candle closes** — in-progress candles are
  ignored. Periods are configurable; the shipped default is the intraday
  noise-filter profile **Tenkan 12 / Kijun 24 / Senkou B 120 / displacement 24**
  (`strategy:` in `config.yaml` also documents the Standard 9/26/52/26 and Fast
  7/22/44/22 profiles).
- **Two independent pipelines per index**: 1-minute and 5-minute, each with its own
  calculations, entries and exits.
- **LONG**: close strictly above Tenkan, Kijun, Span A, Span B (and the whole cloud)
  → buy an **ITM Call** at the open of the next candle.
- **SHORT**: close strictly below all levels → buy an **ITM Put** at the next open.
- **Entry filters** (optional confluence, **all OFF by default**): entry always
  requires the close beyond all four Ichimoku levels; these only add extra
  confirmation on top — **Chikou span** clear of price N candles ago,
  **MACD(12,26,9) histogram** confirming direction, and a **minimum cloud
  thickness** gate. Each is a **live toggle on the dashboard** (persisted to
  `state/settings.json`) and also presettable in `config.yaml`
  (`use_macd_filter`, `use_chikou_filter`, `use_thickness_filter`). With all off,
  entry is the pure breakout. The MACD entry filter is separate from the
  MACD-slope **exit** (`exit_mode: macd`), which stays active regardless. The
  thickness gate is **per index** — each index moves on its own point scale
  (BANKNIFTY in the hundreds, MIDCPNIFTY in the tens), so set
  `min_cloud_thickness:` on each instrument in `config.yaml` (applied only when
  `use_thickness_filter` is on); an index without one inherits the global
  `strategy.min_cloud_thickness`.
- **Exit mode** (`exit_mode`, default `macd`):
  - `macd` — **MACD-histogram momentum fade** on the underlying index: exit a
    LONG the first time the current (closed) histogram is **less** than the prior
    candle's, a SHORT the first time it is **greater**; otherwise hold.
  - `kijun` — soft trailing stop on a **Kijun close** plus a hard stop at the
    **opposite Kumo edge** (instead of exiting on any single line).
  - `any_level` — original: exit on a close past any one of the four lines.

  A position that exits on a candle is never re-opened on the **same side on that
  same candle** (which would happen when the exit rule and the breakout entry
  disagree on one close — e.g. MACD ticks against a still-valid breakout). It
  goes flat instead; a genuine reversal to the opposite side on that candle is
  still taken.
- **Partial profit-taking**: book `partial_exit_fraction` (50%) of the position
  once the option premium gains `partial_target_pct` (15%); needs ≥2 lots.
- **Option selection**: delta **0.65–0.75** (target 0.70) from the Upstox option
  chain greeks, nearest weekly/0DTE expiry (but `min_days_to_expiry` rolls to the
  next expiry when the nearest is that many days away or less, avoiding
  gamma/pin risk right at expiry), marketable **limit orders** with a narrow
  tolerance. Thin/untraded strikes are skipped via **liquidity guards** —
  `min_volume`, `min_open_interest` and `max_spread_pct` (bid-ask spread as % of
  mid) under `options:` in `config.yaml`. `min_volume` is the important one: an
  untraded (0-volume) strike carries a stale LTP/greeks that mislead delta
  selection and fabricate PnL, so it must never be chosen.

## Index universe

| Config name | Index | Options |
|---|---|---|
| NIFTY | Nifty 50 | weekly + monthly contracts |
| BANKNIFTY | Nifty Bank | monthly contracts |
| FINNIFTY | Nifty Financial Services | monthly contracts |
| MIDCPNIFTY | Nifty Midcap Select | monthly contracts |
| NIFTYNXT50 | Nifty Next 50 | monthly contracts |

> All five indices have exchange-listed options on NSE. Keys are validated and
> auto-corrected against Upstox's instrument master on startup, so casing
> differences won't break them. Add/remove indices freely in `config.yaml`; any
> index without listed options is treated as signal-only (shown on the dashboard,
> never traded).

## Setup

```bash
pip install -r requirements.txt
```

Create an app at <https://account.upstox.com/developer/apps>. You can enter its
API key / secret / redirect URI **on the dashboard's Connect screen** (no `.env`
needed), or put them in a `.env` file (`cp .env.example .env`) to skip that step.

### Connecting Upstox (tokens expire daily ~3:30 AM IST)

Upstox access tokens last one trading day, so you connect once each morning.
**Two ways:**

- **From the web dashboard (recommended):** run `python -m onlyichu web`, open the
  page, and click **Connect Upstox**. Enter your app credentials once (saved to
  `~/.onlyichu/`), then **Open Upstox login** → approve → you're redirected back
  and connected automatically. If your app's redirect URI doesn't point at the
  dashboard, paste the `code` from the redirect URL, or paste an access token
  directly — both options are on the same screen. Register your Upstox app's
  redirect URI as `http://127.0.0.1:8080/callback` for the automatic flow.
- **From the terminal:** `python -m onlyichu login`, then paste the `code`.

The token is stored in `~/.onlyichu/credentials.json` and reused by `run`, `web`
and `backtest`. The web server starts even without a token — it just shows the
Connect screen until you're authenticated.

### Instrument keys are auto-validated

Upstox index keys are exact strings with inconsistent casing (e.g.
`NSE_INDEX|Nifty 50` but `NSE_INDEX|NIFTY MID SELECT`). On startup, `run`, `web`
and `backtest` download Upstox's public instrument master, validate every
configured key, and auto-correct wrong ones by name match (a warning shows the
corrected key to put in `config.yaml`). An unresolvable index is disabled with
suggestions instead of erroring forever. To search keys manually:

```bash
python -m onlyichu instruments --index-only --search "next 50"
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
5. enforces the opening-range breakout filter, entry cutoff, daily trade caps,
   daily loss limit, and force square-off (defaults: no entries until price
   breaks the first 15 minutes' range, none after 15:00, square-off 15:15 IST).

**Opening-range breakout filter** (`session.opening_range_minutes`, default 15):
the day's first entry per index is held until a candle closes beyond the high/low
of the first N minutes. A flat/choppy open is skipped until the market picks a
direction; a trending open breaks the range immediately and trades normally.
Set to `0` to trade from the open.

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

**Entry-skip notes:** when the running engine skips an entry it wanted to take,
a small amber note appears on the affected pipeline panel — e.g. *"no liquid
strike — entry skipped"*, *"waiting for opening-range breakout"*, *"max
trades/day reached"*, *"daily loss limit reached"*, or *"past entry cutoff"*.
The note clears once that pipeline trades or after ~90 seconds.

**Per-index trade toggle:** each tradeable index card has a TRADE switch.
Switched off, the index stays fully visible on the dashboard (Ichimoku levels
and LONG/SHORT signals keep updating from real data) but the engine will not
open positions for it — ideal for low capital where you trade a single index.
Exits for already-open positions are always managed regardless of the toggle.
Toggles apply instantly (even to a running engine), persist in
`state/settings.json`, and can also be preset with `trade_enabled: false` per
instrument in `config.yaml`.

**In-flight order lock (safety):** the live broker tracks the order it placed
for each position and, before placing another, checks that order's status. While
an order is still working it refuses to place a duplicate; if the prior order
already filled it uses that fill instead of re-ordering. This prevents the
"one position, multiple sell orders" bug where an exit re-fired each candle while
a prior sell was still unconfirmed (which could flip a long into a naked short).

**Live position reconciliation (self-healing):** in live mode the engine compares
its own book against Upstox's real positions (`api.positions()`) every cycle and
resolves any drift automatically:
- **Orphan** (Upstox holds a position the app doesn't — e.g. an entry that filled
  but came back unconfirmed): if a flat pipeline's current Ichimoku signal still
  supports holding it, the position is **adopted** so the strategy manages its
  exit; otherwise it's **squared off** at market.
- **Phantom** (the app holds a position Upstox has already closed — e.g. after a
  manual square-off): it's **dropped** from the book.

Only if a mismatch remains after healing does it **pause new entries** and show a
red banner naming the instrument. At startup it seeds the live book from Upstox,
and square-off reconciles against Upstox first. Paper mode is always its own
source of truth.

**Feed-lag guard (no drop-churn):** Upstox's position feed lags a just-placed
fill by a few seconds, so a freshly opened position can momentarily look
"missing" on Upstox. The reconciler therefore **never drops a strike it ordered
within the last ~20s**, and a phantom (app holds more than Upstox) must **persist
for two consecutive cycles** before it's dropped. This prevents the
enter → *"closed externally"* → re-enter loop that would otherwise flatten a
position seconds after opening it (real buy/sell round-trips piling up slippage)
while the dashboard read `0 open positions`. Orphans are still adopted/squared
off immediately.

**Net P&L (charges deducted):** with `charges.apply_charges: true` (default),
every trade's estimated **brokerage, STT, exchange transaction charge, SEBI fee,
stamp duty and GST** are deducted so realized P&L, total P&L and the profit
target all reflect the **net** figure your broker reports (Gross P&L − charges =
Net P&L), not gross. Open positions' unrealized MTM is shown net of the charges
it would cost to close them, and the strip shows **Charges today**. Rates follow
the current NSE/Upstox F&O options schedule and are configurable under
`charges:` in `config.yaml` — tune them to match your contract notes exactly, or
set `apply_charges: false` to report gross. The trade log gains a `charges`
column per fill.

**Live P&L and daily profit target:** while the engine runs, the account strip
shows **Unrealized MTM** and **Total P&L** (realized + unrealized) alongside
realized, and each open-position chip shows its **strike, entry, current LTP and
unrealized P&L**, refreshed every cycle. Set a **TARGET ₹** in the control bar
(or `risk.daily_profit_target` in `config.yaml`): the moment total profit reaches
it, the engine **squares off every position at market and halts all trading for
the rest of the day** — the status pill shows `⛔ HALTED`. `0` disables it.

**Lot size & capital from the page:** the control bar has LOTS and CAPITAL ₹
inputs with an APPLY button. Lots-per-trade takes effect on the next entry
(even while the engine is running); capital resets the paper account's cash
(engine must be stopped in paper mode first). Both persist across restarts in
`state/settings.json`, overriding `config.yaml`.

**Trade log:** a table at the bottom of the page shows every executed trade
(newest first) with PAPER | LIVE tabs, auto-refreshing with the dashboard, and
a DOWNLOAD CSV button that serves the full log as a file
(`GET /trades.csv?mode=paper|live`). Each row shows the fill price, index level,
net PnL and the charges deducted. **CLEAR >5d** deletes trade-log rows older than
five days from both logs (`POST /api/trades/purge`, body `{mode, days}`). If the
table looks empty, check the other tab — paper and live trades are kept
separately, so a LIVE run leaves the PAPER tab empty and vice-versa.

**Validation export:** **EXPORT VALIDATION** (`GET /validate.csv`) is a one-time
download of every computed value — Tenkan, Kijun, Span A/B, cloud edges, cloud
thickness, current and previous MACD histogram, Chikou checks, and the resulting
entry/exit booleans and signal — for the **1m and 5m** pipelines of every index,
per candle. Use it to reconcile the engine's numbers against your charting tool.

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
Trade history is appended to `state/trades_paper.csv` / `state/trades_live.csv`
(each row records the option fill `price` and the underlying `index_price` at
that entry/exit, so you can see how the index moved versus the option);
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
