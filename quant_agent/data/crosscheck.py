"""BaoStock 日线（含按规则计算的涨跌停价）与 Tushare 的逐日核对。

不合并、不覆盖任一来源；只报告一致、不一致和单边缺失，供人工判断。
"""

from typing import Any

import pandas as pd

PRICE_TOL = 0.005
SAMPLES = 20


def _dates(frame: pd.DataFrame, column: str = "trade_date") -> pd.Series:
    return pd.to_datetime(frame[column].astype(str), format="mixed").dt.normalize()


def _sample(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    rows = frame[columns].head(SAMPLES).copy()
    rows["trade_date"] = rows["trade_date"].dt.strftime("%Y-%m-%d")
    return [
        {str(key): (None if pd.isna(v) else v) for key, v in row.items()}
        for row in rows.to_dict("records")
    ]


def compare_code(
    bars: pd.DataFrame, daily: pd.DataFrame, limits: pd.DataFrame, suspensions: pd.DataFrame
) -> dict[str, Any]:
    """bars：BaoStock 标准化日线并已 apply_price_limits；其余为 Tushare 原始返回。"""
    base = bars.copy()
    base["trade_date"] = pd.to_datetime(base["trade_date"]).dt.normalize()
    result: dict[str, Any] = {"baostock_rows": len(base)}

    # 1. 价格与昨收：Tushare daily 不含全天停牌日。
    if daily.empty:
        result["prices"] = {"status": "tushare_daily_empty"}
    else:
        ts = daily.assign(trade_date=_dates(daily))[
            ["trade_date", "open", "high", "low", "close", "pre_close"]
        ]
        merged = base.merge(ts, on="trade_date", how="outer", suffixes=("", "_ts"), indicator=True)
        both = merged[merged["_merge"] == "both"]
        diff = pd.Series(False, index=both.index)
        for column in ["open", "high", "low", "close", "pre_close"]:
            diff |= (both[column].astype(float) - both[f"{column}_ts"].astype(float)).abs() > (
                PRICE_TOL
            )
        only_base = merged[merged["_merge"] == "left_only"]
        trading_only_base = only_base[~only_base["is_suspended"].fillna(False).astype(bool)]
        result["prices"] = {
            "matched_rows": len(both),
            "mismatch_rows": int(diff.sum()),
            "mismatch_samples": _sample(
                both[diff], ["trade_date", "close", "close_ts", "pre_close", "pre_close_ts"]
            ),
            "baostock_trading_rows_missing_in_tushare": len(trading_only_base),
            "tushare_rows_missing_in_baostock": int((merged["_merge"] == "right_only").sum()),
        }

    # 2. 涨跌停价：规则计算值 vs Tushare 每日涨跌停价。
    if limits.empty:
        result["limits"] = {"status": "tushare_stk_limit_empty"}
    else:
        ts = limits.assign(trade_date=_dates(limits))[["trade_date", "up_limit", "down_limit"]]
        merged = base.merge(ts, on="trade_date", how="left", suffixes=("", "_ts"))
        has_ts = merged["up_limit_ts"].notna()
        known = merged["up_limit"].notna()
        exempt = merged["price_limit_exempt"].astype(bool)
        compared = merged[has_ts & known]
        wrong = (
            (compared["up_limit"].astype(float) - compared["up_limit_ts"]).abs() > PRICE_TOL
        ) | ((compared["down_limit"].astype(float) - compared["down_limit_ts"]).abs() > PRICE_TOL)
        result["limits"] = {
            "compared_rows": len(compared),
            "mismatch_rows": int(wrong.sum()),
            "mismatch_samples": _sample(
                compared[wrong],
                [
                    "trade_date",
                    "limit_rule",
                    "up_limit",
                    "up_limit_ts",
                    "down_limit",
                    "down_limit_ts",
                ],
            ),
            "rule_unknown_but_tushare_has_limit": int((has_ts & ~known & ~exempt).sum()),
            "rule_exempt_but_tushare_has_limit": int((has_ts & exempt).sum()),
            "rule_known_but_tushare_missing": int((~has_ts & known).sum()),
        }

    # 3. 停牌：BaoStock tradestatus vs Tushare 全天停牌记录（S 且无日内时段）。
    if suspensions.empty:
        full_day: set[pd.Timestamp] = set()
    else:
        timing = suspensions.get("suspend_timing", pd.Series(index=suspensions.index, dtype=object))
        mask = (suspensions["suspend_type"] == "S") & (timing.isna() | (timing.astype(str) == ""))
        full_day = set(_dates(suspensions[mask]))
    flagged = set(base.loc[base["is_suspended"].fillna(False).astype(bool), "trade_date"])
    in_window = {
        day for day in full_day if base["trade_date"].min() <= day <= base["trade_date"].max()
    }
    result["suspension"] = {
        "baostock_suspended": len(flagged),
        "tushare_full_day_suspended": len(in_window),
        "agree": len(flagged & in_window),
        "only_baostock_count": len(flagged - in_window),
        "only_tushare_count": len(in_window - flagged),
        "only_baostock": sorted(str(d.date()) for d in flagged - in_window)[:SAMPLES],
        "only_tushare": sorted(str(d.date()) for d in in_window - flagged)[:SAMPLES],
    }
    return result


def summarize(per_code: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def total(section: str, key: str) -> int:
        return sum(int(item.get(section, {}).get(key, 0)) for item in per_code.values())

    return {
        "price_mismatch_rows": total("prices", "mismatch_rows"),
        "limit_compared_rows": total("limits", "compared_rows"),
        "limit_mismatch_rows": total("limits", "mismatch_rows"),
        "limits_tushare_fills_unknown": total("limits", "rule_unknown_but_tushare_has_limit"),
        "suspension_agree": total("suspension", "agree"),
        "suspension_disagree": total("suspension", "only_baostock_count")
        + total("suspension", "only_tushare_count"),
    }


def raw_from_yahoo(yahoo: pd.DataFrame) -> pd.DataFrame:
    """把 Yahoo 按拆股调整过的价格还原为原始价格：乘以该日之后全部拆股比例之积。"""
    frame = yahoo.sort_values("trade_date").reset_index(drop=True).copy()
    ratios = frame["splits"].where(frame["splits"] > 0, 1.0).astype(float)
    # 当日之后（不含当日）的累计拆股比例；除权日当天的价格已是拆股后价格。
    later = ratios[::-1].cumprod()[::-1].shift(-1, fill_value=1.0)
    for column in ["open", "high", "low", "close"]:
        frame[column] = frame[column] * later
    return frame


def compare_yahoo(bars: pd.DataFrame, yahoo: pd.DataFrame) -> dict[str, Any]:
    """bars：BaoStock 标准化日线并已 apply_price_limits；yahoo：YahooSource.history 的结果。"""
    base = bars.copy()
    base["trade_date"] = pd.to_datetime(base["trade_date"]).dt.normalize()
    if yahoo.empty:
        return {"baostock_rows": len(base), "status": "yahoo_empty"}
    raw = raw_from_yahoo(yahoo)
    merged = base.merge(raw, on="trade_date", how="outer", suffixes=("", "_y"), indicator=True)
    both = merged[merged["_merge"] == "both"].copy()
    base_suspended = both["is_suspended"].fillna(False).astype(bool)
    yahoo_flat = (both["volume_y"] <= 0) & (both["high_y"] - both["low_y"]).abs().le(1e-9)

    # 1. 价格：双方都有成交的日子比较原始收盘价（容差：0.01 元或 0.05%，取大者）。
    traded = both[~base_suspended & ~yahoo_flat]
    tolerance = (traded["close"].astype(float) * 0.0005).clip(lower=0.01)
    wrong = (traded["close"].astype(float) - traded["close_y"]).abs() > tolerance
    # 2. 停牌/无成交：BaoStock 标记停牌 vs Yahoo 成交量为0且价格不动。
    only_base = both[base_suspended & ~yahoo_flat]
    only_yahoo = both[~base_suspended & yahoo_flat]
    # 3. 公司行动日期：BaoStock 复权因子变化日 vs Yahoo 分红/拆股日。
    factor = base.sort_values("trade_date").set_index("trade_date")["adj_factor"].astype(float)
    changed = factor.pct_change(fill_method=None).abs() > 1e-6
    base_actions = set(factor.index[changed])
    window = (yahoo["trade_date"] > base["trade_date"].min()) & (
        yahoo["trade_date"] <= base["trade_date"].max()
    )
    events = yahoo[window & ((yahoo["dividends"] > 0) | (yahoo["splits"] > 0))]
    yahoo_actions = set(events["trade_date"])
    # 4. 涨跌停规则：用 Yahoo 的原始最高/最低价独立检验规则计算值（仅比较双方都有成交的日子）。
    limited = traded[traded["up_limit"].notna()]
    breach = (limited["high_y"] > limited["up_limit"].astype(float) + 0.005) | (
        limited["low_y"] < limited["down_limit"].astype(float) - 0.005
    )

    def days(values: Any) -> list[str]:
        return sorted(str(pd.Timestamp(v).date()) for v in values)[:SAMPLES]

    sample = traded[wrong].assign(close_raw_y=traded.loc[wrong, "close_y"])
    return {
        "baostock_rows": len(base),
        "prices": {
            "compared_rows": len(traded),
            "mismatch_rows": int(wrong.sum()),
            "mismatch_samples": _sample(sample, ["trade_date", "close", "close_raw_y"]),
            "baostock_rows_missing_in_yahoo": int((merged["_merge"] == "left_only").sum()),
            "yahoo_rows_missing_in_baostock": int((merged["_merge"] == "right_only").sum()),
        },
        "suspension": {
            "agree": int((base_suspended & yahoo_flat).sum()),
            "only_baostock_count": len(only_base),
            "only_yahoo_count": len(only_yahoo),
            "only_baostock": days(only_base["trade_date"]),
            "only_yahoo": days(only_yahoo["trade_date"]),
        },
        "corporate_actions": {
            "agree": len(base_actions & yahoo_actions),
            "only_baostock_factor_change": days(base_actions - yahoo_actions),
            "only_yahoo_event": days(yahoo_actions - base_actions),
        },
        "limits": {
            "checked_rows": len(limited),
            "yahoo_price_outside_rule_limits": int(breach.sum()),
            "breach_samples": _sample(
                limited[breach],
                ["trade_date", "limit_rule", "up_limit", "high_y", "down_limit", "low_y"],
            ),
        },
    }


def summarize_yahoo(per_code: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def total(section: str, key: str) -> int:
        return sum(int(item.get(section, {}).get(key, 0)) for item in per_code.values())

    return {
        "price_compared_rows": total("prices", "compared_rows"),
        "price_mismatch_rows": total("prices", "mismatch_rows"),
        "suspension_agree": total("suspension", "agree"),
        "suspension_only_baostock": total("suspension", "only_baostock_count"),
        "suspension_only_yahoo": total("suspension", "only_yahoo_count"),
        "corporate_actions_agree": total("corporate_actions", "agree"),
        "corporate_actions_disagree": sum(
            len(item.get("corporate_actions", {}).get("only_baostock_factor_change", []))
            + len(item.get("corporate_actions", {}).get("only_yahoo_event", []))
            for item in per_code.values()
        ),
        "limit_checked_rows": total("limits", "checked_rows"),
        "limit_breaches_by_yahoo_prices": total("limits", "yahoo_price_outside_rule_limits"),
    }
