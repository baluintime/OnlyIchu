"""Configuration loading for OnlyIchu."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from typing import Any

import yaml


@dataclass
class IndexConfig:
    name: str
    key: str
    enabled: bool = True            # show on dashboard / compute signals
    options_available: bool = True  # index has exchange-listed option contracts
    trade_enabled: bool = True      # place trades on signals (toggle in the UI)


@dataclass
class Config:
    mode: str = "paper"

    poll_interval_seconds: float = 5.0
    warmup_days: int = 7

    timezone: str = "Asia/Kolkata"
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    entry_cutoff: time = time(15, 0)
    square_off: time = time(15, 15)
    # No entries until price breaks the first N minutes' high/low (opening-range
    # breakout). 0 disables — trade from the open.
    opening_range_minutes: int = 0

    timeframes_minutes: list[int] = field(default_factory=lambda: [1, 5])
    tenkan: int = 9
    kijun: int = 26
    senkou_b: int = 52
    displacement: int = 26

    target_delta: float = 0.70
    delta_min: float = 0.65
    delta_max: float = 0.75
    expiry: str = "nearest"
    # Skip an expiry that is this many days away or less and roll to the next
    # (avoids trading right at expiry). 0 = always use the nearest.
    min_days_to_expiry: int = 0
    order_type: str = "LIMIT"
    limit_tolerance_pct: float = 0.25
    product: str = "I"
    # liquidity guards (0 disables the check)
    min_volume: int = 0
    min_open_interest: int = 0
    max_spread_pct: float = 0.0

    lots_per_trade: int = 1
    max_trades_per_day_per_pipeline: int = 10
    max_daily_loss: float = 10000.0
    # When total profit (realized + unrealized) reaches this INR value, square off
    # everything and stop trading for the day. 0 disables.
    daily_profit_target: float = 0.0

    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_refresh_seconds: int = 10

    paper_starting_cash: float = 500000.0
    paper_slippage_pct: float = 0.05
    paper_state_file: str = "state/paper_state.json"
    paper_trade_log: str = "state/trades_paper.csv"
    live_trade_log: str = "state/trades_live.csv"

    instruments: list[IndexConfig] = field(default_factory=list)

    @property
    def enabled_instruments(self) -> list[IndexConfig]:
        return [i for i in self.instruments if i.enabled]


def _parse_time(value: Any, default: time) -> time:
    if value is None:
        return default
    if isinstance(value, time):
        return value
    parts = str(value).split(":")
    return time(int(parts[0]), int(parts[1]))


def load_config(path: str = "config.yaml") -> Config:
    raw: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    cfg = Config()
    cfg.mode = str(raw.get("mode", cfg.mode)).lower()

    data = raw.get("data", {}) or {}
    cfg.poll_interval_seconds = float(data.get("poll_interval_seconds", cfg.poll_interval_seconds))
    cfg.warmup_days = int(data.get("warmup_days", cfg.warmup_days))

    sess = raw.get("session", {}) or {}
    cfg.timezone = sess.get("timezone", cfg.timezone)
    cfg.market_open = _parse_time(sess.get("market_open"), cfg.market_open)
    cfg.market_close = _parse_time(sess.get("market_close"), cfg.market_close)
    cfg.entry_cutoff = _parse_time(sess.get("entry_cutoff"), cfg.entry_cutoff)
    cfg.square_off = _parse_time(sess.get("square_off"), cfg.square_off)
    cfg.opening_range_minutes = int(sess.get("opening_range_minutes", cfg.opening_range_minutes))

    strat = raw.get("strategy", {}) or {}
    cfg.timeframes_minutes = list(strat.get("timeframes_minutes", cfg.timeframes_minutes))
    cfg.tenkan = int(strat.get("tenkan", cfg.tenkan))
    cfg.kijun = int(strat.get("kijun", cfg.kijun))
    cfg.senkou_b = int(strat.get("senkou_b", cfg.senkou_b))
    cfg.displacement = int(strat.get("displacement", cfg.displacement))

    opts = raw.get("options", {}) or {}
    cfg.target_delta = float(opts.get("target_delta", cfg.target_delta))
    cfg.delta_min = float(opts.get("delta_min", cfg.delta_min))
    cfg.delta_max = float(opts.get("delta_max", cfg.delta_max))
    cfg.expiry = opts.get("expiry", cfg.expiry)
    cfg.min_days_to_expiry = int(opts.get("min_days_to_expiry", cfg.min_days_to_expiry))
    cfg.order_type = str(opts.get("order_type", cfg.order_type)).upper()
    cfg.limit_tolerance_pct = float(opts.get("limit_tolerance_pct", cfg.limit_tolerance_pct))
    cfg.product = opts.get("product", cfg.product)
    cfg.min_volume = int(opts.get("min_volume", cfg.min_volume))
    cfg.min_open_interest = int(opts.get("min_open_interest", cfg.min_open_interest))
    cfg.max_spread_pct = float(opts.get("max_spread_pct", cfg.max_spread_pct))

    risk = raw.get("risk", {}) or {}
    cfg.lots_per_trade = int(risk.get("lots_per_trade", cfg.lots_per_trade))
    cfg.max_trades_per_day_per_pipeline = int(
        risk.get("max_trades_per_day_per_pipeline", cfg.max_trades_per_day_per_pipeline)
    )
    cfg.max_daily_loss = float(risk.get("max_daily_loss", cfg.max_daily_loss))
    cfg.daily_profit_target = float(risk.get("daily_profit_target", cfg.daily_profit_target))

    web = raw.get("web", {}) or {}
    cfg.web_host = web.get("host", cfg.web_host)
    cfg.web_port = int(web.get("port", cfg.web_port))
    cfg.web_refresh_seconds = int(web.get("refresh_seconds", cfg.web_refresh_seconds))

    paper = raw.get("paper", {}) or {}
    cfg.paper_starting_cash = float(paper.get("starting_cash", cfg.paper_starting_cash))
    cfg.paper_slippage_pct = float(paper.get("slippage_pct", cfg.paper_slippage_pct))
    cfg.paper_state_file = paper.get("state_file", cfg.paper_state_file)
    cfg.paper_trade_log = paper.get("trade_log", cfg.paper_trade_log)

    live = raw.get("live", {}) or {}
    cfg.live_trade_log = live.get("trade_log", cfg.live_trade_log)

    cfg.instruments = [
        IndexConfig(
            name=item["name"],
            key=item["key"],
            enabled=bool(item.get("enabled", True)),
            options_available=bool(item.get("options_available", True)),
            trade_enabled=bool(item.get("trade_enabled", True)),
        )
        for item in (raw.get("instruments") or [])
    ]
    return cfg
