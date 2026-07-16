"""ITM option selection per the strategy's risk framework.

Delta profile 0.65–0.75 (solidly ITM), nearest weekly/0DTE expiry. The
selection uses Upstox's option-chain endpoint, which returns per-strike
greeks and LTP. If greeks are missing (illiquid strikes), falls back to a
strike roughly two steps in the money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from .config import Config
from .upstox_api import UpstoxAPI

log = logging.getLogger(__name__)


@dataclass
class OptionSelection:
    instrument_key: str
    trading_symbol: str
    strike: float
    option_type: str  # "CE" | "PE"
    expiry: str  # YYYY-MM-DD
    lot_size: int
    ltp: float | None
    delta: float | None


class OptionSelector:
    def __init__(self, api: UpstoxAPI, cfg: Config):
        self.api = api
        self.cfg = cfg
        self._contracts_cache: dict[str, list[dict]] = {}

    # ------------------------------------------------------------- expiries

    def _contracts(self, underlying_key: str) -> list[dict]:
        if underlying_key not in self._contracts_cache:
            try:
                self._contracts_cache[underlying_key] = self.api.option_contracts(underlying_key)
            except Exception as exc:  # noqa: BLE001 - report and treat as no contracts
                log.warning("option contracts lookup failed for %s: %s", underlying_key, exc)
                self._contracts_cache[underlying_key] = []
        return self._contracts_cache[underlying_key]

    def has_options(self, underlying_key: str) -> bool:
        return bool(self._contracts(underlying_key))

    def nearest_expiry(self, underlying_key: str, today: date | None = None) -> str | None:
        today = today or datetime.now().date()
        expiries = set()
        for c in self._contracts(underlying_key):
            exp = c.get("expiry")
            if exp:
                expiries.add(str(exp)[:10])
        future = sorted(e for e in expiries if date.fromisoformat(e) >= today)
        return future[0] if future else None

    def lot_size(self, underlying_key: str, expiry: str) -> int:
        for c in self._contracts(underlying_key):
            if str(c.get("expiry", ""))[:10] == expiry and c.get("lot_size"):
                return int(c["lot_size"])
        for c in self._contracts(underlying_key):
            if c.get("lot_size"):
                return int(c["lot_size"])
        return 1

    # ------------------------------------------------------------ selection

    def select_itm(self, underlying_key: str, direction: str, spot: float) -> OptionSelection | None:
        """Pick an ITM option for `direction` ("LONG" -> CE, "SHORT" -> PE).

        Prefers the strike whose |delta| is closest to target_delta within
        [delta_min, delta_max]; falls back to ~2 strikes in the money.
        """
        expiry = self.nearest_expiry(underlying_key)
        if not expiry:
            return None
        opt_field = "call_options" if direction == "LONG" else "put_options"
        opt_type = "CE" if direction == "LONG" else "PE"
        try:
            chain = self.api.option_chain(underlying_key, expiry)
        except Exception as exc:  # noqa: BLE001
            log.warning("option chain fetch failed for %s %s: %s", underlying_key, expiry, exc)
            return None
        if not chain:
            return None

        lot = self.lot_size(underlying_key, expiry)
        candidates: list[tuple[float, dict, float, float | None, float | None]] = []
        fallback: list[tuple[float, dict]] = []

        for row in chain:
            strike = float(row.get("strike_price", 0) or 0)
            leg = row.get(opt_field) or {}
            if not leg.get("instrument_key"):
                continue
            itm = strike < spot if opt_type == "CE" else strike > spot
            if not itm:
                continue
            ltp = ((leg.get("market_data") or {}).get("ltp"))
            ltp = float(ltp) if ltp not in (None, 0) else None
            delta = (leg.get("option_greeks") or {}).get("delta")
            delta = float(delta) if delta is not None else None
            fallback.append((strike, leg))
            if delta is not None and self.cfg.delta_min <= abs(delta) <= self.cfg.delta_max:
                candidates.append(
                    (abs(abs(delta) - self.cfg.target_delta), row, strike, ltp, delta)
                )

        if candidates:
            candidates.sort(key=lambda t: t[0])
            _, row, strike, ltp, delta = candidates[0]
            leg = row[opt_field]
        elif fallback:
            # ~2 strikes in the money: for calls the 2nd-highest strike below
            # spot; for puts the 2nd-lowest strike above spot.
            fallback.sort(key=lambda t: t[0], reverse=(opt_type == "CE"))
            strike, leg = fallback[min(1, len(fallback) - 1)]
            ltp = ((leg.get("market_data") or {}).get("ltp"))
            ltp = float(ltp) if ltp not in (None, 0) else None
            delta = (leg.get("option_greeks") or {}).get("delta")
            delta = float(delta) if delta is not None else None
            log.warning(
                "%s %s: no strike with delta in [%.2f, %.2f]; fell back to strike %.0f",
                underlying_key, expiry, self.cfg.delta_min, self.cfg.delta_max, strike,
            )
        else:
            return None

        return OptionSelection(
            instrument_key=leg["instrument_key"],
            trading_symbol=leg.get("trading_symbol") or leg.get("tradingsymbol") or leg["instrument_key"],
            strike=strike,
            option_type=opt_type,
            expiry=expiry,
            lot_size=lot,
            ltp=ltp,
            delta=delta,
        )
