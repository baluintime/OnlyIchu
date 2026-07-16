"""Execution layer: shared position/trade bookkeeping with a simulated
(paper) implementation and a real Upstox (live) implementation.

Both brokers hold at most one position per pipeline and log every fill to a
CSV trade log. In both directions the strategy only ever BUYS options
(calls for longs, puts for shorts), so "exit" always means selling what we
hold. Paper mode can additionally hold synthetic index positions for
indices that have no exchange-traded options.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .config import Config
from .upstox_api import UpstoxAPI, UpstoxError

log = logging.getLogger(__name__)

TICK = 0.05


def _round_tick(price: float) -> float:
    return max(TICK, round(round(price / TICK) * TICK, 2))


@dataclass
class Position:
    pipeline_id: str
    instrument_key: str
    symbol: str
    qty: int
    entry_price: float
    entry_time: str
    direction: str  # "LONG" | "SHORT" (underlying view)
    kind: str  # "OPTION" (long premium) | "SYNTHETIC" (index-level, paper only)

    def pnl(self, exit_price: float) -> float:
        if self.kind == "SYNTHETIC" and self.direction == "SHORT":
            return (self.entry_price - exit_price) * self.qty
        return (exit_price - self.entry_price) * self.qty


@dataclass
class BrokerState:
    cash: float = 0.0
    realized_pnl_today: float = 0.0
    pnl_date: str = ""
    positions: dict[str, Position] = field(default_factory=dict)


class BaseBroker:
    """Common bookkeeping; subclasses implement _fill_buy/_fill_sell."""

    def __init__(self, cfg: Config, trade_log_path: str):
        self.cfg = cfg
        self.state = BrokerState(pnl_date=datetime.now().strftime("%Y-%m-%d"))
        self.trade_log_path = trade_log_path
        os.makedirs(os.path.dirname(trade_log_path) or ".", exist_ok=True)

    # -- interface -----------------------------------------------------

    def position(self, pipeline_id: str) -> Position | None:
        return self.state.positions.get(pipeline_id)

    def position_side(self, pipeline_id: str) -> str | None:
        pos = self.position(pipeline_id)
        return pos.direction if pos else None

    def open_positions(self) -> list[Position]:
        return list(self.state.positions.values())

    def enter(
        self,
        pipeline_id: str,
        instrument_key: str,
        symbol: str,
        qty: int,
        direction: str,
        price_hint: float | None,
        kind: str = "OPTION",
    ) -> Position | None:
        if pipeline_id in self.state.positions:
            log.warning("%s already holds a position; entry skipped", pipeline_id)
            return None
        fill = self._fill_buy(instrument_key, qty, price_hint, kind)
        if fill is None:
            return None
        pos = Position(
            pipeline_id=pipeline_id,
            instrument_key=instrument_key,
            symbol=symbol,
            qty=qty,
            entry_price=fill,
            entry_time=datetime.now().isoformat(timespec="seconds"),
            direction=direction,
            kind=kind,
        )
        self.state.positions[pipeline_id] = pos
        self._log_trade("ENTRY", pos, fill, 0.0)
        self._persist()
        return pos

    def exit(self, pipeline_id: str, price_hint: float | None, note: str = "") -> float | None:
        pos = self.state.positions.get(pipeline_id)
        if pos is None:
            return None
        fill = self._fill_sell(pos, price_hint)
        if fill is None:
            return None
        pnl = pos.pnl(fill)
        self._roll_pnl_date()
        self.state.realized_pnl_today += pnl
        del self.state.positions[pipeline_id]
        self._log_trade(f"EXIT{(' ' + note) if note else ''}", pos, fill, pnl)
        self._persist()
        log.info("%s exited %s @ %.2f pnl=%+.2f", pipeline_id, pos.symbol, fill, pnl)
        return pnl

    def realized_pnl_today(self) -> float:
        self._roll_pnl_date()
        return self.state.realized_pnl_today

    # -- hooks ----------------------------------------------------------

    def _fill_buy(
        self, instrument_key: str, qty: int, price_hint: float | None, kind: str
    ) -> float | None:
        raise NotImplementedError

    def _fill_sell(self, pos: Position, price_hint: float | None) -> float | None:
        raise NotImplementedError

    def _persist(self) -> None:
        pass

    # -- helpers ---------------------------------------------------------

    def _roll_pnl_date(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.pnl_date != today:
            self.state.pnl_date = today
            self.state.realized_pnl_today = 0.0

    def _log_trade(self, action: str, pos: Position, price: float, pnl: float) -> None:
        new_file = not os.path.exists(self.trade_log_path)
        with open(self.trade_log_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(
                    ["time", "pipeline", "action", "kind", "symbol", "instrument_key",
                     "direction", "qty", "price", "pnl"]
                )
            writer.writerow(
                [datetime.now().isoformat(timespec="seconds"), pos.pipeline_id, action,
                 pos.kind, pos.symbol, pos.instrument_key, pos.direction, pos.qty,
                 f"{price:.2f}", f"{pnl:.2f}"]
            )


class PaperBroker(BaseBroker):
    """Simulated fills at live LTP with configurable slippage; state persists
    to JSON so a restart resumes open paper positions."""

    def __init__(self, cfg: Config, api: UpstoxAPI | None):
        super().__init__(cfg, cfg.paper_trade_log)
        self.api = api
        self.state.cash = cfg.paper_starting_cash
        self._state_file = cfg.paper_state_file
        self._load()

    def _quote(self, instrument_key: str, price_hint: float | None) -> float | None:
        if self.api is not None:
            try:
                ltp = self.api.ltp_single(instrument_key)
                if ltp:
                    return ltp
            except UpstoxError as exc:
                log.warning("LTP fetch failed for %s: %s", instrument_key, exc)
        return price_hint

    def _fill_buy(self, instrument_key, qty, price_hint, kind):
        price = self._quote(instrument_key, price_hint)
        if price is None:
            log.error("paper buy skipped: no price for %s", instrument_key)
            return None
        fill = _round_tick(price * (1 + self.cfg.paper_slippage_pct / 100.0))
        if kind == "OPTION":
            cost = fill * qty
            if cost > self.state.cash:
                log.error("paper buy skipped: cost %.2f exceeds cash %.2f", cost, self.state.cash)
                return None
            self.state.cash -= cost
        return fill

    def _fill_sell(self, pos, price_hint):
        price = self._quote(pos.instrument_key, price_hint)
        if price is None:
            log.error("paper sell has no live price for %s; using entry price", pos.instrument_key)
            price = pos.entry_price
        fill = _round_tick(price * (1 - self.cfg.paper_slippage_pct / 100.0))
        if pos.kind == "OPTION":
            self.state.cash += fill * pos.qty
        else:
            self.state.cash += pos.pnl(fill)
        return fill

    # -- persistence ------------------------------------------------------

    def _persist(self) -> None:
        os.makedirs(os.path.dirname(self._state_file) or ".", exist_ok=True)
        payload = {
            "cash": self.state.cash,
            "realized_pnl_today": self.state.realized_pnl_today,
            "pnl_date": self.state.pnl_date,
            "positions": {k: asdict(v) for k, v in self.state.positions.items()},
        }
        with open(self._state_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def _load(self) -> None:
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.state.cash = float(payload.get("cash", self.state.cash))
            self.state.realized_pnl_today = float(payload.get("realized_pnl_today", 0.0))
            self.state.pnl_date = payload.get("pnl_date", self.state.pnl_date)
            self.state.positions = {
                k: Position(**v) for k, v in (payload.get("positions") or {}).items()
            }
            if self.state.positions:
                log.info("resumed %d open paper position(s)", len(self.state.positions))
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("could not load paper state (%s); starting fresh", exc)


class LiveBroker(BaseBroker):
    """Places real orders on Upstox. Uses marketable LIMIT orders (LTP +/-
    limit_tolerance_pct) per the spec's slippage guidance, or MARKET if
    configured. Fill price is read back from order details."""

    FILL_POLL_SECONDS = 15

    def __init__(self, cfg: Config, api: UpstoxAPI):
        super().__init__(cfg, cfg.live_trade_log)
        self.api = api

    def _place_and_wait(
        self, instrument_key: str, qty: int, side: str, ltp: float | None
    ) -> float | None:
        order_type = self.cfg.order_type if ltp else "MARKET"
        price = 0.0
        if order_type == "LIMIT" and ltp:
            tol = self.cfg.limit_tolerance_pct / 100.0
            price = _round_tick(ltp * (1 + tol) if side == "BUY" else ltp * (1 - tol))
        try:
            order_id = self.api.place_order(
                instrument_key=instrument_key,
                quantity=qty,
                transaction_type=side,
                order_type=order_type,
                price=price,
                product=self.cfg.product,
            )
        except UpstoxError as exc:
            log.error("order placement failed (%s %s x%d): %s", side, instrument_key, qty, exc)
            return None
        log.info("placed %s %s x%d %s@%.2f order_id=%s", side, instrument_key, qty, order_type, price, order_id)

        deadline = _time.time() + self.FILL_POLL_SECONDS
        status = ""
        while _time.time() < deadline:
            try:
                details = self.api.order_details(order_id)
            except UpstoxError:
                _time.sleep(1)
                continue
            status = (details.get("status") or "").lower()
            if status == "complete":
                avg = details.get("average_price")
                return float(avg) if avg else (price or ltp or 0.0)
            if status in ("rejected", "cancelled"):
                log.error("order %s %s: %s", order_id, status, details.get("status_message"))
                return None
            _time.sleep(1)

        # Unfilled limit order: cancel and chase once with a MARKET order.
        log.warning("order %s not filled in %ds (status=%s); cancelling", order_id, self.FILL_POLL_SECONDS, status)
        try:
            self.api.cancel_order(order_id)
        except UpstoxError as exc:
            log.error("cancel failed for %s: %s — check the order book manually", order_id, exc)
            return None
        if order_type == "LIMIT":
            log.info("retrying %s %s as MARKET", side, instrument_key)
            try:
                order_id = self.api.place_order(
                    instrument_key=instrument_key, quantity=qty,
                    transaction_type=side, order_type="MARKET", product=self.cfg.product,
                )
            except UpstoxError as exc:
                log.error("market retry failed: %s", exc)
                return None
            _time.sleep(2)
            try:
                details = self.api.order_details(order_id)
                if (details.get("status") or "").lower() == "complete":
                    avg = details.get("average_price")
                    return float(avg) if avg else (ltp or 0.0)
            except UpstoxError:
                pass
            log.error("market retry %s not confirmed — check the order book manually", order_id)
        return None

    def _fill_buy(self, instrument_key, qty, price_hint, kind):
        if kind != "OPTION":
            log.warning("live mode cannot trade synthetic index positions; skipped %s", instrument_key)
            return None
        ltp = price_hint
        try:
            ltp = self.api.ltp_single(instrument_key) or price_hint
        except UpstoxError:
            pass
        return self._place_and_wait(instrument_key, qty, "BUY", ltp)

    def _fill_sell(self, pos, price_hint):
        ltp = price_hint
        try:
            ltp = self.api.ltp_single(pos.instrument_key) or price_hint
        except UpstoxError:
            pass
        return self._place_and_wait(pos.instrument_key, pos.qty, "SELL", ltp)
