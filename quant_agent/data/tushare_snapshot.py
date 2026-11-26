"""Tushare 本地快照：先把原始接口返回落盘（带 manifest），之后导出全部离线读取。

token 有有效期；落盘后结果可复现，不依赖账号是否仍可用。

快照目录结构：
  manifest.json                     下载区间、来源主机、文件清单
  trade_cal.parquet, stock_basic.parquet
  index_weight_<指数代码>.parquet     月度历史成分与权重
  <接口>/<证券代码>.parquet           daily、adj_factor、stk_limit、suspend_d、namechange

与 BaoStock 的口径差异（已在 bars() 中统一）：
  - daily 不含全天停牌日：按交易日历补行，价格取停牌前收盘，成交量 0，is_suspended=True；
  - vol 单位为手、amount 为千元：换算为股和元；
  - ST 由 namechange 的名称区间推断；某日没有任何名称记录时为未知，不默认为否；
  - 停牌行沿用停牌前最后成交日的复权因子（停牌期间除权的因子变化落在复牌日）。
"""

import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quant_agent.data.schema import normalize

PER_CODE = ["daily", "adj_factor", "stk_limit", "suspend_d", "namechange"]
INDEXES = {"hs300": "399300.SZ"}
ST_PREFIXES = ("ST", "*ST", "S*ST", "SST")


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _parse(column: pd.Series) -> pd.Series:
    return pd.to_datetime(column.astype("string"), format="%Y%m%d", errors="coerce")


