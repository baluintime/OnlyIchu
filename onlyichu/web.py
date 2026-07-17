"""Web dashboard: fetches historical + intraday candles from Upstox, computes
the Ichimoku Cloud server-side for every index on both timeframes, and serves
an auto-refreshing animated page (no charts — signal cards and level ladders).

Run with:  python -m onlyichu web
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import os
import threading
import time as _time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file

from . import settings as settings_mod

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
        # failing indices are paused for a while instead of retried every poll
        self.fail_cooldown = 300.0
        self._failed: dict[str, tuple[float, str]] = {}

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
                "trade_enabled": index.trade_enabled,
                "ltp": ltps.get(index.key),
                "prev_close": None,
                "change_pct": None,
                "pipelines": [],
                "error": None,
            }
            paused = self._failed.get(index.key)
            if paused and _time.monotonic() - paused[0] < self.fail_cooldown:
                entry["error"] = paused[1] + " (retry paused)"
                indices_payload.append(entry)
                error = error or "Some indices failed to load — see index cards."
                continue
            try:
                one_min = self._index_candles(index)
                self._failed.pop(index.key, None)
            except UpstoxError as exc:
                msg = str(exc)[:200]
                self._failed[index.key] = (_time.monotonic(), msg)
                entry["error"] = msg
                log.warning(
                    "candle fetch failed for %s (pausing retries for %.0fs): %s",
                    index.name, self.fail_cooldown, exc,
                )
                indices_payload.append(entry)
                error = error or "Some indices failed to load — see index cards."
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


def read_trade_log(path: str, limit: int = 200) -> list[dict]:
    """Last `limit` trades from a broker CSV log, newest first."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error) as exc:
        log.warning("could not read trade log %s: %s", path, exc)
        return []
    return rows[-limit:][::-1]


class TradingController:
    """Starts/stops the trading Engine in a background thread, switchable
    between paper and live mode from the dashboard."""

    def __init__(self, cfg: Config, api: UpstoxAPI, token: str | None = None):
        self.base_cfg = cfg
        self.api = api
        self.token = token
        self._lock = threading.Lock()
        self._engine = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self.last_error: str | None = None
        self.started_at: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, mode: str) -> tuple[bool, str]:
        from .engine import Engine

        with self._lock:
            if self.running:
                return False, "engine is already running — stop it first"
            if mode not in ("paper", "live"):
                return False, f"unknown mode {mode!r}"
            cfg = copy.copy(self.base_cfg)
            cfg.mode = mode
            api = UpstoxAPI(self.token) if self.token else self.api
            try:
                engine = Engine(cfg, api)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                return False, self.last_error
            stop = threading.Event()

            def _run() -> None:
                try:
                    engine.run(stop)
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("engine crashed")

            self.last_error = None
            self._engine = engine
            self._stop = stop
            self._thread = threading.Thread(target=_run, daemon=True, name=f"engine-{mode}")
            self.started_at = datetime.now().strftime("%H:%M:%S")
            self._thread.start()
            log.info("engine started from dashboard in %s mode", mode.upper())
            return True, f"{mode.upper()} engine started"

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self.running:
                return False, "engine is not running"
            assert self._stop is not None and self._thread is not None
            self._stop.set()
            self._thread.join(timeout=15)
            log.info("engine stopped from dashboard")
            return True, "engine stopped (open positions left untouched)"

    def square_off(self) -> tuple[bool, str]:
        engine = self._engine
        if engine is None or not self.running:
            return False, "engine is not running"
        engine.square_off_all("manual (dashboard)")
        return True, "square-off requested for all open positions"

    def status(self) -> dict:
        running = self.running
        st: dict = {
            "running": running,
            "mode": self._engine.cfg.mode if (running and self._engine) else None,
            "started_at": self.started_at if running else None,
            "last_error": self.last_error,
            "positions": [],
            "realized_pnl_today": None,
            "cash": None,
        }
        if running and self._engine is not None:
            broker = self._engine.broker
            st["realized_pnl_today"] = broker.realized_pnl_today()
            if self._engine.cfg.mode == "paper":
                st["cash"] = broker.state.cash
            st["positions"] = [
                {
                    "pipeline": p.pipeline_id,
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                }
                for p in broker.open_positions()
            ]
        return st


