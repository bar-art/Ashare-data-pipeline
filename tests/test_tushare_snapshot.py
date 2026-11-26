import json
from datetime import date

import pandas as pd
import pytest

from quant_agent.cli import main
from quant_agent.data.tushare_snapshot import TushareSnapshotProvider, download, st_flags

DAYS = ["20240102", "20240103", "20240104", "20240105", "20240108", "20240109"]


def fake_query(calls):
    """模拟 Tushare 返回：600518.SH 在 01-04 全天停牌（daily 无该行）。"""

    def query(api, **params):
        calls.append(api)
        code = params.get("ts_code", "")
        if api == "trade_cal":
            return pd.DataFrame({"exchange": "SSE", "cal_date": DAYS, "is_open": 1})
        if api == "stock_basic":
            rows = {
                "L": [["600518.SH", "康美药业", "20010319", None]],
                "D": [["600001.SH", "邯郸钢铁", "19980122", "20240108"]],
                "P": [],
            }[params["list_status"]]
            return pd.DataFrame(rows, columns=["ts_code", "name", "list_date", "delist_date"])
        if api == "index_weight":
            # 1 月末权重只在月末发布；月中查询不得看到。
            if params["start_date"] != "20240101":
                return pd.DataFrame(columns=["index_code", "con_code", "trade_date", "weight"])
            return pd.DataFrame(
                {
                    "index_code": "399300.SZ",
                    "con_code": ["600518.SH", "600001.SH", "600518.SH"],
                    "trade_date": ["20240102", "20240102", "20240105"],
                    "weight": [0.5, 0.5, 1.0],
                }
            )
        if api == "daily":
            traded = [d for d in DAYS if d != "20240104"]
            if code == "600001.SH":
                traded = ["20240102", "20240103"]  # 之后停牌至退市
            close = [10.0 + i for i in range(len(traded))]
            return pd.DataFrame(
                {
                    "ts_code": code,
                    "trade_date": traded,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "pre_close": [9.0, *close[:-1]],
                    "vol": 12.0,
                    "amount": 3.0,
                }
            )
        if api == "adj_factor":
            if code == "600001.SH":
                days = [d for d in DAYS if d <= "20240104"]  # 长期停牌后不发布因子
                return pd.DataFrame({"ts_code": code, "trade_date": days, "adj_factor": 2.0})
            # 600518.SH 在停牌日 01-04 除权：来源当天即发布新因子 3.0
            factor = [2.0, 2.0, 3.0, 3.0, 3.0, 3.0]
            return pd.DataFrame({"ts_code": code, "trade_date": DAYS, "adj_factor": factor})
        if api == "namechange":
            return pd.DataFrame(
                {
                    "ts_code": code,
                    "name": ["康美药业", "*ST康美"],
                    "start_date": ["20010319", "20240105"],
                    "end_date": ["20240104", None],
                }
            )
        if api == "suspend_d":
            # 600518 在 01-04 全天停牌、01-08 盘中停牌（当天有成交）；600001 自 01-04 停牌至退市。
            rows = {
                "600518.SH": [("20240104", None), ("20240108", "09:30-10:30")],
                "600001.SH": [("20240104", None), ("20240105", None), ("20240108", None)],
            }[code]
            return pd.DataFrame(
                {
                    "ts_code": code,
                    "trade_date": [day for day, _ in rows],
                    "suspend_timing": [timing for _, timing in rows],
                    "suspend_type": "S",
                }
            )
        return pd.DataFrame({"ts_code": [code]})

    return query


@pytest.fixture
def snapshot(tmp_path):
    calls: list[str] = []
    root = tmp_path / "snap"
    download(fake_query(calls), root, "399300.SZ", date(2024, 1, 2), date(2024, 1, 9), "测试")
    return root, calls


def test_download_is_resumable_and_writes_manifest(snapshot):
    root, calls = snapshot
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["codes"] == ["600001.SH", "600518.SH"]
    first = len(calls)
    download(fake_query(calls), root, "399300.SZ", date(2024, 1, 2), date(2024, 1, 9), "测试")
    assert len(calls) == first  # 已有文件全部跳过


