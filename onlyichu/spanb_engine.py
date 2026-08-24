"""Trading engine for the Span B slope strategy (separate from the Ichimoku
engine in engine.py). Polls completed candles, tracks Span B slope per
(index, timeframe), and sells OTM options on the slope — short call on a falling
Span B, short put on a rising one — buying them back when the slope reverts.

Deliberately self-contained so the original strategy is untouched and can be
reverted to at any time; only PaperBroker/LiveBroker, OptionSelector, candle and
session plumbing are shared.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, time, timedelta

from .broker import BaseBroker, LiveBroker, PaperBroker
from .candles import Candle, CandleSeries, TimeframeAggregator
from .config import Config, IndexConfig
from .options import OptionSelector
from .spanb import (
    EXIT, SELL_CALL, SELL_PUT, SHORT_CALL, SHORT_PUT,
    SpanbConfig, SpanbPipeline, SpanbSignal, build_spanb_config,
)
from .tzutil import get_zone
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)


class SpanbRunner:
    def __init__(self, index: IndexConfig, cfg: Config, sc: SpanbConfig):
        self.index = index
        self.cfg = cfg
        self.one_min = CandleSeries()
        self.aggregators = {
            tf: TimeframeAggregator(tf) for tf in cfg.timeframes_minutes if tf != 1
        }
        self.pipelines: dict[int, SpanbPipeline] = {
            tf: SpanbPipeline(index.name, tf, sc) for tf in cfg.timeframes_minutes
        }


class SpanbEngine:
    def __init__(self, cfg: Config, api: UpstoxAPI):
        self.cfg = cfg
        self.api = api
        self.tz = get_zone(cfg.timezone)
        self.sc = build_spanb_config(cfg)
        self.params = self.sc.ich
        self.selector = OptionSelector(api, cfg)
        self.broker: BaseBroker = (
            LiveBroker(cfg, api) if cfg.mode == "live" else PaperBroker(cfg, api)
        )
        self.runners = [SpanbRunner(ix, cfg, self.sc) for ix in cfg.enabled_instruments]
        self.trades_today: dict[str, int] = {}
        self._squared_off = False
        self._stop = threading.Event()
        self.last_skips: dict[str, dict] = {}

    # ------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _record_skip(self, pid: str, reason: str, direction: str | None = None) -> None:
        self.last_skips[pid] = {"reason": reason, "direction": direction, "at": _time.time()}

    def recent_skips(self, ttl: float = 90.0) -> list[dict]:
        now = _time.time()
        return [
            {"pipeline": pid, "reason": s["reason"], "direction": s["direction"], "age_s": round(now - s["at"])}
            for pid, s in self.last_skips.items() if now - s["at"] <= ttl
        ]

    # ------------------------------------------------------------ warmup

    def warmup(self) -> None:
        now = self._now()
        to_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=self.cfg.warmup_days)).strftime("%Y-%m-%d")
        for runner in self.runners:
            rows: list[list] = []
            try:
                rows.extend(self.api.historical_candles(runner.index.key, to_date, from_date))
            except UpstoxError as exc:
                log.warning("spanb historical warmup failed for %s: %s", runner.index.name, exc)
            try:
                rows.extend(self.api.intraday_candles(runner.index.key))
            except UpstoxError as exc:
                log.warning("spanb intraday warmup failed for %s: %s", runner.index.name, exc)
            cutoff = self._now().replace(second=0, microsecond=0)
            candles = sorted(
                (c for r in rows if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff),
                key=lambda c: c.ts,
            )
            new_1m: list[Candle] = []
            for c in candles:
                if runner.one_min.append(c):
                    new_1m.append(c)
            if 1 in runner.pipelines:
                runner.pipelines[1].warmup(new_1m)
            for tf, agg in runner.aggregators.items():
                runner.pipelines[tf].warmup([done for c in new_1m if (done := agg.feed(c))])
            log.info("spanb %s warmup: %d x 1m candles", runner.index.name, len(runner.one_min))

    # ------------------------------------------------------------ session

    def run(self, stop_event: threading.Event | None = None) -> None:
        if stop_event is not None:
            self._stop = stop_event
        log.info("spanb engine starting in %s mode with %d indices", self.cfg.mode.upper(), len(self.runners))
        self.warmup()
        while not self._stop.is_set():
            now = self._now()
            t = now.time()
            if t >= self.cfg.market_close:
                log.info("market closed — stopping spanb")
                break
            if t < self.cfg.market_open:
                wait = (datetime.combine(now.date(), self.cfg.market_open, self.tz) - now).total_seconds()
                self._stop.wait(min(wait + 1, 300))
                continue
            if t >= self.cfg.square_off and not self._squared_off:
                self.square_off_all("square-off time")
                self._squared_off = True
            self.poll_once()
            self._stop.wait(self.cfg.poll_interval_seconds)
        if self._stop.is_set():
            log.info("spanb engine stopped on request (open positions left untouched)")
            return
        if not self._squared_off and self.broker.open_positions():
            self.square_off_all("session end")

    def poll_once(self) -> None:
        for runner in self.runners:
            try:
                rows = self.api.intraday_candles(runner.index.key)
            except UpstoxError as exc:
                log.warning("spanb candle poll failed for %s: %s", runner.index.name, exc)
                continue
            cutoff = self._now().replace(second=0, microsecond=0)
            fresh = sorted(
                (c for r in rows if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff),
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

    def _process_candle(self, runner: SpanbRunner, tf: int, candle: Candle) -> None:
        pipeline = runner.pipelines.get(tf)
        if pipeline is None:
            return
        side = self.broker.position_side(pipeline.pipeline_id)
        for signal in pipeline.on_candle_close(candle, side):
            self._execute(runner, signal)

    # ---------------------------------------------------------- execution

    def _execute(self, runner: SpanbRunner, signal: SpanbSignal) -> None:
        pid = signal.pipeline_id
        tf = int(pid.rsplit(":", 1)[1].rstrip("m"))
        candle_close = (signal.candle.ts + timedelta(minutes=tf)).isoformat(timespec="seconds")
        if signal.action == EXIT:
            self.broker.exit(pid, price_hint=None, note="spanb revert",
                             underlying_spot=signal.candle.close, candle_time=candle_close)
            return

        opt_type = "CE" if signal.action == SELL_CALL else "PE"
        side = SHORT_CALL if signal.action == SELL_CALL else SHORT_PUT
        now_t = self._now().time()
        if self._squared_off or now_t >= self.cfg.entry_cutoff:
            self._record_skip(pid, "past entry cutoff", side)
            return
        if not runner.index.options_available:
            return
        if not runner.index.trade_enabled:
            self._record_skip(pid, "trading toggled off", side)
            return
        spot = signal.candle.close
        sel = self.selector.select_otm(runner.index.key, opt_type, spot, self.sc.otm_strikes)
        if sel is None:
            self._record_skip(pid, "no liquid OTM strike — entry skipped", side)
            return
        qty = sel.lot_size * self.cfg.lots_per_trade
        log.info(
            "%s spanb -> SELL %s x%d (%d strikes OTM, strike %.0f, exp %s)",
            pid, sel.trading_symbol, qty, self.sc.otm_strikes, sel.strike, sel.expiry,
        )
        pos = self.broker.enter(
            pid, sel.instrument_key, sel.trading_symbol, qty, side, sel.ltp,
            underlying_spot=spot, strike=sel.strike, lot_size=sel.lot_size,
            candle_time=candle_close, short=True,
        )
        if pos is not None:
            self.trades_today[pid] = self.trades_today.get(pid, 0) + 1
            self.last_skips.pop(pid, None)

    def _last_index_price(self, pipeline_id: str) -> float | None:
        name = pipeline_id.split(":", 1)[0]
        for runner in self.runners:
            if runner.index.name == name and runner.one_min.candles:
                return runner.one_min.candles[-1].close
        return None

    def square_off_all(self, reason: str) -> None:
        for pos in self.broker.open_positions():
            log.info("spanb square-off (%s): %s %s", reason, pos.pipeline_id, pos.symbol)
            self.broker.exit(pos.pipeline_id, price_hint=None, note=f"square-off:{reason}",
                             underlying_spot=self._last_index_price(pos.pipeline_id))
