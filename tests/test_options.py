"""ITM option selection: delta band + liquidity guards."""

from onlyichu.config import Config
from onlyichu.options import OptionSelector


def leg(key, delta, ltp=100.0, oi=100000, bid=99.5, ask=100.5):
    md = {"ltp": ltp}
    if oi is not None:
        md["oi"] = oi
    if bid is not None:
        md["bid_price"] = bid
    if ask is not None:
        md["ask_price"] = ask
    return {
        "instrument_key": key,
        "trading_symbol": key,
        "market_data": md,
        "option_greeks": ({"delta": delta} if delta is not None else {}),
    }


class ChainAPI:
    """Serves a CE chain around spot 25000 (strikes 24800..25200 step 100)."""

    def __init__(self, rows):
        self._rows = rows

    def option_contracts(self, key):
        return [{"expiry": "2026-07-24", "lot_size": 75}]

    def option_chain(self, key, expiry):
        return self._rows


def cfg_with(**kw):
    cfg = Config()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def make(rows, **cfgkw):
    return OptionSelector(ChainAPI(rows), cfg_with(**cfgkw))


# ITM calls are strikes below spot=25000. deltas: 24800->0.72, 24900->0.68
GOOD_ROWS = [
    {"strike_price": 24800, "call_options": leg("CE24800", 0.72)},
    {"strike_price": 24900, "call_options": leg("CE24900", 0.68)},
    {"strike_price": 25100, "call_options": leg("CE25100", 0.40)},  # OTM, ignored for CE
]


def test_picks_delta_closest_to_target():
    sel = make(GOOD_ROWS).select_itm("NSE_INDEX|X", "LONG", 25000)
    assert sel is not None
    # target 0.70: 24800 (0.72, dist .02) vs 24900 (0.68, dist .02) — tie, first wins
    assert sel.strike in (24800, 24900)
    assert sel.option_type == "CE"


def test_skips_wide_spread_strike():
    rows = [
        {"strike_price": 24800, "call_options": leg("CE24800", 0.72, bid=90, ask=110)},  # 20% spread
        {"strike_price": 24900, "call_options": leg("CE24900", 0.68, bid=99.5, ask=100.5)},  # ~1%
    ]
    sel = make(rows, max_spread_pct=5.0).select_itm("NSE_INDEX|X", "LONG", 25000)
    assert sel is not None and sel.strike == 24900  # wide one skipped


def test_skips_low_oi_strike():
    rows = [
        {"strike_price": 24800, "call_options": leg("CE24800", 0.72, oi=50)},
        {"strike_price": 24900, "call_options": leg("CE24900", 0.68, oi=500000)},
    ]
    sel = make(rows, min_open_interest=1000).select_itm("NSE_INDEX|X", "LONG", 25000)
    assert sel is not None and sel.strike == 24900


def test_returns_none_when_all_illiquid():
    rows = [
        {"strike_price": 24800, "call_options": leg("CE24800", 0.72, bid=0, ask=0)},
        {"strike_price": 24900, "call_options": leg("CE24900", 0.68, bid=0, ask=0)},
    ]
    sel = make(rows, max_spread_pct=5.0).select_itm("NSE_INDEX|X", "LONG", 25000)
    assert sel is None  # nothing tradeable -> skip the trade


def test_missing_quote_data_not_blocked():
    # bid/ask absent entirely -> can't evaluate spread -> must still trade
    rows = [
        {"strike_price": 24800, "call_options": leg("CE24800", 0.72, bid=None, ask=None)},
        {"strike_price": 24900, "call_options": leg("CE24900", 0.68, bid=None, ask=None)},
    ]
    sel = make(rows, max_spread_pct=5.0).select_itm("NSE_INDEX|X", "LONG", 25000)
    assert sel is not None


def test_guards_disabled_by_default():
    # even a terrible strike is accepted when both guards are 0 (default Config)
    rows = [{"strike_price": 24800, "call_options": leg("CE24800", 0.70, oi=1, bid=0, ask=0)}]
    sel = make(rows).select_itm("NSE_INDEX|X", "LONG", 25000)
    assert sel is not None and sel.strike == 24800


def test_put_selection_for_short():
    # SHORT -> PE; ITM puts are strikes ABOVE spot
    rows = [
        {"strike_price": 25100, "put_options": leg("PE25100", -0.72)},
        {"strike_price": 25200, "put_options": leg("PE25200", -0.68)},
    ]
    sel = make(rows, max_spread_pct=5.0).select_itm("NSE_INDEX|X", "SHORT", 25000)
    assert sel is not None and sel.option_type == "PE"
    assert sel.strike in (25100, 25200)
    assert sel.spread_pct is not None and sel.spread_pct < 5.0
