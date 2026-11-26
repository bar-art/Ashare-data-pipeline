from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from quant_agent.cli import main
from quant_agent.config import Settings
from quant_agent.data.cache import LocalCache
from quant_agent.data.providers import AKShareProvider, RequestGate, SyntheticProvider
from quant_agent.data.quality import markdown_report, quality_report
from quant_agent.data.schema import normalize, validate_request
from quant_agent.data.service import DataService

START, END = date(2024, 1, 2), date(2024, 1, 5)
CODE = "600000.SH"


@pytest.fixture
def provider():
    return SyntheticProvider()


@pytest.fixture
def service(tmp_path, provider):
    return DataService(provider, LocalCache(tmp_path))


def test_normalize_unknown_and_units(provider):
    frame = provider.fetch_daily_bars(CODE, START, END)
    assert str(frame.is_st.dtype) == "boolean"
    assert frame.is_st.isna().all()
    assert frame.adj_factor.isna().all()
    assert frame.volume.iloc[0] == 10000
    assert len(frame) == 4


@pytest.mark.parametrize(
    "code,start,end",
    [
        ("600000", START, END),
        ("000001.SH", START, END),
        ("../x.SH", START, END),
        (CODE, END, START),
        (CODE, START, date.today()),
    ],
)
def test_invalid_request(code, start, end):
    with pytest.raises(ValueError):
        validate_request(code, start, end)


def test_invalid_schema(provider):
    frame = provider.fetch_daily_bars(CODE, START, END)
    for bad in [
        frame.drop(columns="close"),
        pd.concat([frame, frame]),
        frame.assign(ts_code="000001.SZ"),
        frame.assign(trade_date=None),
    ]:
        with pytest.raises(ValueError):
            normalize(bad, CODE)


def test_incremental_cache_and_refresh(service, provider, monkeypatch):
    fetch = Mock(wraps=provider.fetch_daily_bars)
    monkeypatch.setattr(provider, "fetch_daily_bars", fetch)
    service.get_daily_bars(CODE, START, date(2024, 1, 3))
    assert len(service.get_daily_bars(CODE, START, END)) == 4
    assert fetch.call_args.args[1:] == (date(2024, 1, 4), END)
    assert fetch.call_count == 2
    assert len(service.get_daily_bars(CODE, date(2024, 1, 3), date(2024, 1, 4))) == 2
    assert fetch.call_count == 2
    service.get_daily_bars(CODE, START, END, refresh=True)
    assert fetch.call_count == 3
    assert len(service.cache.provenance("synthetic", "bars", CODE)) == 3


def test_cache_gaps_and_isolation(tmp_path, provider):
    cache = LocalCache(tmp_path)
    frame = provider.fetch_daily_bars(CODE, START, END)
    cache.put("synthetic", "bars", CODE, date(2024, 1, 3), date(2024, 1, 4), frame, "test")
    assert cache.gaps("synthetic", CODE, START, END) == [(START, START), (END, END)]
    assert cache.gaps("akshare", CODE, START, END) == [(START, END)]
    assert cache.read("other", "bars", CODE) is None
    cache.export_manifest(tmp_path / "manifest.json", "synthetic", CODE)
    assert '"sha256"' in (tmp_path / "manifest.json").read_text()
    entry = cache.entries("synthetic", "bars", CODE)[0]
    Path(str(entry[2])).write_bytes(b"damaged")
    with pytest.raises(ValueError, match="校验"):
        cache.read("synthetic", "bars", CODE)


def test_empty_outside_and_wrong_code_not_cached(service, provider, monkeypatch):
    original = provider.fetch_daily_bars(CODE, START, END)
    for bad in [
        original.iloc[:0],
        original.assign(trade_date=pd.Timestamp("2020-01-01")),
        original.assign(ts_code="000001.SZ"),
    ]:
        monkeypatch.setattr(provider, "fetch_daily_bars", lambda *args, bad=bad: bad)
        with pytest.raises(ValueError):
            service.get_daily_bars(CODE, START, END)
        assert service.cache.entries("synthetic", "bars", CODE) == []


def test_calendar_and_instruments_cached(service, provider, monkeypatch):
    for method in ["calendar", "instruments"]:
        fetch = Mock(wraps=getattr(provider, f"fetch_{method}"))
        monkeypatch.setattr(provider, f"fetch_{method}", fetch)
        getter = getattr(service, f"get_{method}")
        first = getter()
        pd.testing.assert_frame_equal(first, getter())
        assert fetch.call_count == 1
        getter(refresh=True)
        assert fetch.call_count == 2
    assert "synthetic_only" in service.get_instruments().coverage.tolist()