def download(
    query: Callable[..., pd.DataFrame],
    root: Path,
    index_code: str,
    start: date,
    end: date,
    source: str,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """下载指数月度成分及全部出现过的成分股原始数据；已存在的文件跳过（可断点续传）。"""
    root.mkdir(parents=True, exist_ok=True)
    span = {"start_date": _ymd(start), "end_date": _ymd(end)}

    def save(frame: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)

    if not (root / "trade_cal.parquet").exists():
        save(query("trade_cal", exchange="SSE", **span), root / "trade_cal.parquet")
    if not (root / "stock_basic.parquet").exists():
        fields = "ts_code,symbol,name,market,exchange,list_status,list_date,delist_date"
        basic = [query("stock_basic", list_status=s, fields=fields) for s in "LDP"]
        save(pd.concat(basic, ignore_index=True), root / "stock_basic.parquet")
    weights_path = root / f"index_weight_{index_code}.parquet"
    if not weights_path.exists():
        months = pd.period_range(start, end, freq="M")
        frames = [
            query(
                "index_weight",
                index_code=index_code,
                start_date=_ymd(m.start_time.date()),
                end_date=_ymd(min(m.end_time.date(), end)),
            )
            for m in months
        ]
        save(pd.concat(frames, ignore_index=True), weights_path)
    codes = sorted(set(pd.read_parquet(weights_path)["con_code"].astype(str)))
    for number, code in enumerate(codes, 1):
        for api in PER_CODE:
            path = root / api / f"{code}.parquet"
            if not path.exists():
                params = {"ts_code": code} | ({} if api == "namechange" else span)
                save(query(api, **params), path)
        if number % 50 == 0:
            log(f"{number}/{len(codes)}")
    manifest = {
        "source": source,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "index_codes": [index_code],
        "codes": codes,
        "datasets": ["trade_cal", "stock_basic", "index_weight", *PER_CODE],
        "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "units": "daily.vol=手, daily.amount=千元（读取时换算为股、元）",
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def st_flags(names: pd.DataFrame, days: pd.Series) -> pd.Series:
    """每个交易日适用的证券简称是否为 ST；没有覆盖该日的名称记录时为 NA。"""
    result = pd.Series(pd.NA, index=days.index, dtype="boolean")
    if names.empty:
        return result
    spans = names.assign(start=_parse(names["start_date"]), stop=_parse(names["end_date"]))
    spans = spans.dropna(subset=["start"]).sort_values("start")
    for begin, stop, name in zip(spans["start"], spans["stop"], spans["name"], strict=True):
        covered = days >= begin
        if not pd.isna(stop):
            covered &= days <= stop
        # 按开始日期排序后覆盖：区间重叠时以较晚生效的名称为准。
        result[covered] = str(name).upper().startswith(ST_PREFIXES)
    return result


class TushareSnapshotProvider:
    """离线读取 download() 生成的快照，输出与 BaoStockProvider 相同的标准化字段。"""

    name = "tushare"

    def __init__(self, root: Path) -> None:
        manifest = root / "manifest.json"
        if not manifest.exists():
            raise ValueError(f"Tushare 快照缺少 manifest.json：{root}；先运行 snapshot 命令")
        self.root = root
        self.manifest = json.loads(manifest.read_text(encoding="utf-8"))
        self.start = date.fromisoformat(self.manifest["start"])
        self.end = date.fromisoformat(self.manifest["end"])
        self.source = (
            f"{self.manifest['source']}本地快照（{self.manifest['start']}至"
            f"{self.manifest['end']}，下载于{self.manifest['downloaded_at'][:10]}）："
            "daily;adj_factor;suspend_d;namechange;trade_cal;stock_basic;index_weight"
        )

    def _read(self, api: str, code: str) -> pd.DataFrame:
        path = self.root / api / f"{code}.parquet"
        if not path.exists():
            raise ValueError(f"快照中没有 {code} 的 {api}")
        return pd.read_parquet(path)

    def fetch_calendar(self) -> pd.DataFrame:
        cal = pd.read_parquet(self.root / "trade_cal.parquet")
        days = _parse(cal.loc[cal["is_open"].astype(int) == 1, "cal_date"])
        return pd.DataFrame({"trade_date": days.sort_values().reset_index(drop=True)})

    def fetch_instruments(self) -> pd.DataFrame:
        basic = pd.read_parquet(self.root / "stock_basic.parquet")
        return pd.DataFrame(
            {
                "symbol": basic["ts_code"],
                "name": basic["name"],
                "ipo_date": _parse(basic["list_date"]).dt.strftime("%Y-%m-%d"),
                "out_date": _parse(basic["delist_date"]).dt.strftime("%Y-%m-%d"),
                "coverage": "tushare_stock_basic_listed_delisted_paused",
            }
        )

    def fetch_daily_bars(self, code: str, start: date, end: date) -> pd.DataFrame:
        if start < self.start or end > self.end:
            raise ValueError(f"请求区间超出快照范围 {self.start} 至 {self.end}")
        daily = self._read("daily", code)
        empty = pd.DataFrame(columns=["trade_date", "open", "high", "low", "close", "volume"])
        if daily.empty:
            return empty.assign(amount=[])
        daily = daily.assign(trade_date=_parse(daily["trade_date"]))
        if daily["trade_date"].duplicated().any():
            raise ValueError(f"{code} daily 存在重复日期")
        instruments = self.fetch_instruments()
        out_date = instruments.loc[instruments["symbol"] == code, "out_date"].dropna()
        first = daily["trade_date"].min()
        last = daily["trade_date"].max()
        if not out_date.empty:
            # 最后成交到退市日（含）之间的交易日记为停牌，与 BaoStock 和质量检查的口径一致。
            last = pd.Timestamp(str(out_date.iloc[0]))
        calendar = self.fetch_calendar()["trade_date"]
        # 从快照内首个成交日开始构建，最后再截取请求区间：区间起点处于停牌时，
        # 停牌行也能取到停牌前真实的收盘价和因子。
        days = calendar[calendar.between(first, min(last, pd.Timestamp(end)))]
        frame = pd.DataFrame({"trade_date": days.to_numpy()}).merge(
            daily, on="trade_date", how="left", validate="one_to_one"
        )
        frame["is_suspended"] = frame["close"].isna()
        carried = frame["close"].ffill()
        for column in ["open", "high", "low", "close"]:
            frame[column] = frame[column].fillna(carried)
        # 停牌行昨收即上一交易日（沿用）的收盘价。
        frame["pre_close"] = frame["pre_close"].fillna(carried.shift(1))
        frame["volume"] = (frame["vol"] * 100).fillna(0.0)
        frame["amount"] = (frame["amount"] * 1000).fillna(0.0)

        factors = self._read("adj_factor", code)
        factors = factors.assign(trade_date=_parse(factors["trade_date"]))[
            ["trade_date", "adj_factor"]
        ].drop_duplicates("trade_date")
        frame = frame.merge(factors, on="trade_date", how="left", validate="one_to_one")
        # 停牌行价格是停牌前的原始收盘，必须配停牌前最后成交日的因子：停牌期间除权时，
        # 来源当天已发布新因子，直接相乘会凭空跳涨；因子变化应落在复牌日（原始价已除权）。
        # 长期停牌期间来源也可能不发布因子，同样沿用。
        traded = frame["adj_factor"].where(~frame["is_suspended"]).ffill()
        frame["adj_factor"] = frame["adj_factor"].where(~frame["is_suspended"], traded)
        frame["is_st"] = st_flags(self._read("namechange", code), frame["trade_date"]).to_numpy()
        frame = frame[frame["trade_date"] >= pd.Timestamp(start)]
        keep = ["trade_date", "open", "high", "low", "close", "pre_close", "volume", "amount"]
        return normalize(frame[[*keep, "adj_factor", "is_st", "is_suspended"]], code)

    def fetch_index_members(self, index: str, as_of: date) -> pd.DataFrame:
        """as_of 当日或之前最近一次已发布的成分（不使用之后的月末权重）。"""
        if index not in INDEXES:
            raise ValueError(f"Tushare 快照目前只包含 {sorted(INDEXES)}")
        path = self.root / f"index_weight_{INDEXES[index]}.parquet"
        if not path.exists():
            raise ValueError(f"快照中没有 {index} 的历史成分")
        weights = pd.read_parquet(path)
        weights = weights.assign(published=_parse(weights["trade_date"]))
        known = weights[weights["published"] <= pd.Timestamp(as_of)]
        if known.empty:
            raise ValueError(f"{as_of} 之前没有 {index} 成分记录；不得回退到之后的成分")
        latest = known[known["published"] == known["published"].max()]
        if latest["con_code"].duplicated().any():
            raise ValueError("成分代码重复")
        return pd.DataFrame(
            {
                "symbol": latest["con_code"].astype(str).to_numpy(),
                "weight": latest["weight"].to_numpy(),
                "source_update_date": latest["published"].dt.strftime("%Y-%m-%d").to_numpy(),
                "snapshot_date": as_of.isoformat(),
                "index": index,
                "coverage": "tushare_index_weight_month_end",
            }
        )
