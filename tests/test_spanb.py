"""Span B slope strategy: slope detection and short-premium decisions."""

from onlyichu.ichimoku import IchimokuParams
from onlyichu.spanb import (
    EXIT, SELL_CALL, SELL_PUT, SHORT_CALL, SHORT_PUT,
    SpanbConfig, spanb_decide, spanb_evaluate,
)

P = IchimokuParams(tenkan=3, kijun=9, senkou_b=12, displacement=3)


def ramp(n, start, step):
    closes = [start + i * step for i in range(n)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    return highs, lows, closes


def test_span_b_rising_signals_sell_put():
    highs, lows, _ = ramp(40, 100.0, 1.0)   # steadily up -> Span B rising
    ev = spanb_evaluate(highs, lows, P)
    assert ev is not None and ev.ready
    assert ev.span_b > ev.prev_span_b
    assert ev.slope == "UP" and ev.signal == "SELL_PUT"


def test_span_b_falling_signals_sell_call():
    highs, lows, _ = ramp(40, 200.0, -1.0)  # steadily down -> Span B falling
    ev = spanb_evaluate(highs, lows, P)
    assert ev.span_b < ev.prev_span_b
    assert ev.slope == "DOWN" and ev.signal == "SELL_CALL"


def test_span_b_flat_is_neutral():
    highs = [100.0] * 40
    lows = [99.0] * 40
    ev = spanb_evaluate(highs, lows, P)
    assert ev.slope == "FLAT" and ev.signal == "NEUTRAL"


def test_warmup_returns_none():
    highs, lows, _ = ramp(5, 100.0, 1.0)
    assert spanb_evaluate(highs, lows, P) is None


def _ev(slope):
    signal = {"UP": "SELL_PUT", "DOWN": "SELL_CALL", "FLAT": "NEUTRAL"}[slope]
    return type("E", (), {"ready": True, "span_b": 1.0, "prev_span_b": 0.0,
                          "slope": slope, "signal": signal})()


def test_decide_entries():
    assert spanb_decide(_ev("DOWN"), None) == [SELL_CALL]
    assert spanb_decide(_ev("UP"), None) == [SELL_PUT]
    assert spanb_decide(_ev("FLAT"), None) == []


def test_decide_holds_until_revert():
    # short call held while Span B keeps falling / is flat
    assert spanb_decide(_ev("DOWN"), SHORT_CALL) == []
    assert spanb_decide(_ev("FLAT"), SHORT_CALL) == []
    # reverts up -> exit the call and open a put in one candle
    assert spanb_decide(_ev("UP"), SHORT_CALL) == [EXIT, SELL_PUT]
    # symmetric for a short put
    assert spanb_decide(_ev("UP"), SHORT_PUT) == []
    assert spanb_decide(_ev("DOWN"), SHORT_PUT) == [EXIT, SELL_CALL]


def test_min_candles():
    sc = SpanbConfig(ich=P)
    assert sc.min_candles == P.min_candles + 1


def test_spanb_engine_constructs_and_warms_up(tmp_path):
    from datetime import datetime, timedelta, timezone

    from onlyichu.config import Config, IndexConfig
    from onlyichu.spanb_engine import SpanbEngine

    IST = timezone(timedelta(hours=5, minutes=30))

    class FakeAPI:
        access_token = "x"
        has_token = True

        def _rows(self, n, start, step, t0):
            out = []
            p = start
            for i in range(n):
                out.append([(t0 + timedelta(minutes=i)).isoformat(), p, p + 1, p - 1, p + step, 1, 0])
                p += step
            return list(reversed(out))

        def historical_candles(self, key, to_date, from_date, unit="1minute"):
            y = datetime(2026, 8, 20, 9, 15, tzinfo=IST)
            return self._rows(200, 1000.0, 0.5, y)

        def intraday_candles(self, key, unit="1minute"):
            t = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0) - timedelta(hours=9)
            return self._rows(60, 1100.0, 1.0, t)

        def ltp_single(self, k):
            return 100.0

        def ltp(self, keys):
            return {k: 100.0 for k in keys}

    cfg = Config()
    cfg.tenkan, cfg.kijun, cfg.senkou_b, cfg.displacement = 3, 9, 12, 3
    cfg.paper_state_file = str(tmp_path / "s.json")
    cfg.paper_trade_log = str(tmp_path / "t.csv")
    cfg.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]

    e = SpanbEngine(cfg, FakeAPI())
    e.warmup()
    assert len(e.runners) == 1
    ev = e.runners[0].pipelines[1].evaluate()
    assert ev is not None and ev.slope in ("UP", "DOWN", "FLAT")


def test_prime_opens_short_call_on_standing_down_slope(tmp_path):
    from datetime import datetime, timedelta, timezone

    from onlyichu.config import Config, IndexConfig
    from onlyichu.spanb import SHORT_CALL
    from onlyichu.spanb_engine import SpanbEngine

    IST = timezone(timedelta(hours=5, minutes=30))

    class FakeAPI:
        access_token = "x"
        has_token = True

        def _rows(self, n, start, step, t0):
            out, p = [], start
            for i in range(n):
                out.append([(t0 + timedelta(minutes=i)).isoformat(), p, p + 1, p - 1, p + step, 5, 0])
                p += step
            return list(reversed(out))

        def historical_candles(self, key, to_date, from_date, unit="1minute"):
            y = datetime(2026, 8, 20, 9, 15, tzinfo=IST)
            return self._rows(200, 2000.0, -0.5, y)          # falling -> Span B DOWN -> SELL_CALL

        def intraday_candles(self, key, unit="1minute"):
            t = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0) - timedelta(hours=9)
            return self._rows(60, 1900.0, -1.0, t)

        def ltp_single(self, k):
            return 30.0

        def ltp(self, keys):
            return {k: 30.0 for k in keys}

        def option_contracts(self, key):
            return [{"expiry": "2026-08-27", "lot_size": 50}]

        def option_chain(self, key, expiry):
            spot = 1841.0  # near the downtrend's last close
            rows = []
            for k in range(1600, 2101, 50):  # strikes around spot, 50 apart
                leg = {"instrument_key": f"NSE_FO|{k}", "trading_symbol": f"X {k} CE",
                       "market_data": {"ltp": 30.0, "oi": 1000, "volume": 500, "bid_price": 29.5, "ask_price": 30.5},
                       "option_greeks": {"delta": 0.3}}
                pleg = dict(leg); pleg["instrument_key"] = f"NSE_FO|{k}P"; pleg["trading_symbol"] = f"X {k} PE"
                rows.append({"strike_price": k, "call_options": leg, "put_options": pleg})
            return rows

    cfg = Config()
    cfg.tenkan, cfg.kijun, cfg.senkou_b, cfg.displacement = 3, 9, 12, 3
    cfg.spanb_otm_strikes = 5
    cfg.min_volume = 1
    cfg.paper_state_file = str(tmp_path / "s.json")
    cfg.paper_trade_log = str(tmp_path / "t.csv")
    cfg.market_open = __import__("datetime").time(0, 0)
    cfg.entry_cutoff = __import__("datetime").time(23, 59)
    cfg.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]

    e = SpanbEngine(cfg, FakeAPI())
    e.warmup()
    e.prime_from_current_state()
    shorts = [p for p in e.broker.open_positions() if p.short and p.direction == SHORT_CALL]
    assert shorts, "a falling Span B should open a short CALL at startup"
