"""Self-healing reconciliation: adopt an orphan the strategy can still hold,
square off one it can't, and drop phantoms Upstox has already closed."""

from datetime import datetime, timedelta, timezone

from onlyichu.broker import Position
from onlyichu.candles import Candle
from onlyichu.config import Config, IndexConfig
from onlyichu.engine import Engine

IST = timezone(timedelta(hours=5, minutes=30))


class FakeAPI:
    access_token = "x"
    has_token = True

    def __init__(self, positions):
        self._positions = positions
        self.placed = []
        self.n = 0
        self.status = {}
        self.avg = {}

    def positions(self):
        return [p for p in self._positions if p["quantity"] != 0]

    def ltp(self, keys):
        return {k: 100.0 for k in keys}

    def ltp_single(self, k):
        return 100.0

    def place_order(self, instrument_key, quantity, transaction_type, order_type="LIMIT",
                    price=0.0, product="I", tag="onlyichu"):
        self.n += 1
        oid = f"O{self.n}"
        self.placed.append((transaction_type, quantity, instrument_key))
        if transaction_type == "SELL":  # reflect the close on the Upstox side
            for p in self._positions:
                if p["instrument_token"] == instrument_key:
                    p["quantity"] -= quantity
        self.status[oid] = "complete"
        self.avg[oid] = price or 100.0
        return oid

    def order_details(self, oid):
        return {"status": self.status.get(oid, "open"), "average_price": self.avg.get(oid)}

    def cancel_order(self, oid):
        self.status[oid] = "cancelled"


def upos(token, qty, avg, symbol):
    return {"instrument_token": token, "quantity": qty, "average_price": avg, "tradingsymbol": symbol}


def make_engine(tmp_path, positions):
    c = Config()
    c.mode = "live"
    c.live_trade_log = str(tmp_path / "live.csv")
    c.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]
    e = Engine(c, FakeAPI(positions))
    e.broker.FILL_POLL_SECONDS = 2
    return e


def warm(engine, up=True):
    n = engine.params.min_candles + 6
    t0 = datetime(2026, 7, 29, 9, 15, tzinfo=IST)
    step = 1.0 if up else -1.0
    price = 100.0 if up else 400.0
    candles = []
    for i in range(n):
        candles.append(Candle(t0 + timedelta(minutes=i), price, price + 0.5, price - 0.5, price + step, 1))
        price += step
    for p in engine.runners[0].pipelines.values():
        p.warmup(candles)


def test_orphan_kept_when_signal_still_valid(tmp_path):
    e = make_engine(tmp_path, [upos("NSE_FO|1", 65, 240.0, "NIFTY 24050 CE")])
    warm(e, up=True)  # bullish -> a CE (long) is still a valid trade -> adopt & keep
    assert e.reconcile_positions() is True
    held = [p for p in e.broker.open_positions() if p.instrument_key == "NSE_FO|1"]
    assert held and held[0].qty == 65 and held[0].direction == "LONG"
    assert e.api.placed == []  # kept, nothing sold


def test_orphan_squared_when_signal_invalid(tmp_path):
    e = make_engine(tmp_path, [upos("NSE_FO|1", 65, 240.0, "NIFTY 24050 CE")])
    warm(e, up=False)  # bearish -> a CE should be exited -> square it off
    assert e.reconcile_positions() is True  # squared off -> Upstox flat -> in sync
    assert e.api.placed and e.api.placed[0][0] == "SELL" and e.api.placed[0][1] == 65
    assert e.broker.open_positions() == []


def test_phantom_dropped_when_upstox_flat(tmp_path):
    e = make_engine(tmp_path, [])  # Upstox holds nothing
    e.broker.state.positions["NIFTY:1m"] = Position(
        "NIFTY:1m", "NSE_FO|1", "NIFTY 24050 CE", 65, 240.0, "t", "LONG")
    assert e.reconcile_positions() is True  # phantom dropped -> in sync
    assert e.broker.open_positions() == []
    assert e.api.placed == []  # nothing sold (already closed on Upstox)


def test_paper_mode_never_paused(tmp_path):
    c = Config()
    c.mode = "paper"
    c.paper_state_file = str(tmp_path / "s.json")
    c.paper_trade_log = str(tmp_path / "t.csv")
    c.instruments = [IndexConfig("NIFTY", "NSE_INDEX|Nifty 50", True, True)]

    class PaperAPI:
        access_token = "x"
        has_token = True

    e = Engine(c, PaperAPI())
    assert e.reconcile_positions() is True
