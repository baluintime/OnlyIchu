"""Trading charges: estimated brokerage/STT/txn/SEBI/stamp/GST deducted so P&L
is NET (Gross − charges), matching what Upstox reports."""

from onlyichu.charges import (
    ChargesConfig, buy_charges, round_trip_charges, sell_charges,
)
from onlyichu.config import Config, load_config
from onlyichu.broker import PaperBroker, Position


class FakeAPI:
    def __init__(self, prices):
        self.prices = prices

    def ltp_single(self, key):
        return self.prices.get(key)

    def ltp(self, keys):
        return {k: self.prices[k] for k in keys if k in self.prices}


def make_cfg(tmp_path, charges=True) -> Config:
    cfg = Config()
    cfg.paper_slippage_pct = 0.0
    cfg.paper_starting_cash = 1_000_000.0
    cfg.paper_state_file = str(tmp_path / "paper_state.json")
    cfg.paper_trade_log = str(tmp_path / "trades.csv")
    cfg.apply_charges = charges
    return cfg


# --------------------------------------------------------------- calculator

def test_disabled_charges_are_zero():
    cfg = ChargesConfig(enabled=False)
    assert buy_charges(cfg, 100.0, 75) == 0.0
    assert sell_charges(cfg, 100.0, 75) == 0.0
    assert round_trip_charges(cfg, 100.0, 110.0, 75) == 0.0


def test_gst_identity_and_components():
    cfg = ChargesConfig(enabled=True)
    price, qty = 300.0, 75
    turnover = price * qty
    txn = cfg.exchange_txn_pct * turnover
    sebi = cfg.sebi_pct * turnover
    # GST is 18% of (brokerage + exchange txn + SEBI); confirm the total decomposes
    expected_gst = cfg.gst_pct * (cfg.brokerage_per_order + txn + sebi)
    # sell charges = brokerage + txn + sebi + stt + gst
    stt = cfg.stt_sell_pct * turnover
    expected = round(cfg.brokerage_per_order + txn + sebi + stt + expected_gst, 2)
    assert sell_charges(cfg, price, qty) == expected


def test_stt_only_on_sell_stamp_only_on_buy():
    cfg = ChargesConfig(enabled=True)
    price, qty = 200.0, 60
    turnover = price * qty
    # difference between sell and buy legs is exactly (STT) − (stamp duty)
    diff = sell_charges(cfg, price, qty) - buy_charges(cfg, price, qty)
    assert round(diff, 2) == round(cfg.stt_sell_pct * turnover - cfg.stamp_buy_pct * turnover, 2)


def test_brokerage_percent_cap():
    # a tiny turnover: percent-of-turnover brokerage is lower than the flat fee
    cfg = ChargesConfig(enabled=True, brokerage_pct=0.0003)
    # turnover 1000 -> pct brokerage 0.30 << flat 20
    c = buy_charges(cfg, 10.0, 100)
    flat = buy_charges(ChargesConfig(enabled=True), 10.0, 100)
    assert c < flat


# --------------------------------------------------------------- broker wiring

def test_exit_records_net_pnl_and_charges(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|1": 100.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY CE", 75, "LONG", None)
    api.prices["NSE_FO|1"] = 110.0
    pnl = broker.exit("NIFTY:1m", price_hint=None)

    rt = round_trip_charges(broker.charges, 100.0, 110.0, 75)
    assert rt > 0
    assert pnl == 750.0 - rt                       # net, not gross 750
    assert broker.realized_pnl_today() == 750.0 - rt
    assert round(broker.state.charges_today, 2) == round(rt, 2)


def test_charges_off_reports_gross(tmp_path):
    cfg = make_cfg(tmp_path, charges=False)
    api = FakeAPI({"NSE_FO|1": 100.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY CE", 75, "LONG", None)
    api.prices["NSE_FO|1"] = 110.0
    assert broker.exit("NIFTY:1m", price_hint=None) == 750.0     # gross
    assert broker.state.charges_today == 0.0


def test_zero_entry_price_no_charges(tmp_path):
    # a badly-adopted orphan (entry 0) must not fabricate PnL OR charges
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|9": 1433.75})
    broker = PaperBroker(cfg, api)
    broker.state.positions["X:1m"] = Position(
        "X:1m", "NSE_FO|9", "BANKNIFTY 58300 PE", 30, 0.0, "t", "SHORT")
    assert broker.exit("X:1m", price_hint=1433.75) == 0.0
    assert broker.state.charges_today == 0.0


def test_partial_exit_nets_charges(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|1": 100.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY CE", 120, "LONG", None, lot_size=60)
    api.prices["NSE_FO|1"] = 120.0
    pnl = broker.partial_exit("NIFTY:1m", 0.5, price_hint=120.0)
    rt = round_trip_charges(broker.charges, 100.0, 120.0, 60)
    assert pnl == (120.0 - 100.0) * 60 - rt
    assert round(broker.state.charges_today, 2) == round(rt, 2)


def test_mark_to_market_is_net_of_estimated_exit_charges(tmp_path):
    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|1": 100.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY CE", 75, "LONG", None)
    api.prices["NSE_FO|1"] = 110.0
    unreal, _ = broker.mark_to_market()
    rt = round_trip_charges(broker.charges, 100.0, 110.0, 75)
    assert unreal == 750.0 - rt


def test_trade_log_has_charges_column(tmp_path):
    import csv

    cfg = make_cfg(tmp_path)
    api = FakeAPI({"NSE_FO|1": 100.0})
    broker = PaperBroker(cfg, api)
    broker.enter("NIFTY:1m", "NSE_FO|1", "NIFTY CE", 75, "LONG", None)
    api.prices["NSE_FO|1"] = 110.0
    broker.exit("NIFTY:1m", price_hint=None)
    with open(cfg.paper_trade_log, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert "charges" in rows[0]
    assert float(rows[1]["charges"]) > 0        # exit row carries the charges


def test_config_parses_charges_section(tmp_path):
    yml = tmp_path / "c.yaml"
    yml.write_text(
        "charges:\n"
        "  apply_charges: true\n"
        "  brokerage_per_order: 15\n"
        "  stt_sell_pct: 0.000625\n"
    )
    cfg = load_config(str(yml))
    assert cfg.apply_charges is True
    assert cfg.brokerage_per_order == 15.0
    assert cfg.stt_sell_pct == 0.000625
