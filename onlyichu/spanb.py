"""Span B slope strategy (the 'spanb' engine).

A separate, self-contained strategy that trades purely off the slope of the
Ichimoku **Senkou Span B** line (the slow Kumo boundary), independent of the
close-vs-cloud breakout logic in `strategy.py`:

- **Span B falling** (current < previous) → the slow trend is turning down →
  **sell an OUT-OF-THE-MONEY CALL** (short premium on the side price is leaving).
- **Span B rising** (current > previous) → **sell an OUT-OF-THE-MONEY PUT**.
- **Exit when it reverts**: a short call is bought back when Span B turns up
  again; a short put when Span B turns down. A flat/unchanged Span B holds.

Entry is a SELL-to-open (collect premium); exit is a BUY-to-close. The strike is
`otm_strikes` strikes out of the money.

This module is pure decision logic (no I/O). It is kept apart from the Ichimoku
strategy so either can run — or be reverted to — without touching the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .candles import Candle, CandleSeries
from .ichimoku import IchimokuParams, compute_state

log = logging.getLogger(__name__)

# Actions a Span B pipeline can emit on a candle close
EXIT = "EXIT"
SELL_CALL = "SELL_CALL"   # -> sell (write) an OTM CALL
SELL_PUT = "SELL_PUT"     # -> sell (write) an OTM PUT

# Position sides for this strategy (short premium)
SHORT_CALL = "SHORT_CALL"
SHORT_PUT = "SHORT_PUT"


@dataclass(frozen=True)
class SpanbConfig:
    ich: IchimokuParams
    otm_strikes: int = 5      # how many strikes out of the money to sell

    @property
    def min_candles(self) -> int:
        # need Span B at the current and the previous candle for a slope
        return self.ich.min_candles + 1


@dataclass(frozen=True)
class SpanbEval:
    ready: bool
    span_b: float | None
    prev_span_b: float | None
    slope: str            # "UP" | "DOWN" | "FLAT"
    signal: str           # "SELL_PUT" (up) | "SELL_CALL" (down) | "NEUTRAL"


def spanb_evaluate(
    highs: list[float], lows: list[float], params: IchimokuParams, index: int | None = None
) -> SpanbEval | None:
    """Span B slope at candle `index` (default: last). None until there is enough
    history for Span B at both this candle and the previous one."""
    i = len(highs) - 1 if index is None else index
    if i < 1:
        return None
    cur = compute_state(highs, lows, params, index=i)
    prev = compute_state(highs, lows, params, index=i - 1)
    if cur is None or prev is None:
        return None
    span_b, prev_span_b = cur.span_b, prev.span_b
    if span_b > prev_span_b:
        slope, signal = "UP", "SELL_PUT"
    elif span_b < prev_span_b:
        slope, signal = "DOWN", "SELL_CALL"
    else:
        slope, signal = "FLAT", "NEUTRAL"
    return SpanbEval(
        ready=True, span_b=span_b, prev_span_b=prev_span_b, slope=slope, signal=signal
    )


def spanb_decide(ev: SpanbEval, position_side: str | None) -> list[str]:
    """Ordered actions from a Span B eval + held side.

    Exit a short call when Span B turns UP again (revert), a short put when it
    turns DOWN. After exiting on a reversal, the opposite side is opened on the
    same candle. A FLAT slope holds. Never re-opens the same side it just exited."""
    actions: list[str] = []
    side = position_side
    exited: str | None = None
    if side == SHORT_CALL and ev.slope == "UP":
        actions.append(EXIT)
        exited, side = SHORT_CALL, None
    elif side == SHORT_PUT and ev.slope == "DOWN":
        actions.append(EXIT)
        exited, side = SHORT_PUT, None
    if side is None:
        if ev.slope == "DOWN" and exited != SHORT_CALL:
            actions.append(SELL_CALL)
        elif ev.slope == "UP" and exited != SHORT_PUT:
            actions.append(SELL_PUT)
    return actions


@dataclass
class SpanbSignal:
    pipeline_id: str
    action: str
    candle: Candle
    span_b: float


class SpanbPipeline:
    """One (index, timeframe) Span B pipeline."""

    def __init__(self, index_name: str, timeframe_min: int, sc: SpanbConfig):
        self.index_name = index_name
        self.timeframe_min = timeframe_min
        self.sc = sc
        self.series = CandleSeries(max_len=max(2000, sc.min_candles * 4))

    @property
    def pipeline_id(self) -> str:
        return f"{self.index_name}:{self.timeframe_min}m"

    def warmup(self, candles: list[Candle]) -> None:
        for c in candles:
            self.series.append(c)

    def evaluate(self) -> SpanbEval | None:
        return spanb_evaluate(self.series.highs, self.series.lows, self.sc.ich)

    def on_candle_close(self, candle: Candle, position_side: str | None) -> list[SpanbSignal]:
        if not self.series.append(candle):
            return []
        ev = self.evaluate()
        if ev is None:
            return []
        actions = spanb_decide(ev, position_side)
        if actions:
            log.info(
                "%s %s spanB=%.2f prev=%.2f slope=%s -> %s",
                self.pipeline_id, candle.ts.strftime("%H:%M"),
                ev.span_b or 0.0, ev.prev_span_b or 0.0, ev.slope, ",".join(actions),
            )
        return [SpanbSignal(self.pipeline_id, a, candle, ev.span_b or 0.0) for a in actions]


def build_spanb_config(cfg) -> SpanbConfig:
    """Build a SpanbConfig from the app Config (duck-typed)."""
    return SpanbConfig(
        ich=IchimokuParams(cfg.tenkan, cfg.kijun, cfg.senkou_b, cfg.displacement),
        otm_strikes=getattr(cfg, "spanb_otm_strikes", 5),
    )
