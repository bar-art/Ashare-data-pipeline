import json
import sys
from datetime import date

import pandas as pd
import pytest
from test_baostock_export import fake_baostock

from quant_agent.cli import main
from quant_agent.data.universe import load_snapshots, membership, monthly_snapshot_dates


def write_snapshots(path, rows):
    pd.DataFrame(rows, columns=["symbol", "snapshot_date", "index"]).to_csv(path, index=False)
    return path


def test_monthly_dates_come_from_source_calendar():
    calendar = pd.DataFrame(
        {"trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-02-01", "2024-03-04"])}
    )
    assert monthly_snapshot_dates(calendar, date(2024, 1, 3), date(2024, 3, 31)) == [
        date(2024, 1, 3),
        date(2024, 2, 1),
        date(2024, 3, 4),
    ]
    with pytest.raises(ValueError):
        monthly_snapshot_dates(calendar, date(2025, 1, 1), date(2025, 2, 1))


def test_membership_uses_latest_snapshot_not_later(tmp_path):
    snapshots = load_snapshots(
        write_snapshots(
            tmp_path / "s.csv",
            [
                ["600518.SH", "2019-05-17", "hs300"],
                ["000002.SZ", "2019-05-17", "hs300"],
                ["000002.SZ", "2019-05-21", "hs300"],  # 600518 在 05-21 被调出
            ],
        )
    )
    bars = pd.DataFrame(
        {
            "ts_code": ["600518.SH"] * 4 + ["000002.SZ"],
            "trade_date": pd.to_datetime(
                ["2019-05-17", "2019-05-20", "2019-05-21", "2019-05-22", "2019-05-20"]
            ),
        }
    )
    assert membership(snapshots, bars).tolist() == [True, True, False, False, True]
    early = bars.assign(trade_date=pd.to_datetime(["2019-05-16"] * 5))
    with pytest.raises(ValueError, match="晚于行情起点"):
        membership(snapshots, early)


def test_snapshot_file_validation(tmp_path):
    pd.DataFrame({"symbol": ["x"]}).to_csv(tmp_path / "b.csv", index=False)
    with pytest.raises(ValueError, match="缺少字段"):
        load_snapshots(tmp_path / "b.csv")
    mixed = [["600518.SH", "2019-05-17", "hs300"], ["600000.SH", "2019-05-17", "zz500"]]
    with pytest.raises(ValueError, match="一个指数"):
        load_snapshots(write_snapshots(tmp_path / "c.csv", mixed))
    duplicate = [["600518.SH", "2019-05-17", "hs300"]] * 2
    with pytest.raises(ValueError, match="重复"):
        load_snapshots(write_snapshots(tmp_path / "d.csv", duplicate))


def test_cli_export_with_universe_includes_removed_and_reports_unavailable(monkeypatch, tmp_path):
    early = [str(d.date()) for d in pd.bdate_range("2001-03-19", "2019-05-16")]
    monkeypatch.setitem(sys.modules, "baostock", fake_baostock(early_days=early))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REQUEST_INTERVAL", "0")
    snapshots = write_snapshots(
        tmp_path / "hs300.csv",
        [
            ["600518.SH", "2019-05-17", "hs300"],
            ["000002.SZ", "2019-05-17", "hs300"],  # 伪造来源中没有它的行情
            ["000002.SZ", "2019-05-21", "hs300"],
        ],
    )
    base = ["export", "--provider", "baostock", "--start", "2019-05-17", "--end", "2019-05-22"]
    base += ["--target", "quant-research-lab", "--universe-file", str(snapshots)]
    # 默认：取不到行情就停止，不悄悄缩小股票池。
    assert main([*base, "--output", str(tmp_path / "strict")]) == 1
    assert main([*base, "--skip-unavailable", "--output", str(tmp_path / "out")]) == 0
    bars = pd.read_csv(tmp_path / "out" / "bars.csv")
    assert bars.universe_member.tolist() == [True, True, False, False]
    report = json.loads((tmp_path / "out" / "export_report.json").read_text())
    assert list(report["unavailable_codes"]) == ["000002.SZ"]
    assert report["universe_member_rows"] == 2
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert "hs300 历史成分快照（2 次" in manifest["source"]
    alpha = ["export", "--provider", "baostock", "--codes", "600518.SH", "--target"]
    alpha += ["alpha-evidence-lab", "--universe-file", str(snapshots)]
    assert main([*alpha, "--output", str(tmp_path / "alpha")]) == 1
    monthly = ["universe", "--provider", "baostock", "--monthly"]
    assert main([*monthly, "--start", "2030-01-01", "--end", "2030-02-01"]) == 1  # 无交易日
