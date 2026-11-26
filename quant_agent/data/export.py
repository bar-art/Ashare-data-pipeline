"""把标准化日线导出为 quant-research-lab 与 alpha-evidence-lab 的输入格式。

两个下游项目都在交易日 15:00（上海时间）收盘后决策，因此完整日线的 available_at 记为当日 15:00。
这假设收盘价在收盘时即可得；它不是来源发布时间的证明。

价格口径：
- hfq（默认）：不复权价格乘以后复权因子。因子只依赖当日及之前的公司行动，除权日不会出现虚假跌幅；
  涨跌停价用同一因子缩放，与开盘价的比较关系不变。两个下游项目都按比例计费、允许碎股，缩放不影响结果。
- raw：交易所不复权价格；除权日会表现为价格下跳，下游收益会计入这部分非真实亏损。
"""

import json
from pathlib import Path
from typing import Any, Literal

import pandas as pd

PriceMode = Literal["hfq", "raw"]
CLOSE_TIME = "T15:00:00+08:00"


def _scaled(frame: pd.DataFrame, mode: PriceMode) -> pd.DataFrame:
    result = frame.copy()
    if mode == "raw":
        return result
    if result["adj_factor"].isna().any():
        raise ValueError("存在未知复权因子，不能导出后复权价格；可改用 --price-mode raw")
    for column in ["open", "high", "low", "close", "up_limit", "down_limit"]:
        result[column] = result[column].astype(float) * result["adj_factor"].astype(float)
    return result


