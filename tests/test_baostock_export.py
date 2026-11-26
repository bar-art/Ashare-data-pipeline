import json
import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from quant_agent.cli import main
from quant_agent.data.export import to_alpha_evidence_lab, to_quant_research_lab
from quant_agent.data.providers import BaoStockProvider, RequestGate
from quant_agent.data.quality import quality_report
from quant_agent.data.rules import apply_price_limits, limit_price, limit_ratio

# 600518.SH 2019-05-17..23 的结构：停牌日价格为停牌前收盘、成交量为空；05-21 起为 ST。
RAW = [
    ["2019-05-17", "sh.600518", "6.99", "7.00", "6.64", "6.64", "6.99", "100", "700", "1", "0"],
    ["2019-05-20", "sh.600518", "6.64", "6.64", "6.64", "6.64", "6.64", "", "", "0", "0"],
    ["2019-05-21", "sh.600518", "6.31", "6.31", "6.31", "6.31", "6.64", "200", "1262", "1", "1"],
    ["2019-05-22", "sh.600518", "6.00", "6.10", "6.00", "6.00", "6.31", "300", "1800", "1", "1"],
]
FIELDS = BaoStockProvider.FIELDS.split(",")


class Result:
    def __init__(self, rows, fields, error_code="0"):
        self.rows, self.fields, self.error_code = list(rows), fields, error_code

    def next(self):
        return bool(self.rows)

    def get_row_data(self):
        return self.rows.pop(0)


def fake_baostock(login_code="0", early_days=()):
    def history(code, fields, start_date, end_date, frequency, adjustflag):
        rows = [r for r in RAW if r[1] == code and start_date <= r[0] <= end_date]
        if adjustflag == "1":
            rows = [[*r[:5], f"{float(r[5]) * 2:.4f}", *r[6:]] for r in rows]
        return Result(rows, FIELDS)

    return SimpleNamespace(
        login=lambda: SimpleNamespace(error_code=login_code),
        logout=lambda: None,
        query_history_k_data_plus=history,
        query_trade_dates=lambda start_date, end_date: Result(
            [[d, "1"] for d in early_days] + [[r[0], "1"] for r in RAW] + [["2019-05-18", "0"]],
            ["calendar_date", "is_trading_day"],
        ),
        query_stock_basic=lambda: Result(
            [["sh.600518", "样本", "2001-03-19", "", "1", "1"]],
            ["code", "code_name", "ipoDate", "outDate", "type", "status"],
        ),
    )


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock())
    return BaoStockProvider(RequestGate(interval=0, sleep=lambda _: None))


def bars(provider):
    return provider.fetch_daily_bars("600518.SH", date(2019, 5, 17), date(2019, 5, 22))


def test_baostock_fields_from_source_not_inferred(provider):
    frame = bars(provider)
    assert frame.is_suspended.tolist() == [False, True, False, False]
    assert frame.is_st.tolist() == [False, False, True, True]
    assert frame.pre_close.tolist() == [6.99, 6.64, 6.64, 6.31]
    assert frame.volume.isna().tolist() == [False, True, False, False]
    assert frame.adj_factor.tolist() == pytest.approx([2.0] * 4)
    calendar = provider.fetch_calendar()
    assert len(calendar) == 4  # 非交易日被排除
    instruments = provider.fetch_instruments()
    assert instruments.symbol.tolist() == ["600518.SH"]
    assert instruments.ipo_date.tolist() == ["2001-03-19"]
    with pytest.raises(ValueError):
        provider.symbol("830799.BJ")


def test_baostock_login_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock("10001"))
    provider = BaoStockProvider(RequestGate(interval=0, attempts=1, sleep=lambda _: None))
    with pytest.raises(ConnectionError):
        provider.fetch_calendar()


