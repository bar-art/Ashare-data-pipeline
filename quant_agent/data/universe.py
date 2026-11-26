"""历史指数成分快照 → 逐日股票池资格（universe_member）。

规则：某交易日使用"不晚于该日的最近一次快照"。快照之间成分视为不变，所以月度快照下调入调出最多滞后
一个快照间隔；这是近似，导出报告会写明快照日期。首个快照晚于导出起点时拒绝，不向前回填。
"""

from datetime import date
from pathlib import Path

import pandas as pd

REQUIRED = {"symbol", "snapshot_date", "index"}


def load_snapshots(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"symbol": str})
    if REQUIRED - set(frame.columns):
        raise ValueError(f"成分快照缺少字段：{sorted(REQUIRED - set(frame.columns))}")
    if frame.empty:
        raise ValueError("成分快照为空")
    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"], errors="raise").dt.normalize()
    if frame["index"].nunique() != 1:
        raise ValueError("一个成分文件只能包含一个指数")
    if frame.duplicated(["snapshot_date", "symbol"]).any():
        raise ValueError("同一快照日期出现重复证券")
    return frame


def monthly_snapshot_dates(calendar: pd.DataFrame, start: date, end: date) -> list[date]:
    """区间内每个月的第一个交易日（取自来源交易日历，不按工作日推算）。"""
    days = pd.to_datetime(calendar["trade_date"]).dt.normalize()
    days = days[days.between(pd.Timestamp(start), pd.Timestamp(end))].sort_values()
    if days.empty:
        raise ValueError("区间内没有交易日")
    firsts = days.groupby(days.dt.to_period("M")).min()
    return [stamp.date() for stamp in firsts]


def membership(snapshots: pd.DataFrame, bars: pd.DataFrame) -> pd.Series:
    """对 bars 的每一行（ts_code, trade_date）返回是否在当日适用快照中。"""
    dates = pd.to_datetime(bars["trade_date"]).dt.normalize()
    snapshot_days = sorted(snapshots["snapshot_date"].unique())
    if dates.min() < pd.Timestamp(snapshot_days[0]):
        raise ValueError(
            f"首个成分快照 {pd.Timestamp(snapshot_days[0]).date()} 晚于行情起点 "
            f"{dates.min().date()}；请补充更早的快照，不向前回填"
        )
    members = {
        pd.Timestamp(str(day)): set(group["symbol"].astype(str))
        for day, group in snapshots.groupby("snapshot_date")
    }
    keys = pd.Series(pd.to_datetime(snapshot_days))
    index = keys.searchsorted(dates, side="right") - 1
    applicable = keys.iloc[index].to_numpy()
    return pd.Series(
        [
            str(code) in members[pd.Timestamp(day)]
            for code, day in zip(bars["ts_code"], applicable, strict=True)
        ],
        index=bars.index,
        dtype=bool,
    )
