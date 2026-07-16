from onlyichu.broker import PaperBroker
from onlyichu.config import Config


class FakeAPI:
    def __init__(self, prices):
        self.prices = prices

    def ltp_single(self, key):
        return self.prices.get(key)


def make_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.paper_slippage_pct = 0.0
    cfg.paper_starting_cash = 100000.0
    cfg.paper_state_file = str(tmp_path / "paper_state.json")
    cfg.paper_trade_log = str(tmp_path / "trades.csv")
    return cfg


def test_paper_option_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|123": 100.0})
    broker = PaperBroker(cfg, api)

    pos = broker.enter("NIFTY:1m", "NSE_FO|123", "NIFTY CE", 75, "LONG", None, kind="OPTION")
    assert pos is not None and pos.entry_price == 100.0
    assert broker.state.cash == 100000.0 - 100.0 * 75
    assert broker.position_side("NIFTY:1m") == "LONG"

    api.prices["NSE_FO|123"] = 110.0
    pnl = broker.exit("NIFTY:1m", price_hint=None)
    assert pnl == (110.0 - 100.0) * 75
    assert broker.state.cash == 100000.0 + 750.0
    assert broker.position("NIFTY:1m") is None
    assert broker.realized_pnl_today() == 750.0


def test_paper_synthetic_short(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_INDEX|X": 1000.0})
    broker = PaperBroker(cfg, api)
    broker.enter("SMALLCAP:5m", "NSE_INDEX|X", "SMALLCAP", 1, "SHORT", None, kind="SYNTHETIC")
    api.prices["NSE_INDEX|X"] = 990.0
    pnl = broker.exit("SMALLCAP:5m", price_hint=None)
    assert pnl == 10.0  # short profits when index falls


def test_paper_state_persists(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|123": 50.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:5m", "NSE_FO|123", "NIFTY CE", 75, "LONG", None)

    resumed = PaperBroker(cfg, api)
    pos = resumed.position("NIFTY:5m")
    assert pos is not None and pos.qty == 75 and pos.entry_price == 50.0


def test_paper_rejects_double_entry_and_overspend(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.paper_starting_cash = 100.0
    api = FakeAPI({"NSE_FO|123": 10.0})
    broker = PaperBroker(cfg, api)
    assert broker.enter("A:1m", "NSE_FO|123", "X", 5, "LONG", None) is not None
    assert broker.enter("A:1m", "NSE_FO|123", "X", 5, "LONG", None) is None  # already holding
    assert broker.enter("B:1m", "NSE_FO|123", "X", 50, "LONG", None) is None  # 500 > cash left