@pytest.mark.parametrize(
    "code,day,st,session,ipo,expected",
    [
        ("600000.SH", date(2024, 1, 2), False, 100, date(1999, 11, 10), (0.1, "main_10pct")),
        ("600518.SH", date(2026, 7, 3), True, 100, date(2001, 3, 19), (0.05, "main_st_5pct")),
        ("600518.SH", date(2026, 7, 6), True, 100, date(2001, 3, 19), (0.1, "main_st_10pct")),
        ("600000.SH", date(2024, 1, 2), None, 100, date(1999, 11, 10), (None, "unknown_st_status")),
        ("300750.SZ", date(2020, 8, 21), False, 100, date(2018, 6, 11), (0.1, "chinext_10pct")),
        ("300750.SZ", date(2020, 8, 21), True, 100, date(2018, 6, 11), (0.05, "chinext_st_5pct")),
        ("300750.SZ", date(2020, 8, 24), True, 100, date(2018, 6, 11), (0.2, "chinext_20pct")),
        (
            "301001.SZ",
            date(2021, 1, 5),
            False,
            3,
            date(2020, 12, 30),
            (0.0, "ipo_first_5_sessions_no_limit"),
        ),
        (
            "300999.SZ",
            date(2019, 1, 2),
            False,
            0,
            date(2019, 1, 2),
            (None, "pre_reform_ipo_first_day_issue_price_rule"),
        ),
        (
            "688981.SH",
            date(2020, 7, 22),
            False,
            4,
            date(2020, 7, 16),
            (0.0, "ipo_first_5_sessions_no_limit"),
        ),
        ("688981.SH", date(2020, 7, 23), False, 5, date(2020, 7, 16), (0.2, "star_20pct")),
        (
            "603000.SH",
            date(2023, 4, 14),
            False,
            4,
            date(2023, 4, 10),
            (0.0, "ipo_first_5_sessions_no_limit"),
        ),
        (
            "603000.SH",
            date(2020, 1, 2),
            False,
            0,
            date(2020, 1, 2),
            (None, "pre_registration_ipo_first_day_issue_price_rule"),
        ),
        ("830799.BJ", date(2022, 1, 4), False, 30, date(2021, 11, 15), (0.3, "bse_30pct")),
        (
            "830799.BJ",
            date(2021, 11, 15),
            False,
            0,
            date(2021, 11, 15),
            (0.0, "bse_ipo_first_session_no_limit"),
        ),
        (
            "830799.BJ",
            date(2021, 1, 4),
            False,
            30,
            date(2020, 7, 27),
            (None, "before_bse_rules_verified"),
        ),
        ("600000.SH", date(2024, 1, 2), False, None, None, (None, "unknown_listing_date")),
        ("600000.SH", date(1999, 1, 4), False, -1, date(1999, 11, 10), (None, "before_listing")),
    ],
)
def test_limit_rule_table(code, day, st, session, ipo, expected):
    assert limit_ratio(code, day, st, session, ipo) == expected


def test_limit_price_rounds_half_up():
    assert limit_price(10.05, 0.1, True) == 11.06  # 11.055
    assert limit_price(3.33, 0.05, False) == 3.16  # 3.1635
    assert limit_price(6.64, 0.05, False) == 6.31


def long_calendar(provider):
    early = pd.DataFrame({"trade_date": pd.bdate_range("2001-03-19", "2019-05-16")})
    return pd.concat([early, provider.fetch_calendar()], ignore_index=True)


def test_short_calendar_makes_listing_session_unknown(provider):
    frame = apply_price_limits(bars(provider), provider.fetch_calendar(), date(2001, 3, 19))
    assert frame.limit_rule.tolist()[:4] == ["unknown_listing_date"] * 4
    assert frame.up_limit.isna().all() and not frame.price_limit_exempt.any()


def test_apply_limits_uses_exchange_pre_close_and_rejects_mismatch(provider):
    calendar = long_calendar(provider)
    frame = apply_price_limits(bars(provider), calendar, date(2001, 3, 19))
    assert frame.limit_rule.tolist() == ["main_10pct", "main_10pct", "main_st_5pct", "main_st_5pct"]
    assert frame.up_limit.tolist()[2:] == [6.97, 6.63]
    assert frame.down_limit.tolist()[2:] == [6.31, 5.99]
    # 价格超出计算值：规则不适用，改为未知并标记。
    broken = bars(provider)
    broken.loc[3, "high"] = 7.5
    marked = apply_price_limits(broken, calendar, date(2001, 3, 19))
    assert pd.isna(marked.up_limit.iloc[3]) and marked.limit_rule.iloc[3].startswith(
        "rule_mismatch"
    )
    assert not marked.price_limit_exempt.iloc[3]
    missing = bars(provider)
    missing.loc[0, "pre_close"] = pd.NA
    assert apply_price_limits(missing, calendar, date(2001, 3, 19)).limit_rule.iloc[0] == (
        "unknown_pre_close"
    )
    assert apply_price_limits(bars(provider), calendar, None).up_limit.isna().all()


def test_quality_respects_listing_suspension_and_exemption(provider):
    calendar = long_calendar(provider)
    frame = apply_price_limits(bars(provider), calendar, date(2001, 3, 19))
    report = quality_report(frame, calendar, date(2019, 5, 17), date(2019, 5, 22))
    kinds = {item["kind"] for item in report["issues"]}
    assert "invalid_volume" not in kinds and "unknown_up_limit" not in kinds
    assert report["suspended_rows_without_volume"] == 1
    later = quality_report(
        frame.iloc[2:],
        calendar,
        date(2019, 5, 17),
        date(2019, 5, 22),
        listed_from=date(2019, 5, 21),
    )
    assert later["sessions_outside_listing"] == 2
    assert "missing_sessions" not in {item["kind"] for item in later["issues"]}


