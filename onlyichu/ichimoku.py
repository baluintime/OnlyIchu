"""Ichimoku Cloud calculation and the strategy's strict close-based rules.

Implements the exact protocol from the strategy spec:

  LONG entry : candle close strictly ABOVE Tenkan-sen, Kijun-sen, Senkou Span A,
               Senkou Span B (and therefore the whole Kumo body).
  LONG exit  : candle close strictly BELOW ANY single one of those levels.
  SHORT entry: candle close strictly BELOW all levels.
  SHORT exit : candle close strictly ABOVE ANY single one of those levels.

Senkou spans are the *displayed* values at the current candle, i.e. the values
computed `displacement` periods ago and projected forward onto now.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IchimokuParams:
    tenkan: int = 9
    kijun: int = 26
    senkou_b: int = 52
    displacement: int = 26

    @property
    def min_candles(self) -> int:
        return self.senkou_b + self.displacement


@dataclass(frozen=True)
class IchimokuState:
    tenkan: float
    kijun: float
    span_a: float  # displayed at current candle
    span_b: float  # displayed at current candle

    @property
    def cloud_top(self) -> float:
        return max(self.span_a, self.span_b)

    @property
    def cloud_bottom(self) -> float:
        return min(self.span_a, self.span_b)

    @property
    def levels(self) -> tuple[float, float, float, float]:
        return (self.tenkan, self.kijun, self.span_a, self.span_b)


def _donchian_mid(highs: list[float], lows: list[float], end: int, period: int) -> float:
    """Midpoint of highest high / lowest low over `period` candles ending at index `end`."""
    start = end - period + 1
    return (max(highs[start : end + 1]) + min(lows[start : end + 1])) / 2.0


def compute_state(
    highs: list[float], lows: list[float], params: IchimokuParams, index: int | None = None
) -> IchimokuState | None:
    """Ichimoku state at candle `index` (default: last). None if not enough history."""
    i = len(highs) - 1 if index is None else index
    if i + 1 < params.min_candles:
        return None
    j = i - params.displacement  # candle whose projection is displayed at i
    tenkan = _donchian_mid(highs, lows, i, params.tenkan)
    kijun = _donchian_mid(highs, lows, i, params.kijun)
    span_a = (
        _donchian_mid(highs, lows, j, params.tenkan) + _donchian_mid(highs, lows, j, params.kijun)
    ) / 2.0
    span_b = _donchian_mid(highs, lows, j, params.senkou_b)
    return IchimokuState(tenkan=tenkan, kijun=kijun, span_a=span_a, span_b=span_b)


# --------------------------------------------------------------------- rules


def long_entry(close: float, s: IchimokuState) -> bool:
    """Close strictly above all levels (implies above the entire cloud body)."""
    return all(close > level for level in s.levels)


def long_exit(close: float, s: IchimokuState) -> bool:
    """Close strictly below any single level."""
    return any(close < level for level in s.levels)


def short_entry(close: float, s: IchimokuState) -> bool:
    """Close strictly below all levels (implies below the entire cloud body)."""
    return all(close < level for level in s.levels)


def short_exit(close: float, s: IchimokuState) -> bool:
    """Close strictly above any single level."""
    return any(close > level for level in s.levels)