def test_bars_fill_suspension_convert_units_and_flag_st(snapshot):
    provider = TushareSnapshotProvider(snapshot[0])
    bars = provider.fetch_daily_bars("600518.SH", date(2024, 1, 2), date(2024, 1, 9))
    assert len(bars) == 6
    suspended = bars[bars.trade_date == "2024-01-04"].iloc[0]
    assert bool(suspended.is_suspended) and suspended.volume == 0
    assert suspended.close == 11.0 and suspended.pre_close == 11.0
    after = bars[bars.trade_date == "2024-01-05"].iloc[0]
    assert after.pre_close == 11.0 and not bool(after.is_suspended)
    assert bars.volume.iloc[0] == 1200 and bars.amount.iloc[0] == 3000  # 手→股，千元→元
    assert bars.is_st.tolist() == [False, False, False, True, True, True]
    # 停牌日沿用停牌前因子，后复权价不在无成交时跳变；因子变化落在复牌日。
    assert bars.adj_factor.tolist() == [2.0, 2.0, 2.0, 3.0, 3.0, 3.0]
    with pytest.raises(ValueError, match="超出快照范围"):
        provider.fetch_daily_bars("600518.SH", date(2023, 1, 2), date(2024, 1, 9))


def test_delisted_stock_suspended_until_delisting(snapshot):
    bars = TushareSnapshotProvider(snapshot[0]).fetch_daily_bars(
        "600001.SH", date(2024, 1, 2), date(2024, 1, 9)
    )
    assert bars.trade_date.dt.strftime("%m%d").tolist() == ["0102", "0103", "0104", "0105", "0108"]
    assert bars.is_suspended.tolist() == [False, False, True, True, True]
    late = TushareSnapshotProvider(snapshot[0]).fetch_daily_bars(
        "600001.SH", date(2024, 1, 4), date(2024, 1, 9)
    )
    assert late.pre_close.iloc[0] == 11.0  # 区间首行停牌：昨收取停牌前收盘


def test_members_never_use_later_weights(snapshot):
    provider = TushareSnapshotProvider(snapshot[0])
    early = provider.fetch_index_members("hs300", date(2024, 1, 4))
    assert sorted(early.symbol) == ["600001.SH", "600518.SH"]
    assert early.source_update_date.unique().tolist() == ["2024-01-02"]
    assert provider.fetch_index_members("hs300", date(2024, 1, 8)).symbol.tolist() == ["600518.SH"]
    with pytest.raises(ValueError, match="不得回退"):
        provider.fetch_index_members("hs300", date(2023, 12, 29))


def test_st_unknown_without_name_record():
    days = pd.Series(pd.to_datetime(["2020-01-02", "2024-01-05"]))
    names = pd.DataFrame({"name": ["*ST甲"], "start_date": ["20240101"], "end_date": [None]})
    assert st_flags(names, days).tolist()[1] is True
    assert pd.isna(st_flags(names, days).iloc[0])