def test_invalid_snapshots(service, provider, monkeypatch):
    monkeypatch.setattr(provider, "fetch_calendar", lambda: pd.DataFrame({"trade_date": []}))
    with pytest.raises(ValueError):
        service.get_calendar()
    monkeypatch.setattr(provider, "fetch_instruments", lambda: pd.DataFrame({"x": [1]}))
    with pytest.raises(ValueError):
        service.get_instruments()


def test_quality_diagnostics(provider):
    frame = provider.fetch_daily_bars(CODE, START, END).iloc[:3].copy()
    frame.loc[0, "open"] = 0
    frame.loc[1, "high"] = 1
    frame.loc[2, "volume"] = -1
    frame["adj_factor"] = [1.0, 2.0, 2.0]
    frame["up_limit"] = 10.0
    frame["down_limit"] = 9.5
    report = quality_report(frame, provider.fetch_calendar(), date(2024, 1, 1), END)
    kinds = {issue["kind"] for issue in report["issues"]}
    assert {
        "invalid_prices",
        "invalid_ohlc",
        "invalid_volume",
        "missing_sessions",
        "calendar_coverage",
        "limit_breach",
        "factor_jump",
        "unknown_is_st",
    } <= kinds
    assert report["full_market_coverage"] is False
    assert "仅供研究" in markdown_report(report)


def test_request_retry_and_limit():
    waits = []
    action = Mock(side_effect=[OSError("network"), 42])
    gate = RequestGate(interval=1, attempts=2, sleep=waits.append, clock=lambda: 0.0)
    assert gate.call(action) == 42
    assert waits == [1, 1]
    with pytest.raises(OSError):
        gate.call(Mock(side_effect=OSError()))
    invalid = Mock(side_effect=ValueError("schema"))
    with pytest.raises(ValueError):
        gate.call(invalid)
    assert invalid.call_count == 1


def test_akshare_mapping_and_endpoint(monkeypatch):
    raw = pd.DataFrame(
        {
            "日期": ["2024-01-02"],
            "开盘": [10.0],
            "最高": [11.0],
            "最低": [9.0],
            "收盘": [10.5],
            "成交量": [100.0],
            "成交额": [105000.0],
        }
    )
    api = SimpleNamespace(
        stock_zh_a_hist_em=Mock(return_value=raw),
        calendar_trade_date_hist_sina=Mock(return_value=pd.DataFrame({"trade_date": [START]})),
        stock_info_a_code_name=Mock(return_value=pd.DataFrame({"code": ["600000"], "name": ["A"]})),
    )
    adapter = AKShareProvider(RequestGate(interval=0))
    monkeypatch.setattr(adapter, "_api", lambda: api)
    result = adapter.fetch_daily_bars(CODE, START, END)
    assert result.volume.iloc[0] == 10000
    assert result.pre_close.isna().all()
    assert api.stock_zh_a_hist_em.call_args.kwargs["adjust"] == ""
    assert not adapter.fetch_calendar().empty
    assert "not_historical" in adapter.fetch_instruments().coverage.iloc[0]
    # 兼容旧版接口，仍使用同一来源。
    api.stock_zh_a_hist = api.stock_zh_a_hist_em
    api.tool_trade_date_hist_sina = api.calendar_trade_date_hist_sina
    del api.stock_zh_a_hist_em
    del api.calendar_trade_date_hist_sina
    assert len(adapter.fetch_daily_bars(CODE, START, END)) == 1
    assert len(adapter.fetch_calendar()) == 1


def test_cli_all_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "cache"))
    output = tmp_path / "reports"
    for command in ["fetch", "calendar", "instruments"]:
        assert main([command, "--provider", "synthetic", "--output", str(output)]) == 0
    assert len(list(output.glob("*quality.json"))) == 1
    assert main(["fetch", "--provider", "synthetic", "--code", "invalid"]) == 1
    assert "执行失败" in capsys.readouterr().out


def test_settings_secret_and_bounds():
    settings = Settings(tushare_token="not-a-real-key", _env_file=None)
    assert "not-a-real-key" not in repr(settings)
    with pytest.raises(ValueError):
        Settings(request_attempts=0, _env_file=None)


def test_rate_limit_is_not_retried():
    class RateLimitError(OSError):
        pass

    action = Mock(side_effect=RateLimitError())
    with pytest.raises(RateLimitError):
        RequestGate(attempts=3).call(action)
    assert action.call_count == 1


def test_akshare_import_timeout(monkeypatch):
    api = SimpleNamespace(set_timeout=Mock())
    monkeypatch.setattr("quant_agent.data.providers.importlib.import_module", lambda name: api)
    assert AKShareProvider(timeout=5)._api() is api
    api.set_timeout.assert_called_once_with(5)
