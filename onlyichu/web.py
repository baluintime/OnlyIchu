"""Web dashboard: fetches historical + intraday candles from Upstox, computes
the Ichimoku Cloud server-side for every index on both timeframes, and serves
an auto-refreshing animated page (no charts — signal cards and level ladders).

Run with:  python -m onlyichu web
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template

from .candles import Candle, TimeframeAggregator
from .config import Config, IndexConfig
from .ichimoku import IchimokuParams, compute_state, long_entry, short_entry
from .tzutil import get_zone
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)

LEVEL_NAMES = [
    ("tenkan", "Tenkan-sen"),
    ("kijun", "Kijun-sen"),
    ("span_a", "Senkou Span A"),
    ("span_b", "Senkou Span B"),
]


def analyze_series(candles: list[Candle], params: IchimokuParams) -> dict | None:
    """Ichimoku snapshot of the latest completed candle of a series."""
    if not candles:
        return None
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    last = candles[-1]
    state = compute_state(highs, lows, params)
    if state is None:
        return {
            "ready": False,
            "candles": len(candles),
            "needed": params.min_candles,
            "close": last.close,
            "candle_time": last.ts.strftime("%H:%M"),
            "candle_date": last.ts.strftime("%Y-%m-%d"),
        }
    close = last.close
    if long_entry(close, state):
        signal = "LONG"
    elif short_entry(close, state):
        signal = "SHORT"
    else:
        signal = "NEUTRAL"
    if close > state.cloud_top:
        zone = "ABOVE CLOUD"
    elif close < state.cloud_bottom:
        zone = "BELOW CLOUD"
    else:
        zone = "IN CLOUD"
    levels = []
    for attr, label in LEVEL_NAMES:
        value = getattr(state, attr)
        levels.append(
            {
                "name": label,
                "value": round(value, 2),
                "above": close > value,
                "below": close < value,
                "dist_pct": round((close - value) / value * 100, 3) if value else 0.0,
            }
        )
    return {
        "ready": True,
        "candles": len(candles),
        "close": close,
        "candle_time": last.ts.strftime("%H:%M"),
        "candle_date": last.ts.strftime("%Y-%m-%d"),
        "signal": signal,
        "zone": zone,
        "levels": levels,
        "cloud": {
            "top": round(state.cloud_top, 2),
            "bottom": round(state.cloud_bottom, 2),
            "bullish": state.span_a >= state.span_b,
            "thickness_pct": round(
                (state.cloud_top - state.cloud_bottom) / close * 100, 3
            ) if close else 0.0,
        },
    }


class DashboardService:
    """Builds the dashboard payload, caching Upstox data briefly so several
    open browser tabs don't multiply API calls."""

    def __init__(self, cfg: Config, api: UpstoxAPI):
        self.cfg = cfg
        self.api = api
        self.tz = get_zone(cfg.timezone)
        self.params = IchimokuParams(cfg.tenkan, cfg.kijun, cfg.senkou_b, cfg.displacement)
        self.ttl = max(3.0, cfg.web_refresh_seconds / 2.0)
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0
        # historical candles are immutable — fetch once per (index, day)
        self._hist: dict[str, tuple[str, list[Candle]]] = {}

    # ------------------------------------------------------------- data

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _historical(self, index: IndexConfig, today: str) -> list[Candle]:
        cached = self._hist.get(index.key)
        if cached and cached[0] == today:
            return cached[1]
        now = self._now()
        to_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=self.cfg.warmup_days)).strftime("%Y-%m-%d")
        rows = self.api.historical_candles(index.key, to_date, from_date)
        candles = sorted((Candle.from_upstox(r) for r in rows), key=lambda c: c.ts)
        self._hist[index.key] = (today, candles)
        return candles

    def _index_candles(self, index: IndexConfig) -> list[Candle]:
        """Completed 1m candles: cached historical warmup + fresh intraday."""
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        candles = list(self._historical(index, today))
        rows = self.api.intraday_candles(index.key)
        cutoff = now.replace(second=0, microsecond=0)
        intraday = sorted(
            (
                c
                for r in rows
                if (c := Candle.from_upstox(r)).ts + timedelta(minutes=1) <= cutoff
            ),
            key=lambda c: c.ts,
        )
        last_ts = candles[-1].ts if candles else None
        candles.extend(c for c in intraday if last_ts is None or c.ts > last_ts)
        return candles

    # ---------------------------------------------------------- payload

    def payload(self) -> dict:
        with self._lock:
            if self._cached is not None and _time.monotonic() - self._cached_at < self.ttl:
                return self._cached
            data = self._build()
            self._cached, self._cached_at = data, _time.monotonic()
            return data

    def _market_status(self, now: datetime) -> str:
        if now.weekday() >= 5:
            return "CLOSED"
        t = now.time()
        if self.cfg.market_open <= t <= self.cfg.market_close:
            return "OPEN"
        return "PRE-OPEN" if t < self.cfg.market_open else "CLOSED"

    def _build(self) -> dict:
        now = self._now()
        indices_payload: list[dict] = []
        error: str | None = None

        keys = [ix.key for ix in self.cfg.enabled_instruments]
        ltps: dict[str, float] = {}
        try:
            ltps = self.api.ltp(keys) if keys else {}
        except UpstoxError as exc:
            log.warning("ltp fetch failed: %s", exc)

        for index in self.cfg.enabled_instruments:
            entry: dict = {
                "name": index.name,
                "key": index.key,
                "options_available": index.options_available,
                "ltp": ltps.get(index.key),
                "prev_close": None,
                "change_pct": None,
                "pipelines": [],
                "error": None,
            }
            try:
                one_min = self._index_candles(index)
            except UpstoxError as exc:
                entry["error"] = str(exc)[:200]
                log.warning("candle fetch failed for %s: %s", index.name, exc)
                indices_payload.append(entry)
                error = error or "Some indices failed to load — check the access token."
                continue

            today = now.date()
            prev = [c for c in one_min if c.ts.date() < today]
            if prev:
                entry["prev_close"] = prev[-1].close
                ref = entry["ltp"] or (one_min[-1].close if one_min else None)
                if ref:
                    entry["change_pct"] = round((ref - prev[-1].close) / prev[-1].close * 100, 2)
            if entry["ltp"] is None and one_min:
                entry["ltp"] = one_min[-1].close

            for tf in self.cfg.timeframes_minutes:
                if tf == 1:
                    series = one_min
                else:
                    agg = TimeframeAggregator(tf)
                    series = [done for c in one_min if (done := agg.feed(c))]
                analysis = analyze_series(series, self.params) or {"ready": False, "candles": 0}
                analysis["timeframe"] = f"{tf}m"
                entry["pipelines"].append(analysis)
            indices_payload.append(entry)

        return {
            "generated_at": now.strftime("%H:%M:%S"),
            "generated_date": now.strftime("%a, %d %b %Y"),
            "refresh_seconds": self.cfg.web_refresh_seconds,
            "market": {
                "status": self._market_status(now),
                "session": f"{self.cfg.market_open.strftime('%H:%M')}–{self.cfg.market_close.strftime('%H:%M')} IST",
            },
            "strategy": {
                "params": f"{self.params.tenkan}/{self.params.kijun}/{self.params.senkou_b} (disp {self.params.displacement})",
                "timeframes": [f"{tf}m" for tf in self.cfg.timeframes_minutes],
            },
            "error": error,
            "indices": indices_payload,
            "paper": self._paper_state(),
        }

    def _paper_state(self) -> dict | None:
        try:
            with open(self.cfg.paper_state_file, encoding="utf-8") as fh:
                state = json.load(fh)
        except (FileNotFoundError, ValueError):
            return None
        positions = [
            {
                "pipeline": pid,
                "symbol": pos.get("symbol"),
                "direction": pos.get("direction"),
                "qty": pos.get("qty"),
                "entry_price": pos.get("entry_price"),
            }
            for pid, pos in (state.get("positions") or {}).items()
        ]
        return {
            "cash": state.get("cash"),
            "realized_pnl_today": state.get("realized_pnl_today"),
            "pnl_date": state.get("pnl_date"),
            "positions": positions,
        }


def create_app(cfg: Config, api: UpstoxAPI) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    service = DashboardService(cfg, api)

    @app.get("/")
    def dashboard():  # type: ignore[unused-variable]
        return render_template("dashboard.html", refresh_seconds=cfg.web_refresh_seconds)

    @app.get("/api/dashboard")
    def api_dashboard():  # type: ignore[unused-variable]
        return jsonify(service.payload())

    return app


def run_web(cfg: Config, api: UpstoxAPI) -> None:
    app = create_app(cfg, api)
    log.info("dashboard on http://%s:%d (refresh every %ds)", cfg.web_host, cfg.web_port, cfg.web_refresh_seconds)
    app.run(host=cfg.web_host, port=cfg.web_port, debug=False, threaded=True)
