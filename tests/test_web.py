from datetime import datetime, timedelta, timezone

from onlyichu.candles import Candle
from onlyichu.config import Config, IndexConfig
from onlyichu.ichimoku import IchimokuParams
from onlyichu.web import DashboardService, analyze_series, create_app

IST = timezone(timedelta(hours=5, minutes=30))
PARAMS = IchimokuParams(tenkan=2, kijun=3, senkou_b=4, displacement=2)


def trending(n, start, step, t0=None):
    t0 = t0 or datetime(2026, 7, 16, 9, 15, tzinfo=IST)
    out, p = [], start
    for i in range(n):
        out.append(Candle(t0 + timedelta(minutes=i), p, p + 0.5, p - 0.5, p + step, 1))
        p += step
    return out


def test_analyze_series_uptrend_long():
    a = analyze_series(trending(12, 100.0, 1.0), PARAMS)
    assert a["ready"] is True
    assert a["signal"] == "LONG"
    assert a["zone"] == "ABOVE CLOUD"
    assert len(a["levels"]) == 4
    assert all(lv["above"] for lv in a["levels"])
    assert a["cloud"]["top"] >= a["cloud"]["bottom"]


def test_analyze_series_downtrend_short():
    a = analyze_series(trending(12, 100.0, -1.0), PARAMS)
    assert a["signal"] == "SHORT"
    assert a["zone"] == "BELOW CLOUD"
    assert all(lv["below"] for lv in a["levels"])


def test_analyze_series_warmup():
    a = analyze_series(trending(3, 100.0, 1.0), PARAMS)
    assert a["ready"] is False
    assert a["needed"] == PARAMS.min_candles
    assert analyze_series([], PARAMS) is None


class FakeAPI:
    """Serves a long uptrend split across yesterday (historical) and today (intraday)."""

    def __init__(self):
        yday = datetime(2026, 7, 15, 9, 15, tzinfo=IST)
        today = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
        self._hist = trending(100, 1000.0, 0.5, t0=yday)
        # far in the past relative to "now" so every candle counts as complete
        self._intra = trending(60, 1050.0, 1.0, t0=today - timedelta(days=0, hours=9))

    @staticmethod
    def _rows(candles):
        return [[c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume, 0] for c in reversed(candles)]

    def historical_candles(self, key, to_date, from_date, unit="1minute"):
        return self._rows(self._hist)

    def intraday_candles(self, key, unit="1minute"):
        return self._rows(self._intra)

    def ltp(self, keys):
        return {k: 1111.0 for k in keys}


def make_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.paper_state_file = str(tmp_path / "nope.json")
    cfg.instruments = [IndexConfig(name="FAKE", key="NSE_INDEX|Fake", options_available=False)]
    return cfg


def test_dashboard_payload(tmp_path):
    svc = DashboardService(make_cfg(tmp_path), FakeAPI())
    payload = svc.payload()
    assert payload["market"]["status"] in ("OPEN", "CLOSED", "PRE-OPEN")
    assert payload["error"] is None
    (ix,) = payload["indices"]
    assert ix["name"] == "FAKE" and ix["ltp"] == 1111.0
    assert [p["timeframe"] for p in ix["pipelines"]] == ["1m", "5m"]
    one_m = ix["pipelines"][0]
    assert one_m["ready"] and one_m["signal"] == "LONG"
    assert payload["paper"] is None
    # cached within TTL: same object returned
    assert svc.payload() is payload


def test_flask_routes(tmp_path):
    app = create_app(make_cfg(tmp_path), FakeAPI())
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"OnlyIchu" in page.data
    api = client.get("/api/dashboard")
    assert api.status_code == 200
    body = api.get_json()
    assert body["indices"][0]["pipelines"][0]["signal"] == "LONG"