def create_app(cfg: Config, api: UpstoxAPI, token: str | None = None) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    service = DashboardService(cfg, api)
    controller = TradingController(cfg, api, token)

    @app.get("/")
    def dashboard():  # type: ignore[unused-variable]
        return render_template("dashboard.html", refresh_seconds=cfg.web_refresh_seconds)

    @app.get("/api/dashboard")
    def api_dashboard():  # type: ignore[unused-variable]
        payload = dict(service.payload())
        payload["trading"] = controller.status()
        payload["settings"] = {
            "lots_per_trade": cfg.lots_per_trade,
            "capital": cfg.paper_starting_cash,
        }
        return jsonify(payload)

    @app.post("/api/settings")
    def api_settings():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        lots = body.get("lots_per_trade")
        capital = body.get("capital")
        err = settings_mod.validate(lots, capital)
        if err:
            return jsonify({"ok": False, "message": err}), 400
        messages = []
        if lots is not None:
            cfg.lots_per_trade = int(lots)
            engine = controller._engine
            if controller.running and engine is not None:
                engine.cfg.lots_per_trade = int(lots)
            messages.append(f"lots per trade set to {int(lots)} (applies to new entries)")
        if capital is not None:
            if controller.running and controller.status().get("mode") == "paper":
                return jsonify(
                    {"ok": False, "message": "stop the paper engine before changing capital"}
                ), 409
            capital = float(capital)
            cfg.paper_starting_cash = capital
            # reset the paper account's free cash to the new capital
            state = {}
            if os.path.exists(cfg.paper_state_file):
                try:
                    with open(cfg.paper_state_file, encoding="utf-8") as fh:
                        state = json.load(fh)
                except (ValueError, OSError):
                    state = {}
            state["cash"] = capital
            os.makedirs(os.path.dirname(cfg.paper_state_file) or ".", exist_ok=True)
            with open(cfg.paper_state_file, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            messages.append(f"paper capital set to ₹{capital:,.0f}")
        settings_mod.save_overrides(cfg, lots=lots, capital=capital)
        service._cached = None  # bust cache so the strip updates immediately
        return jsonify({"ok": True, "message": "; ".join(messages) or "nothing to change"})

    @app.post("/api/index-toggle")
    def api_index_toggle():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", ""))
        enabled = bool(body.get("enabled", True))
        # cfg.instruments objects are shared with any running engine (the
        # engine start uses a shallow config copy), so this applies instantly
        index = next((ix for ix in cfg.instruments if ix.name == name), None)
        if index is None:
            return jsonify({"ok": False, "message": f"unknown index {name!r}"}), 404
        if not index.options_available and enabled:
            return jsonify(
                {"ok": False, "message": f"{name} has no listed options — it is always signal-only"}
            ), 400
        index.trade_enabled = enabled
        settings_mod.save_overrides(cfg, trade_toggle=(name, enabled))
        service._cached = None
        state = "ON" if enabled else "OFF (signals still shown; open positions still managed)"
        return jsonify({"ok": True, "message": f"trading for {name}: {state}"})

    @app.get("/api/trades")
    def api_trades():  # type: ignore[unused-variable]
        mode = request.args.get("mode", "paper")
        path = cfg.live_trade_log if mode == "live" else cfg.paper_trade_log
        return jsonify({"mode": mode, "trades": read_trade_log(path)})

    @app.get("/trades.csv")
    def trades_csv():  # type: ignore[unused-variable]
        mode = request.args.get("mode", "paper")
        path = cfg.live_trade_log if mode == "live" else cfg.paper_trade_log
        if not os.path.exists(path):
            return jsonify({"ok": False, "message": f"no {mode} trades logged yet"}), 404
        return send_file(
            os.path.abspath(path),
            as_attachment=True,
            download_name=f"onlyichu_trades_{mode}_{datetime.now().strftime('%Y%m%d')}.csv",
            mimetype="text/csv",
        )

    @app.post("/api/trading/start")
    def trading_start():  # type: ignore[unused-variable]
        body = request.get_json(silent=True) or {}
        mode = str(body.get("mode", "paper")).lower()
        if mode == "live" and body.get("confirm") != "LIVE":
            return jsonify({"ok": False, "message": 'LIVE mode places real orders — confirmation "LIVE" required'}), 400
        ok, msg = controller.start(mode)
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    @app.post("/api/trading/stop")
    def trading_stop():  # type: ignore[unused-variable]
        ok, msg = controller.stop()
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    @app.post("/api/trading/squareoff")
    def trading_squareoff():  # type: ignore[unused-variable]
        ok, msg = controller.square_off()
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)

    return app


def run_web(cfg: Config, api: UpstoxAPI, token: str | None = None) -> None:
    app = create_app(cfg, api, token)
    log.info("dashboard on http://%s:%d (refresh every %ds)", cfg.web_host, cfg.web_port, cfg.web_refresh_seconds)
    app.run(host=cfg.web_host, port=cfg.web_port, debug=False, threaded=True)