def test_export_quant_research_lab_contract(provider, tmp_path):
    frame = apply_price_limits(bars(provider), long_calendar(provider), date(2001, 3, 19))
    summary = to_quant_research_lab(frame, "测试", tmp_path, "hfq")
    out = pd.read_csv(tmp_path / "bars.csv")
    assert list(out.columns) == [
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "available_at",
        "is_suspended",
        "up_limit",
        "down_limit",
        "price_limit_exempt",
    ]
    assert out.available_at.str.endswith("T15:00:00+08:00").all()
    assert out.is_suspended.tolist() == [False, True, False, False]
    assert out.volume.iloc[1] == 0 and summary["suspended_volume_filled_zero"] == 1
    # 后复权：价格与涨跌停按同一因子缩放，比较关系不变。
    assert out.close.iloc[3] == pytest.approx(12.0)
    assert out.up_limit.iloc[3] == pytest.approx(6.63 * 2)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["kind"] == "user_supplied" and manifest["corporate_actions_complete"] is False
    raw = to_quant_research_lab(frame, "测试", tmp_path / "raw", "raw")
    assert raw["price_mode"] == "raw"
    assert pd.read_csv(tmp_path / "raw" / "bars.csv").close.iloc[3] == 6.0
    unknown = frame.copy()
    unknown.loc[0, "is_suspended"] = pd.NA
    with pytest.raises(ValueError, match="is_suspended"):
        to_quant_research_lab(unknown, "测试", tmp_path / "bad")
    no_factor = frame.copy()
    no_factor.loc[0, "adj_factor"] = pd.NA
    with pytest.raises(ValueError, match="复权因子"):
        to_quant_research_lab(no_factor, "测试", tmp_path / "bad2")


def test_export_alpha_evidence_lab_preserves_suspended(provider, tmp_path):
    frame = apply_price_limits(bars(provider), long_calendar(provider), date(2001, 3, 19))
    summary = to_alpha_evidence_lab(frame, tmp_path)
    out = pd.read_csv(tmp_path / "600518.SH.csv")
    assert list(out.columns) == [
        "date",
        "open",
        "close",
        "available_at",
        "is_suspended",
        "up_limit",
        "down_limit",
        "price_limit_exempt",
    ]
    assert out.date.tolist() == ["2019-05-17", "2019-05-20", "2019-05-21", "2019-05-22"]
    assert summary["suspended_rows_dropped"] == {"600518.SH": 0}


def test_cli_export(monkeypatch, tmp_path):
    early = [str(d.date()) for d in pd.bdate_range("2001-03-19", "2019-05-16")]
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock(early_days=early))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REQUEST_INTERVAL", "0")
    args = [
        "export",
        "--provider",
        "baostock",
        "--codes",
        "600518.SH",
        "--start",
        "2019-05-17",
        "--end",
        "2019-05-22",
        "--target",
    ]
    assert main([*args, "quant-research-lab", "--output", str(tmp_path / "qrl")]) == 0
    report = json.loads((tmp_path / "qrl" / "export_report.json").read_text())
    assert report["rows"] == 4 and report["provider"] == "baostock"
    assert report["limit_rules"]["main_st_5pct"] == 2
    assert main([*args, "quant-research-lab", "--output", str(tmp_path / "qrl")]) == 1  # 非空目录
    assert main([*args, "alpha-evidence-lab", "--output", str(tmp_path / "alpha")]) == 0
    assert (tmp_path / "alpha" / "600518.SH.csv").exists()
    refused = ["export", "--provider", "synthetic", "--target", "quant-research-lab"]
    assert main([*refused, "--output", str(tmp_path / "s")]) == 1
    no_target = ["export", "--provider", "baostock", "--codes", "600518.SH"]
    assert main([*no_target, "--output", str(tmp_path / "t")]) == 1


def test_export_contract_with_actual_downstream_importers(provider, tmp_path, monkeypatch):
    from pathlib import Path

    workspace = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(workspace / "quant-research-lab" / "src"))
    monkeypatch.syspath_prepend(str(workspace / "alpha-evidence-lab" / "src"))
    from alpha_evidence_lab.data import load_csv
    from quant_research_lab.data import MarketDataset

    frame = apply_price_limits(bars(provider), long_calendar(provider), date(2001, 3, 19))
    to_alpha_evidence_lab(frame, tmp_path / "alpha")
    to_quant_research_lab(frame, "offline contract fixture", tmp_path / "quant")
    alpha = load_csv(tmp_path / "alpha" / "600518.SH.csv", "fixture", "offline test")
    quant = MarketDataset.read(
        tmp_path / "quant" / "bars.csv", tmp_path / "quant" / "manifest.json"
    )
    assert len(alpha.bars) == len(quant.frame) == len(frame)
    assert alpha.bars[1].is_suspended and bool(quant.frame.is_suspended.iloc[1])
    assert alpha.bars[2].down_limit == quant.frame.down_limit.iloc[2]
    assert alpha.bars[2].up_limit == quant.frame.up_limit.iloc[2]