def test_cli_universe_and_export_from_snapshot(snapshot, monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_SNAPSHOT_DIR", str(snapshot[0]))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    universe = ["universe", "--provider", "tushare", "--snapshot-dates", "2024-01-02"]
    assert main([*universe, "2024-01-08", "--output", str(tmp_path / "u")]) == 0
    members = tmp_path / "u" / "hs300_historical_members.csv"
    export = ["export", "--provider", "tushare", "--start", "2024-01-02", "--end", "2024-01-09"]
    export += ["--target", "quant-research-lab", "--universe-file", str(members)]
    assert main([*export, "--output", str(tmp_path / "out")]) == 0
    bars = pd.read_csv(tmp_path / "out" / "bars.csv")
    removed = bars[bars.ts_code == "600001.SH"]
    assert removed.universe_member.tolist() == [True, True, True, True, False]
    kept = bars[bars.ts_code == "600518.SH"]
    assert kept.universe_member.all() and kept.is_suspended.sum() == 1
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert "本地快照" in manifest["source"]
    assert main(["crosscheck", "--provider", "tushare", "--check-source", "tushare"]) == 1


def test_chinext_covers_whole_30_segment():
    from quant_agent.data.rules import board

    assert board("302132.SZ") == "chinext" and board("301001.SZ") == "chinext"
    assert board("002594.SZ") == "main"


def drop_daily(root, code, day):
    path = root / "daily" / f"{code}.parquet"
    frame = pd.read_parquet(path)
    frame[frame.trade_date != day].to_parquet(path, index=False)


def test_missing_trading_day_is_unknown_not_suspended(snapshot):
    """审查反例：删掉正常交易日 01-03 的日线，不能凭缺行生成停牌。"""
    root = snapshot[0]
    drop_daily(root, "600518.SH", "20240103")
    bars = TushareSnapshotProvider(root).fetch_daily_bars(
        "600518.SH", date(2024, 1, 2), date(2024, 1, 9)
    )
    status = dict(zip(bars.trade_date.dt.strftime("%m%d"), bars.bar_status, strict=True))
    assert status == {
        "0102": "traded",
        "0103": "missing",
        "0104": "suspended",
        "0105": "traded",
        "0108": "traded",  # 盘中停牌不吞掉当天真实成交
        "0109": "traded",
    }
    gap = bars[bars.trade_date == "2024-01-03"].iloc[0]
    assert pd.isna(gap.is_suspended) and pd.isna(gap.volume)
    confirmed = bars[bars.trade_date == "2024-01-04"].iloc[0]
    assert bool(confirmed.is_suspended) and confirmed.volume == 0


def test_full_day_suspension_with_trades_is_a_conflict(snapshot):
    root = snapshot[0]
    path = root / "suspend_d" / "600518.SH.parquet"
    frame = pd.read_parquet(path)
    frame.loc[len(frame)] = ["600518.SH", "20240105", None, "S"]
    frame.to_parquet(path, index=False)
    bars = TushareSnapshotProvider(root).fetch_daily_bars(
        "600518.SH", date(2024, 1, 2), date(2024, 1, 9)
    )
    row = bars[bars.trade_date == "2024-01-05"].iloc[0]
    assert row.bar_status == "conflict" and pd.isna(row.is_suspended)


def test_delisting_tail_needs_evidence(snapshot):
    root = snapshot[0]
    path = root / "suspend_d" / "600001.SH.parquet"
    frame = pd.read_parquet(path)
    frame[frame.trade_date != "20240108"].to_parquet(path, index=False)
    bars = TushareSnapshotProvider(root).fetch_daily_bars(
        "600001.SH", date(2024, 1, 2), date(2024, 1, 9)
    )
    assert bars.bar_status.tolist() == ["traded", "traded", "suspended", "suspended", "missing"]
    assert bars.trade_date.max() == pd.Timestamp("2024-01-08")  # 不在退市后继续估值


def test_export_refuses_gaps_unless_placeholders_are_explicit(snapshot, monkeypatch, tmp_path):
    root = snapshot[0]
    drop_daily(root, "600518.SH", "20240103")
    monkeypatch.setenv("TUSHARE_SNAPSHOT_DIR", str(root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    export = ["export", "--provider", "tushare", "--codes", "600518.SH"]
    export += ["--start", "2024-01-02", "--end", "2024-01-09", "--target", "quant-research-lab"]
    assert main([*export, "--output", str(tmp_path / "strict")]) == 1
    assert main([*export, "--allow-gap-placeholders", "--output", str(tmp_path / "loose")]) == 0
    report = json.loads((tmp_path / "loose" / "export_report.json").read_text())
    assert report["gap_placeholder_rows"] == 1
    assert report["gap_placeholders"] == {"600518.SH": ["2024-01-03"]}
    bars = pd.read_csv(tmp_path / "loose" / "bars.csv")
    placeholder = bars[bars.trade_date.str.startswith("2024-01-03")].iloc[0]
    assert bool(placeholder.is_suspended)  # 下游不可成交，但报告写明这是缺口占位而非确认停牌
    quality = report["quality_issues"]["600518.SH"]
    assert any(
        item["kind"] == "unverified_gap" and "2024-01-03" in item["detail"] for item in quality
    )
