"""历史涨跌停价：只按已核验的板块规则由交易所昨收（除权参考价）计算，规则外一律标为未知。

已核验规则（来源见 docs/data_coverage.md）：
- 主板：10%；风险警示（ST/*ST）5%，2026-07-06 起调整为 10%。
- 创业板：2020-08-24 起 20%（含风险警示）；此前 10%，风险警示 5%。
- 科创板：20%。
- 北交所：2021-11-15 起 30%。
- 新股：科创板、2020-08-24 起上市的创业板、2023-04-10 起上市的主板，前 5 个交易日不设涨跌幅；
  北交所上市首日不设涨跌幅。更早的主板/创业板上市首日按发行价计算（+44%/-36%），本模块不计算。

退市整理期、重新上市等特殊情形未建模；若当日最高/最低价超出计算出的涨跌停价，判定规则不适用，
该日涨跌停价改为未知（下游回测据此拒绝成交），并计入报告。
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pandas as pd

CHINEXT_REFORM = date(2020, 8, 24)
MAIN_REGISTRATION = date(2023, 4, 10)
MAIN_ST_TEN_PERCENT = date(2026, 7, 6)
BSE_OPEN = date(2021, 11, 15)
STAR_OPEN = date(2019, 7, 22)


def board(code: str) -> str:
    symbol, exchange = code.split(".")
    if exchange == "BJ":
        return "bse"
    if exchange == "SH":
        return "star" if symbol.startswith(("688", "689")) else "main"
    # 创业板代码段为 30xxxx（300、301 以及更名换号后启用的 302 等）。
    return "chinext" if symbol.startswith("30") else "main"


def limit_ratio(
    code: str, day: date, is_st: bool | None, listing_session: int | None, ipo_date: date | None
) -> tuple[float | None, str]:
    """返回 (涨跌幅比例或 None, 规则说明)。比例为 0 表示该日不设涨跌幅（豁免）。

    listing_session：上市后的第几个交易日（上市首日为 0）；未知时为 None。
    """
    kind = board(code)
    if listing_session is None or ipo_date is None:
        return None, "unknown_listing_date"
    if listing_session < 0:
        return None, "before_listing"
    first_days = listing_session < 5
    if kind == "star":
        if day < STAR_OPEN:
            return None, "before_star_market"
        if first_days:
            return 0.0, "ipo_first_5_sessions_no_limit"
        return 0.2, "star_20pct"
    if kind == "bse":
        if day < BSE_OPEN:
            return None, "before_bse_rules_verified"
        if listing_session == 0:
            return 0.0, "bse_ipo_first_session_no_limit"
        return 0.3, "bse_30pct"
    if kind == "chinext":
        if ipo_date >= CHINEXT_REFORM and first_days:
            return 0.0, "ipo_first_5_sessions_no_limit"
        if listing_session == 0:
            return None, "pre_reform_ipo_first_day_issue_price_rule"
        if day >= CHINEXT_REFORM:
            return 0.2, "chinext_20pct"
        if is_st is None:
            return None, "unknown_st_status"
        return (0.05, "chinext_st_5pct") if is_st else (0.1, "chinext_10pct")
    # 主板
    if ipo_date >= MAIN_REGISTRATION and first_days:
        return 0.0, "ipo_first_5_sessions_no_limit"
    if listing_session == 0:
        return None, "pre_registration_ipo_first_day_issue_price_rule"
    if is_st is None:
        return None, "unknown_st_status"
    if is_st and day < MAIN_ST_TEN_PERCENT:
        return 0.05, "main_st_5pct"
    return 0.1, "main_st_10pct" if is_st else "main_10pct"


def limit_price(pre_close: float, ratio: float, up: bool) -> float:
    value = Decimal(str(pre_close)) * (Decimal(1) + (Decimal(str(ratio)) * (1 if up else -1)))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def apply_price_limits(
    frame: pd.DataFrame, calendar: pd.DataFrame, ipo_date: date | None
) -> pd.DataFrame:
    """在标准化日线上填入 up_limit、down_limit、price_limit_exempt 与 limit_rule。"""
    result = frame.copy()
    days = pd.to_datetime(calendar.trade_date).sort_values().reset_index(drop=True)
    ups: list[float | None] = []
    downs: list[float | None] = []
    exempt: list[bool] = []
    rules: list[str] = []
    records: list[dict[Any, Any]] = result.to_dict(orient="records")
    for row in records:
        day = pd.Timestamp(row["trade_date"]).date()
        session: int | None = None
        if ipo_date is not None:
            session = int(((days >= pd.Timestamp(ipo_date)) & (days < pd.Timestamp(day))).sum())
            if day < ipo_date:
                session = -1
            elif days.empty or pd.Timestamp(ipo_date) < days.iloc[0]:
                # 日历晚于上市日：只知道下界（日历内天数+上市日）；不足5日时无法判断新股豁免。
                session = session + 1 if session + 1 >= 5 else None
        is_st = None if pd.isna(row["is_st"]) else bool(row["is_st"])
        ratio, rule = limit_ratio(str(row["ts_code"]), day, is_st, session, ipo_date)
        up = down = None
        if ratio is not None and ratio > 0:
            if pd.isna(row["pre_close"]) or float(row["pre_close"]) <= 0:
                ratio, rule = None, "unknown_pre_close"
            else:
                up = limit_price(float(row["pre_close"]), ratio, True)
                down = limit_price(float(row["pre_close"]), ratio, False)
                # 实际成交价超出计算值：规则不适用于该日（如特殊上市安排），改为未知。
                if float(row["high"]) > up + 0.005 or float(row["low"]) < down - 0.005:
                    up = down = None
                    rule = f"rule_mismatch:{rule}"
        ups.append(up)
        downs.append(down)
        exempt.append(ratio == 0.0)
        rules.append(rule)
    result["up_limit"] = pd.array(ups, dtype="Float64")
    result["down_limit"] = pd.array(downs, dtype="Float64")
    result["price_limit_exempt"] = exempt
    result["limit_rule"] = rules
    return result
