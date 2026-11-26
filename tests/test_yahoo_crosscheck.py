import json
import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from test_baostock_export import bars, fake_baostock, long_calendar

from quant_agent.cli import main
from quant_agent.data.crosscheck import compare_yahoo, raw_from_yahoo, summarize_yahoo
from quant_agent.data.providers import BaoStockProvider, RequestGate
from quant_agent.data.rules import apply_price_limits
from quant_agent.data.yahoo_source import YahooSource


def yahoo_frame():
    """600518.SH 2019-05-17..22 的 Yahoo 形态；05-21 之后发生 2 倍拆股，之前价格被除以 2。"""
    index = pd.DatetimeIndex(
        ["2019-05-17", "2019-05-20", "2019-05-21", "2019-05-22"], tz="Asia/Shanghai", name="Date"
    )
    return pd.DataFrame(
        {
            "Open": [6.99 / 2, 6.64 / 2, 6.31 / 2, 6.00],
            "High": [7.00 / 2, 6.64 / 2, 6.31 / 2, 6.10],
            "Low": [6.64 / 2, 6.64 / 2, 6.31 / 2, 6.00],
            "Close": [6.64 / 2, 6.64 / 2, 6.31 / 2, 6.00],
            "Adj Close": [3.3, 3.3, 3.1, 6.0],
            "Volume": [100, 0, 200, 300],
            "Dividends": [0.0, 0.0, 0.1, 0.0],
            "Stock Splits": [0.0, 0.0, 0.0, 2.0],
        },
        index=index,
    )


def fake_yfinance(frame, calls):
    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **params):
            calls.append((self.symbol, params))
            return frame.copy()

    return SimpleNamespace(Ticker=Ticker)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock())
    return BaoStockProvider(RequestGate(interval=0, sleep=lambda _: None))


def test_yahoo_source_symbols_and_normalization(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance(yahoo_frame(), calls))
    source = YahooSource(RequestGate(interval=0, sleep=lambda _: None))
    assert source.symbol("600518.SH") == "600518.SS" and source.symbol("000001.SZ") == "000001.SZ"
    with pytest.raises(ValueError):
        source.symbol("830799.BJ")
    frame = source.history("600518.SH", date(2019, 5, 17), date(2019, 5, 22))
    assert calls[0] == (
        "600518.SS",
        {"start": "2019-05-17", "end": "2019-05-23", "auto_adjust": False, "actions": True},
    )
    assert frame.trade_date.dt.tz is None and frame.trade_date.iloc[0] == pd.Timestamp("2019-05-17")
    assert list(frame.columns)[:2] == ["trade_date", "open"]
    empty = fake_yfinance(yahoo_frame().iloc[:0], calls)
    monkeypatch.setitem(sys.modules, "yfinance", empty)
    assert source.history("600518.SH", date(2019, 5, 17), date(2019, 5, 22)).empty


def test_raw_from_yahoo_undoes_later_splits(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance(yahoo_frame(), []))
    frame = YahooSource(RequestGate(interval=0)).history(
        "600518.SH", date(2019, 5, 17), date(2019, 5, 22)
    )
    raw = raw_from_yahoo(frame)
    assert raw.close.tolist() == pytest.approx([6.64, 6.64, 6.31, 6.00])


def test_compare_yahoo_categories(provider, monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance(yahoo_frame(), []))
    yahoo = YahooSource(RequestGate(interval=0)).history(
        "600518.SH", date(2019, 5, 17), date(2019, 5, 22)
    )
    limited = apply_price_limits(bars(provider), long_calendar(provider), date(2001, 3, 19))
    report = compare_yahoo(limited, yahoo)
    assert report["prices"]["compared_rows"] == 3 and report["prices"]["mismatch_rows"] == 0
    assert report["suspension"]["agree"] == 1  # 05-20 双方都显示停牌/无成交
    assert report["limits"]["checked_rows"] == 3
    assert report["limits"]["yahoo_price_outside_rule_limits"] == 0
    # BaoStock 复权因子不变，Yahoo 有分红和拆股：两条单边记录。
    assert report["corporate_actions"]["only_yahoo_event"] == ["2019-05-21", "2019-05-22"]
    # 错价、Yahoo 前值填充、突破涨跌停都能发现。
    bad = yahoo.copy()
    bad.loc[0, "close"] = 3.50  # 还原后 7.00，与 6.64 不符
    bad.loc[3, ["volume", "high", "low", "open", "close"]] = [0, 6.31, 6.31, 6.31, 6.31]
    bad.loc[2, "high"] = 7.5 / 2
    worse = compare_yahoo(limited, bad)
    assert worse["prices"]["mismatch_rows"] == 1
    assert worse["suspension"]["only_yahoo"] == ["2019-05-22"]
    assert worse["limits"]["yahoo_price_outside_rule_limits"] == 1
    total = summarize_yahoo({"600518.SH": worse, "x": report})
    assert total["price_mismatch_rows"] == 1 and total["suspension_only_yahoo"] == 1
    assert compare_yahoo(limited, yahoo.iloc[:0])["status"] == "yahoo_empty"


def test_cli_crosscheck_yahoo_default(monkeypatch, tmp_path):
    calls = []
    early = [str(d.date()) for d in pd.bdate_range("2001-03-19", "2019-05-16")]
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock(early_days=early))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance(yahoo_frame(), calls))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REQUEST_INTERVAL", "0")
    args = ["crosscheck", "--provider", "baostock", "--codes", "600518.SH"]
    args += ["--start", "2019-05-17", "--end", "2019-05-22"]
    assert main([*args, "--output", str(tmp_path / "y1")]) == 0
    report = json.loads((tmp_path / "y1" / "crosscheck_report.json").read_text())
    assert report["check_provider"] == "yahoo"
    assert report["summary"]["price_mismatch_rows"] == 0
    assert main([*args, "--output", str(tmp_path / "y2")]) == 0
    assert len(calls) == 1  # 第二次命中缓存