def _fill_suspended_volume(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """来源标记为停牌（tradestatus=0）的行无成交；其成交量为空时按0导出。其他缺失不填。"""
    result = frame.copy()
    mask = result["is_suspended"].fillna(False).astype(bool) & result["volume"].isna()
    result.loc[mask, "volume"] = 0.0
    return result, int(mask.sum())


def resolve_gaps(
    frame: pd.DataFrame, allow_placeholders: bool
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """缺口行（无行情也无停牌证据）默认保持未知，导出会拒绝。

    显式允许时，缺口行改为不可成交的估值占位（is_suspended=True，沿用上一估值），
    bar_status 仍为 missing，并逐证券记录日期；这不是"确认停牌"。来源矛盾（conflict）不放行。
    """
    result = frame.copy()
    status = (
        result["bar_status"] if "bar_status" in result else pd.Series(pd.NA, index=result.index)
    )
    gaps = (status == "missing").fillna(False).astype(bool)
    placeholders = {
        str(code): sorted(pd.to_datetime(group["trade_date"]).dt.strftime("%Y-%m-%d"))
        for code, group in result[gaps].groupby("ts_code")
    }
    if allow_placeholders and gaps.any():
        result.loc[gaps, "is_suspended"] = True
    return result, placeholders if allow_placeholders else {}


def _require_known(frame: pd.DataFrame, columns: list[str]) -> None:
    if "bar_status" in frame:
        unresolved = frame["is_suspended"].isna() & frame["bar_status"].isin(
            ["missing", "conflict"]
        )
    else:
        unresolved = pd.Series(False, index=frame.index)
    if unresolved.any():
        unknown = frame[unresolved]
        sample = [
            f"{code} {pd.Timestamp(day).date()}（{state}）"
            for code, day, state in unknown[["ts_code", "trade_date", "bar_status"]]
            .head(5)
            .itertuples(index=False)
        ]
        raise ValueError(
            f"{len(unknown)} 行既无行情也无停牌证据或来源矛盾：{'; '.join(sample)}；"
            "可核对来源，或显式加 --allow-gap-placeholders 导出为不可成交的估值占位"
        )
    for column in columns:
        missing = int(frame[column].isna().sum())
        if missing:
            raise ValueError(f"{column} 有 {missing} 行未知；未知状态不能当作否，停止导出")


def export_summary(frame: pd.DataFrame) -> dict[str, Any]:
    rules = frame["limit_rule"].value_counts().to_dict()
    return {
        "rows": len(frame),
        "codes": sorted(frame["ts_code"].unique().tolist()),
        "first_date": str(pd.Timestamp(frame["trade_date"].min()).date()),
        "last_date": str(pd.Timestamp(frame["trade_date"].max()).date()),
        "suspended_rows": int(frame["is_suspended"].astype(bool).sum()),
        "st_rows": int(frame["is_st"].astype(bool).sum()),
        "limit_exempt_rows": int(frame["price_limit_exempt"].sum()),
        "unknown_limit_rows": int(
            ((frame["up_limit"].isna()) & ~frame["price_limit_exempt"]).sum()
        ),
        "rule_mismatch_rows": int(frame["limit_rule"].str.startswith("rule_mismatch").sum()),
        "limit_rules": {str(key): int(value) for key, value in rules.items()},
    }


def to_quant_research_lab(
    frame: pd.DataFrame,
    source: str,
    output: Path,
    mode: PriceMode = "hfq",
    universe: pd.Series | None = None,
    allow_gap_placeholders: bool = False,
) -> dict[str, Any]:
    """写 bars.csv 与 manifest.json（quant-research-lab 的 research 命令输入）。

    universe：与 frame 同索引的逐行股票池资格；提供时写入 universe_member 列。
    """
    frame, placeholders = resolve_gaps(frame, allow_gap_placeholders)
    frame, filled = _fill_suspended_volume(frame)
    _require_known(frame, ["is_suspended", "open", "high", "low", "close", "volume"])
    data = _scaled(frame, mode)
    bars = pd.DataFrame(
        {
            "ts_code": data["ts_code"],
            "trade_date": pd.to_datetime(data["trade_date"]).dt.strftime("%Y-%m-%d"),
            "open": data["open"].astype(float),
            "high": data["high"].astype(float),
            "low": data["low"].astype(float),
            "close": data["close"].astype(float),
            "volume": data["volume"].astype(float),
            "available_at": pd.to_datetime(data["trade_date"]).dt.strftime("%Y-%m-%d") + CLOSE_TIME,
            "is_suspended": data["is_suspended"].astype(bool),
            # 未知涨跌停保持空值且不豁免：下游账本据此拒绝成交。
            "up_limit": data["up_limit"].astype(float),
            "down_limit": data["down_limit"].astype(float),
            "price_limit_exempt": data["price_limit_exempt"].astype(bool),
        }
    )
    if universe is not None:
        bars["universe_member"] = universe.reindex(frame.index).astype(bool).to_numpy()
    bars = bars.sort_values(["trade_date", "ts_code"])
    output.mkdir(parents=True, exist_ok=True)
    bars.to_csv(output / "bars.csv", index=False)
    manifest = {
        "kind": "user_supplied",
        "source": f"{source}；价格口径 {mode}；涨跌停由交易所昨收按已核验板块规则计算",
        "historical_universe_complete": False,
        "corporate_actions_complete": False,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "target": "quant-research-lab",
        "price_mode": mode,
        "suspended_volume_filled_zero": filled,
        "universe_member_rows": None if universe is None else int(universe.sum()),
        **export_summary(frame),
        **_placeholder_summary(placeholders),
    }


def _placeholder_summary(placeholders: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "gap_placeholder_rows": sum(len(days) for days in placeholders.values()),
        "gap_placeholders": placeholders,
    }


def to_alpha_evidence_lab(
    frame: pd.DataFrame,
    output: Path,
    mode: PriceMode = "hfq",
    allow_gap_placeholders: bool = False,
) -> dict[str, Any]:
    """每只股票一个 CSV；保留停牌和涨跌停字段，不压缩真实交易时间轴。"""
    frame, placeholders = resolve_gaps(frame, allow_gap_placeholders)
    _require_known(frame, ["is_suspended", "open", "close"])
    data = _scaled(frame, mode)
    output.mkdir(parents=True, exist_ok=True)
    files: dict[str, int] = {}
    dropped: dict[str, int] = {}
    for code, group in data.groupby("ts_code"):
        trading = group.sort_values("trade_date")
        days = pd.to_datetime(trading["trade_date"]).dt.strftime("%Y-%m-%d")
        pd.DataFrame(
            {
                "date": days,
                "open": trading["open"].astype(float),
                "close": trading["close"].astype(float),
                "available_at": days + CLOSE_TIME,
                "is_suspended": trading["is_suspended"].astype(bool),
                "up_limit": trading["up_limit"].astype(float),
                "down_limit": trading["down_limit"].astype(float),
                "price_limit_exempt": trading["price_limit_exempt"].astype(bool),
            }
        ).to_csv(output / f"{code}.csv", index=False)
        files[str(code)] = len(trading)
        dropped[str(code)] = 0
    return {
        "target": "alpha-evidence-lab",
        "price_mode": mode,
        "sessions_per_file": files,
        "suspended_rows_dropped": dropped,
        "note": "同一市场的多只股票相关，不是独立价格路径；独立性需研究者另行论证。",
        **export_summary(frame),
        **_placeholder_summary(placeholders),
    }
