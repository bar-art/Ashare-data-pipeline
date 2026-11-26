"""日线统一字段；未知历史状态不能默认为否。"""

import re
from datetime import date

import pandas as pd

PRICES = ["open", "high", "low", "close"]
OPTIONAL = [
    "pre_close",
    "adj_factor",
    "up_limit",
    "down_limit",
    "is_st",
    "is_suspended",
    "bar_status",
]
# 行状态：traded（有成交）、suspended（来源确认的全天停牌）、missing（无行情也无停牌证据）、
# conflict（来源相互矛盾）。missing/conflict 的 is_suspended 为未知。
BAR_STATUSES = {"traded", "suspended", "missing", "conflict"}
COLUMNS = [
    "ts_code",
    "trade_date",
    *PRICES,
    "pre_close",
    "volume",
    "amount",
    "adj_factor",
    "is_st",
    "is_suspended",
    "up_limit",
    "down_limit",
    "bar_status",
]


def validate_request(code: str, start: date, end: date) -> None:
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        raise ValueError("股票代码需包含交易所，例如 600000.SH")
    symbol, exchange = code.split(".")
    prefixes = {"SH": ("60", "68"), "SZ": ("00", "30"), "BJ": ("4", "8", "92")}
    if not symbol.startswith(prefixes[exchange]):
        raise ValueError("证券代码前缀与交易所不一致或不是支持的A股代码")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if end >= date.today():
        raise ValueError("本阶段仅接收已结束的历史日期，结束日期必须早于今天")


def normalize(frame: pd.DataFrame, code: str) -> pd.DataFrame:
    required = ["trade_date", *PRICES, "volume", "amount"]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"行情字段缺失：{sorted(missing)}")
    result = frame.copy()
    if "ts_code" in result and not result["ts_code"].eq(code).all():
        raise ValueError("返回数据包含其他证券")
    result["ts_code"] = code
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="raise").dt.normalize()
    if result["trade_date"].isna().any():
        raise ValueError("交易日期为空")
    for column in OPTIONAL:
        if column not in result:
            result[column] = pd.NA
    for column in [
        *PRICES,
        "pre_close",
        "volume",
        "amount",
        "adj_factor",
        "up_limit",
        "down_limit",
    ]:
        result[column] = pd.to_numeric(result[column], errors="raise").astype("Float64")
    for column in ["is_st", "is_suspended"]:
        result[column] = result[column].astype("boolean")
    result["bar_status"] = result["bar_status"].astype("string")
    unknown_status = ~result["bar_status"].isna() & ~result["bar_status"].isin(BAR_STATUSES)
    if unknown_status.any():
        raise ValueError(
            f"未知的行状态：{sorted(result.loc[unknown_status, 'bar_status'].unique())}"
        )
    if result.duplicated(["ts_code", "trade_date"]).any():
        raise ValueError("同一证券同一日期存在重复行情")
    return result[COLUMNS].sort_values("trade_date").reset_index(drop=True)
