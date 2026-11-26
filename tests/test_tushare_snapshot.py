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
