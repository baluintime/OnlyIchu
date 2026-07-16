"""Per-index, per-timeframe signal pipeline.

Each (index, timeframe) pair is an independent pipeline, per the spec's
"two completely separate, parallel execution pipelines" requirement. A
pipeline only turns completed candles into abstract actions; execution
(option selection, orders, PnL) is the engine/broker's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .candles import Candle, CandleSeries
from .ichimoku import (
    IchimokuParams,
    IchimokuState,
    compute_state,
    long_entry,
    long_exit,
    short_entry,
    short_exit,
)

log = logging.getLogger(__name__)

# Actions a pipeline can emit on a candle close
EXIT = "EXIT"
ENTER_LONG = "ENTER_LONG"    # -> buy ITM CALL at next candle open
ENTER_SHORT = "ENTER_SHORT"  # -> buy ITM PUT at next candle open


def decide(close: float, state: IchimokuState, position_side: str | None) -> list[str]:
    """Pure decision function: current close + ichimoku state + held side -> actions.

    position_side is "LONG", "SHORT" or None. Returned actions are ordered
    (an EXIT always precedes a same-candle reversal entry).
    """
    actions: list[str] = []
    side = position_side
    if side == "LONG" and long_exit(close, state):
        actions.append(EXIT)
        side = None
    elif side == "SHORT" and short_exit(close, state):
        actions.append(EXIT)
        side = None
    if side is None:
        if long_entry(close, state):
            actions.append(ENTER_LONG)
        elif short_entry(close, state):
            actions.append(ENTER_SHORT)
    return actions


@dataclass
class Signal:
    pipeline_id: str
    action: str
    candle: Candle
    state: IchimokuState


class Pipeline:
    def __init__(self, index_name: str, timeframe_min: int, params: IchimokuParams):
        self.index_name = index_name
        self.timeframe_min = timeframe_min
        self.params = params
        self.series = CandleSeries(max_len=max(2000, params.min_candles * 4))

    @property
    def pipeline_id(self) -> str:
        return f"{self.index_name}:{self.timeframe_min}m"

    def warmup(self, candles: list[Candle]) -> None:
        for c in candles:
            self.series.append(c)

    def on_candle_close(self, candle: Candle, position_side: str | None) -> list[Signal]:
        """Process a completed candle; returns zero or more signals."""
        if not self.series.append(candle):
            return []
        state = compute_state(self.series.highs, self.series.lows, self.params)
        if state is None:
            return []
        actions = decide(candle.close, state, position_side)
        if actions:
            log.info(
                "%s %s close=%.2f tenkan=%.2f kijun=%.2f spanA=%.2f spanB=%.2f -> %s",
                self.pipeline_id,
                candle.ts.strftime("%H:%M"),
                candle.close,
                state.tenkan,
                state.kijun,
                state.span_a,
                state.span_b,
                ",".join(actions),
            )
        return [Signal(self.pipeline_id, a, candle, state) for a in actions]
