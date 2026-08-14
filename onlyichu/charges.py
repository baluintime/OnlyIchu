"""Estimated Indian F&O (index options) trading charges.

Brokers like Upstox report **Net P&L = Gross P&L − charges**. The strategy only
ever buys options (calls for longs, puts for shorts), so every trade is a BUY
(entry) then a SELL (exit). Statutory + exchange charges apply per leg:

- **Brokerage**: flat per executed order (Upstox: ₹20/order for F&O), or a %% of
  turnover, whichever is lower when a %% is configured.
- **STT**: on the SELL side only, on option premium.
- **Exchange transaction charge**: on premium turnover, both legs.
- **SEBI turnover fee**: on turnover, both legs.
- **Stamp duty**: on the BUY side only.
- **GST**: on (brokerage + exchange txn + SEBI), both legs.

Rates are configurable (`charges:` in config.yaml) because the exchanges revise
them; the shipped defaults follow the current NSE/Upstox schedule. These are
estimates — tune them to match your contract notes exactly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargesConfig:
    enabled: bool = False
    brokerage_per_order: float = 20.0     # flat ₹/order (F&O)
    brokerage_pct: float = 0.0            # if > 0, charge = min(flat, pct * turnover)
    stt_sell_pct: float = 0.001           # STT on option SELL premium (0.1%)
    exchange_txn_pct: float = 0.0003503   # NSE options txn charge on premium turnover
    sebi_pct: float = 0.000001            # SEBI turnover fee (₹10 / crore)
    stamp_buy_pct: float = 0.00003        # stamp duty on BUY value (0.003%)
    gst_pct: float = 0.18                 # GST on (brokerage + txn + sebi)


def _brokerage(cfg: ChargesConfig, turnover: float) -> float:
    if cfg.brokerage_pct > 0:
        return min(cfg.brokerage_per_order, cfg.brokerage_pct * turnover)
    return cfg.brokerage_per_order


def buy_charges(cfg: ChargesConfig, price: float, qty: int) -> float:
    """Charges for a single BUY (entry) leg. 0 when disabled or unpriced."""
    if not cfg.enabled or price <= 0 or qty <= 0:
        return 0.0
    turnover = price * qty
    brokerage = _brokerage(cfg, turnover)
    txn = cfg.exchange_txn_pct * turnover
    sebi = cfg.sebi_pct * turnover
    stamp = cfg.stamp_buy_pct * turnover
    gst = cfg.gst_pct * (brokerage + txn + sebi)
    return round(brokerage + txn + sebi + stamp + gst, 2)


def sell_charges(cfg: ChargesConfig, price: float, qty: int) -> float:
    """Charges for a single SELL (exit) leg. 0 when disabled or unpriced."""
    if not cfg.enabled or price <= 0 or qty <= 0:
        return 0.0
    turnover = price * qty
    brokerage = _brokerage(cfg, turnover)
    txn = cfg.exchange_txn_pct * turnover
    sebi = cfg.sebi_pct * turnover
    stt = cfg.stt_sell_pct * turnover
    gst = cfg.gst_pct * (brokerage + txn + sebi)
    return round(brokerage + txn + sebi + stt + gst, 2)


def round_trip_charges(cfg: ChargesConfig, buy_price: float, sell_price: float, qty: int) -> float:
    """Total charges for a completed BUY→SELL trade of `qty`."""
    return round(buy_charges(cfg, buy_price, qty) + sell_charges(cfg, sell_price, qty), 2)
