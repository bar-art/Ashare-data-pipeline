"""质量诊断不把缺失日期自动解释为停牌，也不猜测历史涨跌停。"""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from quant_agent.data.schema import OPTIONAL, PRICES

DISCLAIMER = "仅供研究，不构成投资建议"


def quality_report(
    frame: pd.DataFrame,
    calendar: pd.DataFrame,
    start: date,
    end: date,
    factor_jump_threshold: float = 0.2,
    listed_from: date | None = None,
    listed_to: date | None = None,
) -> dict[str, Any]:
    """listed_from/listed_to 来自证券基础资料；其外的缺失交易日属未上市或已退市，单列不计为问题。"""
    issues: list[dict[str, Any]] = []

    def add(kind: str, count: int, detail: str) -> None:
        if count:
            issues.append({"kind": kind, "count": count, "detail": detail})

    days = pd.to_datetime(calendar.trade_date)
    within = days[days.between(pd.Timestamp(start), pd.Timestamp(end))]
    actual = pd.to_datetime(frame.trade_date)
    expected = within
    if listed_from is not None:
        expected = expected[expected >= pd.Timestamp(listed_from)]
    if listed_to is not None:
        expected = expected[expected <= pd.Timestamp(listed_to)]
    outside = len(within) - len(expected)
    missing = sorted(set(expected.dt.strftime("%Y-%m-%d")) - set(actual.dt.strftime("%Y-%m-%d")))
    add("missing_sessions", len(missing), ", ".join(missing) + "；需结合上市、退市与停牌历史核对")
    beyond = pd.Timestamp(start) < days.min() or pd.Timestamp(end) > days.max()
    add("calendar_coverage", int(beyond), "请求区间超出日历首末日期；不以工作日推算交易日")
    add("off_calendar", int((~actual.isin(days)).sum()), "行情日期不在来源交易日历中")
    prices = frame[PRICES].astype(float)
    add(
        "invalid_prices",
        int(((prices <= 0) | ~np.isfinite(prices)).any(axis=1).sum()),
        "价格缺失、非有限数、为零或为负",
    )
    invalid_ohlc = (frame.high < frame[PRICES].max(axis=1)) | (
        frame.low > frame[PRICES].min(axis=1)
    )
    add("invalid_ohlc", int(invalid_ohlc.sum()), "高低价未覆盖开收盘价")
    suspended = frame["is_suspended"].fillna(False).astype(bool)
    for column in ["volume", "amount"]:
        values = frame[column].astype(float)
        bad = (values < 0) | ~np.isfinite(values)
        # 来源标记停牌的行没有成交，成交数据为空不是质量问题，单独计数。
        add(f"invalid_{column}", int((bad & ~suspended).sum()), "缺失或非法成交数据")
    exempt = (
        frame["price_limit_exempt"].astype(bool)
        if "price_limit_exempt" in frame
        else pd.Series(False, index=frame.index)
    )
    for column in OPTIONAL:
        unknown = frame[column].isna()
        if column in {"up_limit", "down_limit"}:
            unknown &= ~exempt
        add(f"unknown_{column}", int(unknown.sum()), "历史字段缺失；不推断、不填充")
    add(
        "limit_breach",
        int(((frame.high > frame.up_limit + 0.005) | (frame.low < frame.down_limit - 0.005)).sum()),
        "超出来源涨跌停价格",
    )
    factors = frame.adj_factor.dropna().astype(float)
    add(
        "invalid_adj_factor",
        int(((factors <= 0) | ~np.isfinite(factors)).sum()),
        "复权因子非正数或非有限数",
    )
    ratios = frame.sort_values("trade_date").adj_factor.pct_change(fill_method=None).abs()
    add(
        "factor_jump",
        int((ratios > factor_jump_threshold).sum()),
        "复权因子相邻观测跳变超过诊断阈值；公司行动也会触发，需复核",
    )
    return {
        "status": "needs_review" if issues else "checks_passed_not_completeness_proof",
        "rows": len(frame),
        "requested_start": str(start),
        "requested_end": str(end),
        "calendar_start": str(days.min().date()),
        "calendar_end": str(days.max().date()),
        "factor_jump_threshold": factor_jump_threshold,
        "sessions_outside_listing": outside,
        "suspended_rows": int(suspended.sum()),
        "suspended_rows_without_volume": int((suspended & frame["volume"].isna()).sum()),
        "limit_exempt_rows": int(exempt.sum()),
        "full_market_coverage": False,
        "pit_verified": False,
        "issues": issues,
        "disclaimer": DISCLAIMER,
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# 数据质量报告",
        "",
        f"状态：{report['status']}；行数：{report['rows']}",
        "",
        f"数据提供方：{report.get('provider', 'unspecified')}；"
        f"人工样本：{report.get('synthetic', 'unspecified')}",
        "",
        "本报告不证明全市场完整性或历史可得性（PIT）。",
        "",
        "| 检查项 | 数量 | 说明 |",
        "|---|---:|---|",
    ]
    lines += [
        f"| {issue['kind']} | {issue['count']} | {issue['detail']} |" for issue in report["issues"]
    ]
    return "\n".join([*lines, "", DISCLAIMER, ""])
