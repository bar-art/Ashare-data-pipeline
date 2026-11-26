import json
import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from test_baostock_export import bars, fake_baostock, long_calendar

from quant_agent.cli import main
from quant_agent.data.crosscheck import compare_code, summarize
from quant_agent.data.providers import BaoStockProvider, RequestGate
from quant_agent.data.rules import apply_price_limits
from quant_agent.data.tushare_source import TushareSource

DAILY = pd.DataFrame(
    {  # Tushare daily 不含全天停牌日 2019-05-20
        "ts_code": "600518.SH",
        "trade_date": ["20190522", "20190521", "20190517"],
        "open": [6.00, 6.31, 6.99],
        "high": [6.10, 6.31, 7.00],
        "low": [6.00, 6.31, 6.64],
        "close": [6.00, 6.31, 6.64],
        "pre_close": [6.31, 6.64, 6.99],
    }
)
LIMITS = pd.DataFrame(
    {
        "trade_date": ["20190517", "20190521", "20190522"],
        "ts_code": "600518.SH",
        "up_limit": [7.69, 6.97, 6.62],  # 05-22 故意与规则值 6.63 不同
        "down_limit": [6.29, 6.31, 5.99],
    }
)
SUSPEND = pd.DataFrame(
    {
        "ts_code": "600518.SH",
        "trade_date": ["20190520", "20190522"],
        "suspend_timing": [None, "09:30-10:30"],  # 日内停牌不算全天停牌
        "suspend_type": ["S", "S"],
    }
)


def fake_tushare(calls):
    class Pro:
        def __getattr__(self, name):
            def method(**params):
                calls.append((name, params, self.__dict__.get("_DataApi__http_url")))
                return {"daily": DAILY, "stk_limit": LIMITS, "suspend_d": SUSPEND}[name].copy()

            return method

    return SimpleNamespace(pro_api=lambda token: Pro())


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock())
    return BaoStockProvider(RequestGate(interval=0, sleep=lambda _: None))


def test_source_labels_official_and_proxy(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare(calls))
    gate = RequestGate(interval=0, sleep=lambda _: None)
    official = TushareSource("t" * 56, "", gate)
    assert official.name == "tushare" and "官方" in official.source
    official.daily("600518.SH", date(2019, 5, 17), date(2019, 5, 22))
    assert calls[-1][2] is None
    assert calls[-1][1] == {
        "ts_code": "600518.SH",
        "start_date": "20190517",
        "end_date": "20190522",
    }
    proxy = TushareSource("t" * 56, "http://proxy.example.com:24145", gate)
    assert proxy.name == "tushare" and "第三方转发" in proxy.source
    assert "proxy.example.com" not in proxy.source
    proxy.stk_limit("600518.SH", date(2019, 5, 17), date(2019, 5, 22))
    assert calls[-1][2] == "http://proxy.example.com:24145"
    for bad in ["ftp://x.com", "http://user:pw@x.com", "http://x.com/?k=1"]:
        with pytest.raises(ValueError):
            TushareSource("t", bad)
    with pytest.raises(ValueError, match="TUSHARE_TOKEN"):
        TushareSource("", "")


def test_compare_reports_each_category(provider):
    limited = apply_price_limits(bars(provider), long_calendar(provider), date(2001, 3, 19))
    report = compare_code(limited, DAILY, LIMITS, SUSPEND)
    assert report["prices"]["matched_rows"] == 3 and report["prices"]["mismatch_rows"] == 0
    assert report["prices"]["baostock_trading_rows_missing_in_tushare"] == 0  # 缺的只是停牌日
    assert report["limits"]["compared_rows"] == 3
    assert report["limits"]["mismatch_rows"] == 1
    assert report["limits"]["mismatch_samples"][0]["trade_date"] == "2019-05-22"
    assert report["limits"]["rule_known_but_tushare_missing"] == 1  # 停牌日 05-20
    assert report["suspension"]["agree"] == 1 and report["suspension"]["only_tushare_count"] == 0
    total = summarize({"600518.SH": report})
    assert total["limit_mismatch_rows"] == 1 and total["suspension_disagree"] == 0


def test_compare_flags_price_and_suspension_disagreement(provider):
    limited = apply_price_limits(bars(provider), long_calendar(provider), date(2001, 3, 19))
    daily = DAILY.copy()
    daily.loc[0, "close"] = 6.05
    suspend = SUSPEND.assign(trade_date=["20190521", "20190522"])
    report = compare_code(limited, daily, pd.DataFrame(), suspend)
    assert report["prices"]["mismatch_rows"] == 1
    assert report["limits"] == {"status": "tushare_stk_limit_empty"}
    assert report["suspension"]["only_baostock"] == ["2019-05-20"]
    assert report["suspension"]["only_tushare"] == ["2019-05-21"]
    empty = compare_code(limited, pd.DataFrame(), LIMITS, pd.DataFrame())
    assert empty["prices"] == {"status": "tushare_daily_empty"}
    assert empty["suspension"]["only_baostock_count"] == 1


def test_cli_crosscheck_caches_and_records_host(monkeypatch, tmp_path):
    calls = []
    early = [str(d.date()) for d in pd.bdate_range("2001-03-19", "2019-05-16")]
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock(early_days=early))
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare(calls))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REQUEST_INTERVAL", "0")
    monkeypatch.setenv("TUSHARE_TOKEN", "t" * 56)
    monkeypatch.setenv("TUSHARE_HTTP_URL", "http://proxy.example.com:24145")
    args = ["crosscheck", "--provider", "baostock", "--check-source", "tushare"]
    args += ["--codes", "600518.SH"]
    args += ["--start", "2019-05-17", "--end", "2019-05-22"]
    assert main([*args, "--output", str(tmp_path / "c1")]) == 0
    report = json.loads((tmp_path / "c1" / "crosscheck_report.json").read_text())
    assert report["check_provider"] == "tushare"
    assert report["summary"]["limit_mismatch_rows"] == 1
    assert len(calls) == 3
    # 第二次命中缓存，不再请求 Tushare；token 不出现在报告中。
    assert main([*args, "--output", str(tmp_path / "c2")]) == 0
    assert len(calls) == 3
    assert "t" * 56 not in (tmp_path / "c2" / "crosscheck_report.json").read_text()
    assert main([*args, "--output", str(tmp_path / "c2")]) == 1  # 非空目录
    assert main(["crosscheck", "--provider", "akshare", "--output", str(tmp_path / "c3")]) == 1
