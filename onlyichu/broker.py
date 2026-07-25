"""Execution layer: shared position/trade bookkeeping with a simulated
(paper) implementation and a real Upstox (live) implementation.

Both brokers hold at most one position per pipeline and log every fill to a
CSV trade log. In both directions the strategy only ever BUYS options
(calls for longs, puts for shorts), so "exit" always means selling what we
hold. Only real exchange-listed contracts are traded — indices with no
options are signal-only and never reach the broker.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time as _time
from dataclasses import asdict, dataclass, field, fields
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
    direction: str  # "LONG" | "SHORT" (underlying view; the option is always bought)
    entry_spot: float | None = None  # underlying index level at entry
    strike: float | None = None  # option strike price

    def pnl(self, exit_price: float) -> float:
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
        self.api = None  # set by subclasses; used for live mark-to-market
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
        underlying_spot: float | None = None,
        strike: float | None = None,
    ) -> Position | None:
        if pipeline_id in self.state.positions:
            log.warning("%s already holds a position; entry skipped", pipeline_id)
            return None
        fill = self._fill_buy(instrument_key, qty, price_hint)
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
            entry_spot=underlying_spot,
            strike=strike,
        )
        self.state.positions[pipeline_id] = pos
        self._log_trade("ENTRY", pos, fill, 0.0, index_price=underlying_spot)
        log.info(
            "%s entered %s @ %.2f (index %s)", pipeline_id, pos.symbol, fill,
            f"{underlying_spot:.2f}" if underlying_spot is not None else "n/a",
        )
        self._persist()
        return pos

    def exit(
        self,
        pipeline_id: str,
        price_hint: float | None,
        note: str = "",
        underlying_spot: float | None = None,
    ) -> float | None:
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
        self._log_trade(f"EXIT{(' ' + note) if note else ''}", pos, fill, pnl, index_price=underlying_spot)
        self._persist()
        log.info(
            "%s exited %s @ %.2f pnl=%+.2f (index %s vs entry %s)",
            pipeline_id, pos.symbol, fill, pnl,
            f"{underlying_spot:.2f}" if underlying_spot is not None else "n/a",
            f"{pos.entry_spot:.2f}" if pos.entry_spot is not None else "n/a",
        )
        return pnl

    def realized_pnl_today(self) -> float:
        self._roll_pnl_date()
        return self.state.realized_pnl_today

    def mark_to_market(self) -> tuple[float, dict[str, dict]]:
        """Current unrealized PnL across open positions, plus per-position detail
        (ltp, upnl, strike, ...). Uses the broker's API for live option LTPs."""
        positions = self.open_positions()
        detail: dict[str, dict] = {}
        total = 0.0
        ltps: dict[str, float] = {}
        if positions and self.api is not None:
            try:
                ltps = self.api.ltp([p.instrument_key for p in positions])
            except Exception as exc:  # noqa: BLE001 - MTM is best-effort
                log.debug("mark-to-market LTP fetch failed: %s", exc)
        for p in positions:
            ltp = ltps.get(p.instrument_key)
            upnl = p.pnl(ltp) if ltp is not None else None
            if upnl is not None:
                total += upnl
            detail[p.pipeline_id] = {
                "symbol": p.symbol, "strike": p.strike, "direction": p.direction,
                "qty": p.qty, "entry_price": p.entry_price, "ltp": ltp, "upnl": upnl,
                "entry_spot": p.entry_spot,
            }
        return total, detail

    def total_pnl(self) -> float:
        """Realized today + current unrealized (mark-to-market)."""
        return self.realized_pnl_today() + self.mark_to_market()[0]

    # -- broker reconciliation (live only; paper is its own source of truth) --

    def reconcile(self) -> list[dict]:
        """Return per-instrument mismatches between the app book and the real
        broker. Empty = in sync. Paper mode is always in sync."""
        return []

    def seed_from_upstox(self) -> int:
        """Adopt any untracked real positions into the app book so square-off
        closes what actually exists. No-op for paper. Returns count adopted."""
        return 0

    # -- hooks ----------------------------------------------------------

    def _fill_buy(self, instrument_key: str, qty: int, price_hint: float | None) -> float | None:
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

    def _log_trade(
        self, action: str, pos: Position, price: float, pnl: float, index_price: float | None = None
    ) -> None:
        new_file = not os.path.exists(self.trade_log_path)
        with open(self.trade_log_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(
                    ["time", "pipeline", "action", "symbol", "instrument_key",
                     "direction", "qty", "price", "index_price", "pnl"]
                )
            writer.writerow(
                [datetime.now().isoformat(timespec="seconds"), pos.pipeline_id, action,
                 pos.symbol, pos.instrument_key, pos.direction, pos.qty,
                 f"{price:.2f}", f"{index_price:.2f}" if index_price is not None else "",
                 f"{pnl:.2f}"]
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

    def _fill_buy(self, instrument_key, qty, price_hint):
        price = self._quote(instrument_key, price_hint)
        if price is None:
            log.error("paper buy skipped: no price for %s", instrument_key)
            return None
        fill = _round_tick(price * (1 + self.cfg.paper_slippage_pct / 100.0))
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
        self.state.cash += fill * pos.qty
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
            known = {f.name for f in fields(Position)}
            self.state.positions = {
                k: Position(**{a: b for a, b in v.items() if a in known})
                for k, v in (payload.get("positions") or {}).items()
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
        self.pending_unconfirmed = False  # a fill we couldn't confirm — force a reconcile

    # -- reconciliation against the real Upstox position book -----------

    def _upstox_net(self) -> dict[str, dict]:
        """Net (non-zero) Upstox positions keyed by instrument, {qty, avg, symbol}."""
        out: dict[str, dict] = {}
        for p in self.api.positions():
            key = p.get("instrument_token") or p.get("instrument_key")
            qty = int(p.get("quantity") or 0)
            if not key or qty == 0:
                continue
            out[key] = {
                "qty": qty,
                "avg": float(p.get("average_price") or 0.0),
                "symbol": p.get("tradingsymbol") or p.get("trading_symbol") or key,
            }
        return out

    def _app_net(self) -> dict[str, int]:
        net: dict[str, int] = {}
        for pos in self.open_positions():
            net[pos.instrument_key] = net.get(pos.instrument_key, 0) + pos.qty
        return net

    def reconcile(self) -> list[dict]:
        try:
            ups = self._upstox_net()
        except UpstoxError as exc:
            log.warning("reconcile: could not fetch Upstox positions: %s", exc)
            return []  # can't compare — don't raise a false mismatch
        app = self._app_net()
        mismatches = []
        for key in set(ups) | set(app):
            u = ups.get(key, {}).get("qty", 0)
            a = app.get(key, 0)
            if u != a:
                mismatches.append({
                    "instrument": key,
                    "symbol": ups.get(key, {}).get("symbol", key),
                    "app_qty": a,
                    "upstox_qty": u,
                })
        return mismatches

    def seed_from_upstox(self) -> int:
        try:
            ups = self._upstox_net()
        except UpstoxError as exc:
            log.warning("seed: could not fetch Upstox positions: %s", exc)
            return 0
        app = self._app_net()
        added = 0
        for key, info in ups.items():
            delta = info["qty"] - app.get(key, 0)
            if delta <= 0:  # already tracked (or app thinks it holds more — reconcile flags that)
                continue
            pid = base = f"ADOPTED:{info['symbol']}"
            i = 1
            while pid in self.state.positions:
                i += 1
                pid = f"{base}#{i}"
            self.state.positions[pid] = Position(
                pipeline_id=pid, instrument_key=key, symbol=info["symbol"], qty=delta,
                entry_price=info["avg"], entry_time=datetime.now().isoformat(timespec="seconds"),
                direction="LONG",
            )
            added += 1
            log.warning("adopted untracked Upstox position %s x%d @ %.2f", info["symbol"], delta, info["avg"])
        return added

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
            # Unknown outcome: the order may still fill at the exchange. Do NOT
            # assume nothing happened — flag for reconciliation against Upstox.
            self.pending_unconfirmed = True
            log.error(
                "market retry %s not confirmed — flagging for reconcile (may have filled at exchange)",
                order_id,
            )
        return None

    def _fill_buy(self, instrument_key, qty, price_hint):
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
