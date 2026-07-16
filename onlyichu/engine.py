"""Live/paper trading engine.

Data flow per enabled index:

  Upstox intraday 1m candle API --poll--> new completed 1m candles
      -> 1m pipeline (Ichimoku on 1m closes)
      -> 5m aggregator -> 5m pipeline (Ichimoku on 5m closes)

Signals -> ITM option selection (delta 0.65-0.75, nearest expiry)
        -> PaperBroker (simulated) or LiveBroker (real Upstox orders).

Entries execute immediately after the triggering candle closes — i.e. at the
open of the subsequent candle, per the spec. Exits execute immediately.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .broker import BaseBroker, LiveBroker, PaperBroker
from .candles import Candle, CandleSeries, TimeframeAggregator
from .config import Config, IndexConfig
from .ichimoku import IchimokuParams
from .options import OptionSelector
from .strategy import ENTER_LONG, EXIT, Pipeline, Signal
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)


class IndexRunner:
    """All per-index state: 1m feed tracking, aggregators and pipelines."""

    def __init__(self, index: IndexConfig, cfg: Config, params: IchimokuParams):
        self.index = index
        self.cfg = cfg
        self.one_min = CandleSeries()  # dedupe/tracking of raw 1m feed
        self.aggregators = {
            tf: TimeframeAggregator(tf) for tf in cfg.timeframes_minutes if tf != 1
        }
        self.pipelines: dict[int, Pipeline] = {
            tf: Pipeline(index.name, tf, params) for tf in cfg.timeframes_minutes
        }


class Engine:
    def __init__(self, cfg: Config, api: UpstoxAPI):
        self.cfg = cfg
        self.api = api
        self.tz = ZoneInfo(cfg.timezone)
        self.params = IchimokuParams(cfg.tenkan, cfg.kijun, cfg.senkou_b, cfg.displacement)
        self.selector = OptionSelector(api, cfg)
        self.broker: BaseBroker = (
            LiveBroker(cfg, api) if cfg.mode == "live" else PaperBroker(cfg, api)
        )
        self.runners = [IndexRunner(ix, cfg, self.params) for ix in cfg.enabled_instruments]
        self.trades_today: dict[str, int] = {}
        self._squared_off = False

    # ------------------------------------------------------------ warmup

    def warmup(self) -> None:
        now = datetime.now(self.tz)
        to_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=self.cfg.warmup_days)).strftime("%Y-%m-%d")
        for runner in self.runners:
            key = runner.index.key
            rows: list[list] = []
            try:
                rows.extend(self.api.historical_candles(key, to_date, from_date))
            except UpstoxError as exc:
                log.warning("historical warmup failed for %s: %s", runner.index.name, exc)
            try:
                rows.extend(self.api.intraday_candles(key))
            except UpstoxError as exc:
                log.warning("intraday warmup failed for %s: %s", runner.index.name, exc)

            # drop the in-progress candle: only intervals that have fully ended
            cutoff = self._now().replace(second=0, microsecond=0)
            candles = sorted(
                (
                    c
                    for r in rows
                    if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff
                ),
                key=lambda c: c.ts,
            )
            new_1m: list[Candle] = []
            for c in candles:
                if runner.one_min.append(c):
                    new_1m.append(c)
            if 1 in runner.pipelines:
                runner.pipelines[1].warmup(new_1m)
            for tf, agg in runner.aggregators.items():
                agg_candles = [done for c in new_1m if (done := agg.feed(c))]
                runner.pipelines[tf].warmup(agg_candles)
            log.info(
                "%s warmup: %d x 1m candles (%s)",
                runner.index.name,
                len(runner.one_min),
                ", ".join(f"{tf}m series={len(p.series)}" for tf, p in runner.pipelines.items()),
            )

    # ------------------------------------------------------------ session

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _between(self, t: time, start: time, end: time) -> bool:
        return start <= t <= end

    def run(self) -> None:
        log.info("engine starting in %s mode with %d indices", self.cfg.mode.upper(), len(self.runners))
        self.warmup()
        while True:
            now = self._now()
            t = now.time()
            if t >= self.cfg.market_close:
                log.info("market closed — stopping")
                break
            if t < self.cfg.market_open:
                wait = (
                    datetime.combine(now.date(), self.cfg.market_open, self.tz) - now
                ).total_seconds()
                log.info("waiting %.0fs for market open", wait)
                _time.sleep(min(wait + 1, 300))
                continue
            if t >= self.cfg.square_off and not self._squared_off:
                self.square_off_all("square-off time")
                self._squared_off = True
            self.poll_once()
            _time.sleep(self.cfg.poll_interval_seconds)
        # end of session: make sure nothing is left open
        if not self._squared_off and self.broker.open_positions():
            self.square_off_all("session end")

    def poll_once(self) -> None:
        """Fetch today's 1m candles for every index and process new completed ones."""
        for runner in self.runners:
            try:
                rows = self.api.intraday_candles(runner.index.key)
            except UpstoxError as exc:
                log.warning("candle poll failed for %s: %s", runner.index.name, exc)
                continue
            cutoff = self._now().replace(second=0, microsecond=0)
            fresh = sorted(
                (
                    c
                    for r in rows
                    # candle must be complete: its interval must have ended
                    if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff
                ),
                key=lambda c: c.ts,
            )
            for candle in fresh:
                if not runner.one_min.append(candle):
                    continue
                self._process_candle(runner, 1, candle)
                for tf, agg in runner.aggregators.items():
                    done = agg.feed(candle)
                    if done is not None:
                        self._process_candle(runner, tf, done)

    def _process_candle(self, runner: IndexRunner, tf: int, candle: Candle) -> None:
        pipeline = runner.pipelines.get(tf)
        if pipeline is None:
            return
        side = self.broker.position_side(pipeline.pipeline_id)
        for signal in pipeline.on_candle_close(candle, side):
            self._execute(runner, signal)

    # ---------------------------------------------------------- execution

    def _execute(self, runner: IndexRunner, signal: Signal) -> None:
        pid = signal.pipeline_id
        if signal.action == EXIT:
            hint = None
            pos = self.broker.position(pid)
            if pos is not None and pos.kind == "SYNTHETIC":
                hint = signal.candle.close
            self.broker.exit(pid, price_hint=hint, note="signal")
            return

        # entries
        now_t = self._now().time()
        if self._squared_off or now_t >= self.cfg.entry_cutoff:
            log.info("%s entry skipped: past entry cutoff", pid)
            return
        if self.cfg.max_daily_loss > 0 and self.broker.realized_pnl_today() <= -self.cfg.max_daily_loss:
            log.warning("%s entry skipped: daily loss limit hit (pnl=%.2f)", pid, self.broker.realized_pnl_today())
            return
        if self.trades_today.get(pid, 0) >= self.cfg.max_trades_per_day_per_pipeline:
            log.info("%s entry skipped: max trades/day reached", pid)
            return

        direction = "LONG" if signal.action == ENTER_LONG else "SHORT"
        spot = signal.candle.close

        if runner.index.options_available:
            sel = self.selector.select_itm(runner.index.key, direction, spot)
            if sel is None:
                log.warning("%s: no suitable ITM %s found; entry skipped", pid, "CALL" if direction == "LONG" else "PUT")
                return
            qty = sel.lot_size * self.cfg.lots_per_trade
            log.info(
                "%s %s -> BUY %s x%d (strike %.0f, delta %s, exp %s)",
                pid, direction, sel.trading_symbol, qty, sel.strike,
                f"{sel.delta:.2f}" if sel.delta is not None else "n/a", sel.expiry,
            )
            pos = self.broker.enter(
                pid, sel.instrument_key, sel.trading_symbol, qty, direction, sel.ltp, kind="OPTION"
            )
        else:
            if self.cfg.mode == "live":
                log.warning("%s: %s has no listed options; live entry skipped", pid, runner.index.name)
                return
            qty = self.cfg.lots_per_trade
            log.info("%s %s -> SYNTHETIC index position x%d @ ~%.2f (no options listed)", pid, direction, qty, spot)
            pos = self.broker.enter(
                pid, runner.index.key, runner.index.name, qty, direction, spot, kind="SYNTHETIC"
            )
        if pos is not None:
            self.trades_today[pid] = self.trades_today.get(pid, 0) + 1

    def square_off_all(self, reason: str) -> None:
        for pos in self.broker.open_positions():
            log.info("square-off (%s): %s %s", reason, pos.pipeline_id, pos.symbol)
            self.broker.exit(pos.pipeline_id, price_hint=None, note=f"square-off:{reason}")
