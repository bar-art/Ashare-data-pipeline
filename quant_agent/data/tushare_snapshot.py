"""Tushare 本地快照：先把原始接口返回落盘（带 manifest），之后导出全部离线读取。

token 有有效期；落盘后结果可复现，不依赖账号是否仍可用。

快照目录结构：
  manifest.json                     下载区间、来源主机、文件清单
  trade_cal.parquet, stock_basic.parquet
  index_weight_<指数代码>.parquet     月度历史成分与权重
  <接口>/<证券代码>.parquet           daily、adj_factor、stk_limit、suspend_d、namechange

与 BaoStock 的口径差异（已在 bars() 中统一）：
  - daily 不含全天停牌日。按交易日历补行后，逐日用 suspend_d 判断：当天有全天停牌记录才是
    suspended（价格取停牌前收盘、成交量 0）；既无行情也无停牌记录的是 missing，is_suspended
    保持未知，严格导出会拒绝。盘中停牌（有 suspend_timing）当天有成交，按 traded 处理；
    有全天停牌记录却有成交的是 conflict。
  - vol 单位为手、amount 为千元：换算为股和元；
  - ST 由 namechange 的名称区间推断；某日没有任何名称记录时为未知，不默认为否；
  - 停牌行沿用停牌前最后成交日的复权因子（停牌期间除权的因子变化落在复牌日）。
"""

import hashlib
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
STOCK_BASIC_FIELDS = "ts_code,symbol,name,market,exchange,list_status,list_date,delist_date"


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _parse(column: pd.Series) -> pd.Series:
    return pd.to_datetime(column.astype("string"), format="%Y%m%d", errors="coerce")


FORMAT_VERSION = 2
# 单次返回接近接口上限时可能被截断（Tushare 日线类接口单次上限约 6000 行）。
TRUNCATION_ROWS = 5000
# 各接口单次调用空结果的含义：True 表示合法（例如区间内没有停牌、没有暂停上市的证券）；
# False 表示必须报错（例如某个月没有指数权重即为缺月）。
EMPTY_ALLOWED = {
    "trade_cal": False,
    "stock_basic": True,  # 按上市状态分三次查询，单次可为空，合并后不得为空
    "index_weight": False,
    "daily": True,  # 区间内未上市或已退市
    "adj_factor": True,
    "stk_limit": True,
    "suspend_d": True,
    "namechange": True,
}
NONEMPTY_TOTAL = {"trade_cal", "stock_basic", "index_weight"}
DATE_COLUMN = {
    "trade_cal": "cal_date",
    "index_weight": "trade_date",
    "daily": "trade_date",
    "adj_factor": "trade_date",
    "stk_limit": "trade_date",
    "suspend_d": "trade_date",
}
KEYS = {
    "trade_cal": ["cal_date"],
    "stock_basic": ["ts_code"],
    "index_weight": ["trade_date", "con_code"],
    "daily": ["trade_date"],
    "adj_factor": ["trade_date"],
    "stk_limit": ["trade_date"],
    "suspend_d": ["trade_date", "suspend_type"],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def make_plan(
    index_code: str, start: date, end: date, source_label: str, endpoint_digest: str
) -> dict[str, Any]:
    """首次下载前固定的计划；参数变化必须换新目录。不含 token。"""
    return {
        "format_version": FORMAT_VERSION,
        "source_label": source_label,
        "endpoint_digest": endpoint_digest,
        "index_code": index_code,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "datasets": ["trade_cal", "stock_basic", "index_weight", *PER_CODE],
        "stock_basic_fields": STOCK_BASIC_FIELDS,
    }


def check_result(api: str, frame: pd.DataFrame, params: dict[str, Any]) -> list[str]:
    """返回结果的诊断：空表语义、可能截断、重复键、区间外日期。"""
    problems = []
    if frame.empty:
        if not EMPTY_ALLOWED[api]:
            problems.append(f"{api} 返回空表，该接口不允许为空")
        return problems
    if len(frame) >= TRUNCATION_ROWS:
        problems.append(f"{api} 返回 {len(frame)} 行，接近单次上限，可能被截断")
    keys = [key for key in KEYS.get(api, []) if key in frame]
    if keys and len(keys) == len(KEYS[api]) and frame.duplicated(keys).any():
        problems.append(f"{api} 存在重复键 {keys}")
    column = DATE_COLUMN.get(api)
    if column in frame and "start_date" in params:
        dates = frame[column].astype(str)
        outside = (dates < params["start_date"]) | (dates > params["end_date"])
        if outside.any():
            problems.append(f"{api} 返回 {int(outside.sum())} 行请求区间外的日期")
    return problems


class SnapshotLedger:
    """files.jsonl：每个已完成文件的请求、行数、覆盖日期、SHA256 与取得时间（只追加）。"""

    def __init__(self, root: Path) -> None:
        self.path = root / "files.jsonl"
        self.root = root
        self.entries: dict[str, dict[str, Any]] = {}
        # 中断时最后一行可能没写完、没有换行：下一条登记必须另起一行，否则会粘连而丢失。
        self._needs_newline = False
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8")
            self._needs_newline = bool(text) and not text.endswith("\n")
            for line in text.splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # 中断时写了半行：该文件视为未完成，不当作已登记。
                    continue
                self.entries[entry["file"]] = entry

    def record(self, entry: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            if self._needs_newline:
                handle.write("\n")
                self._needs_newline = False
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.entries[entry["file"]] = entry

    def verified(self, relative: str, params: dict[str, Any] | None = None) -> bool:
        """已登记、参数一致且哈希相符返回 True；未登记返回 False；不符直接报错，不覆盖。"""
        path = self.root / relative
        entry = self.entries.get(relative)
        if entry is None:
            if path.exists():
                raise ValueError(f"{relative} 存在但未登记，不会覆盖；请检查后换新目录或执行迁移")
            return False
        if params is not None and entry.get("params") != params:
            raise ValueError(f"{relative} 登记的请求参数与本次计划不一致")
        if not path.exists() or sha256_file(path) != entry["sha256"]:
            raise ValueError(f"{relative} 缺失或哈希不符（可能被修改或写入中断）")
        return True


def download(
    query: Callable[..., pd.DataFrame],
    root: Path,
    index_code: str,
    start: date,
    end: date,
    source: str,
    log: Callable[[str], None] = print,
    endpoint_digest: str = "unspecified",
) -> dict[str, Any]:
    """按固定计划下载；可断点续传，但只跳过已登记且哈希相符的文件。"""
    root.mkdir(parents=True, exist_ok=True)
    plan = make_plan(index_code, start, end, source, endpoint_digest)
    plan_path = root / "plan.json"
    if plan_path.exists():
        existing = json.loads(plan_path.read_text(encoding="utf-8"))
        if existing != plan:
            changed = sorted(key for key in plan if existing.get(key) != plan.get(key))
            raise ValueError(f"快照计划已改变（{changed}）；参数变化请使用新的快照目录")
    elif any(root.iterdir()):
        raise ValueError("目录已有内容但没有 plan.json；旧快照请先执行 snapshot --migrate")
    else:
        _write_json(plan_path, plan)
    manifest = {**plan, "status": "running", "plan_sha256": sha256_file(plan_path)}
    _write_json(root / "manifest.json", manifest)
    ledger = SnapshotLedger(root)
    span = {"start_date": _ymd(start), "end_date": _ymd(end)}

    def fetch(api: str, relative: str, calls: list[dict[str, Any]]) -> pd.DataFrame:
        if ledger.verified(relative, {"calls": calls}):
            return pd.read_parquet(root / relative)
        frames = []
        for params in calls:
            frame = query(api, **params)
            problems = check_result(api, frame, params)
            if problems:
                _write_json(root / "manifest.json", {**manifest, "status": "failed"})
                raise ValueError("；".join(problems) + f"（{relative}）")
            frames.append(frame)
        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if result.empty and api in NONEMPTY_TOTAL:
            _write_json(root / "manifest.json", {**manifest, "status": "failed"})
            raise ValueError(f"{api} 合并后为空（{relative}）")
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        result.to_parquet(temporary, index=False)
        temporary.replace(path)  # 写完才发布，中断不会留下"看似完整"的文件
        column = DATE_COLUMN.get(api)
        dates = result[column].astype(str) if column and column in result else None
        ledger.record(
            {
                "file": relative,
                "api": api,
                "params": {"calls": calls},
                "rows": len(result),
                "first_date": None if dates is None or dates.empty else dates.min(),
                "last_date": None if dates is None or dates.empty else dates.max(),
                "sha256": sha256_file(path),
                "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": "complete" if len(result) else "empty",
            }
        )
        return result

    fetch("trade_cal", "trade_cal.parquet", [{"exchange": "SSE", **span}])
    fetch(
        "stock_basic",
        "stock_basic.parquet",
        [{"list_status": status, "fields": STOCK_BASIC_FIELDS} for status in "LDP"],
    )
    months = pd.period_range(start, end, freq="M")
    weights = fetch(
        "index_weight",
        f"index_weight_{index_code}.parquet",
        [
            {
                "index_code": index_code,
                "start_date": _ymd(m.start_time.date()),
                "end_date": _ymd(min(m.end_time.date(), end)),
            }
            for m in months
        ],
    )
    codes = sorted(set(weights["con_code"].astype(str)))
    for number, code in enumerate(codes, 1):
        for api in PER_CODE:
            params = {"ts_code": code} | ({} if api == "namechange" else span)
            fetch(api, f"{api}/{code}.parquet", [params])
        if number % 50 == 0:
            log(f"{number}/{len(codes)}")
    manifest = {
        **manifest,
        "status": "complete",
        "source": source,
        "index_codes": [index_code],
        "codes": codes,
        "files": len(ledger.entries),
        "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "units": "daily.vol=手, daily.amount=千元（读取时换算为股、元）",
    }
    _write_json(root / "manifest.json", manifest)
    return manifest


def migrate_legacy(root: Path) -> dict[str, Any]:
    """离线迁移旧快照：扫描现有文件生成核验清单，标记 legacy。

    没有原始下载计划与逐文件来源证据：请求参数记为未知，只保证"从迁移时起文件未被改动"。
    """
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("没有 manifest.json，无法识别旧快照")
    if (root / "plan.json").exists() or (root / "files.jsonl").exists():
        raise ValueError("该目录已有 plan.json 或 files.jsonl，不需要迁移")
    old = json.loads(manifest_path.read_text(encoding="utf-8"))
    established = datetime.now().astimezone().isoformat(timespec="seconds")
    plan = {
        "format_version": FORMAT_VERSION,
        "legacy": True,
        "source_label": old.get("source"),
        "endpoint_digest": "unknown_legacy",
        "index_code": (old.get("index_codes") or [None])[0],
        "start": old.get("start"),
        "end": old.get("end"),
        "datasets": old.get("datasets"),
        "stock_basic_fields": STOCK_BASIC_FIELDS,
        "integrity_established_at": established,
    }
    _write_json(root / "plan.json", plan)
    ledger = SnapshotLedger(root)
    for path in sorted(root.rglob("*.parquet")):
        relative = str(path.relative_to(root))
        api = path.parent.name if path.parent != root else path.stem.split("_index")[0]
        api = "index_weight" if path.name.startswith("index_weight_") else api
        frame = pd.read_parquet(path)
        column = DATE_COLUMN.get(api)
        dates = frame[column].astype(str) if column and column in frame else None
        ledger.record(
            {
                "file": relative,
                "api": api,
                "params": "unknown_legacy",
                "rows": len(frame),
                "first_date": None if dates is None or dates.empty else dates.min(),
                "last_date": None if dates is None or dates.empty else dates.max(),
                "sha256": sha256_file(path),
                "retrieved_at": "unknown_legacy",
                "status": "legacy",
            }
        )
    manifest = {
        **old,
        **{key: plan[key] for key in ["format_version", "legacy", "integrity_established_at"]},
        "status": "complete",
        "plan_sha256": sha256_file(root / "plan.json"),
        "files": len(ledger.entries),
    }
    _write_json(manifest_path, manifest)
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
        if self.manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError("旧格式快照没有逐文件核验清单；请先执行 snapshot --migrate")
        if self.manifest.get("status") != "complete":
            raise ValueError(f"快照状态为 {self.manifest.get('status')}，不是 complete")
        self.ledger = SnapshotLedger(root)
        self._checked: set[str] = set()
        self.start = date.fromisoformat(self.manifest["start"])
        self.end = date.fromisoformat(self.manifest["end"])
        self.source = (
            f"{self.manifest['source']}本地快照（{self.manifest['start']}至"
            f"{self.manifest['end']}，下载于{self.manifest['downloaded_at'][:10]}）："
            "daily;adj_factor;suspend_d;namechange;trade_cal;stock_basic;index_weight"
        )

    def _file(self, relative: str) -> pd.DataFrame:
        """读取前核对登记的哈希；未登记、缺失或被改动都停止。"""
        if relative not in self._checked:
            if not self.ledger.verified(relative):
                raise ValueError(f"快照中没有已登记的 {relative}")
            self._checked.add(relative)
        return pd.read_parquet(self.root / relative)

    def _read(self, api: str, code: str) -> pd.DataFrame:
        return self._file(f"{api}/{code}.parquet")

    def fetch_calendar(self) -> pd.DataFrame:
        cal = self._file("trade_cal.parquet")
        days = _parse(cal.loc[cal["is_open"].astype(int) == 1, "cal_date"])
        return pd.DataFrame({"trade_date": days.sort_values().reset_index(drop=True)})

    def fetch_instruments(self) -> pd.DataFrame:
        basic = self._file("stock_basic.parquet")
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
        frame["bar_status"] = self._bar_status(code, frame)
        not_traded = frame["bar_status"] != "traded"
        frame["is_suspended"] = (
            frame["bar_status"].map({"traded": False, "suspended": True}).astype("boolean")
        )
        carried = frame["close"].ffill()
        for column in ["open", "high", "low", "close"]:
            frame[column] = frame[column].fillna(carried)
        # 停牌行昨收即上一交易日（沿用）的收盘价。
        frame["pre_close"] = frame["pre_close"].fillna(carried.shift(1))
        # 确认停牌无成交；缺口的成交量未知，不填 0。
        suspended = frame["bar_status"] == "suspended"
        frame["volume"] = (frame["vol"] * 100).mask(suspended, 0.0)
        frame["amount"] = (frame["amount"] * 1000).mask(suspended, 0.0)

        factors = self._read("adj_factor", code)
        factors = factors.assign(trade_date=_parse(factors["trade_date"]))[
            ["trade_date", "adj_factor"]
        ].drop_duplicates("trade_date")
        frame = frame.merge(factors, on="trade_date", how="left", validate="one_to_one")
        # 停牌行价格是停牌前的原始收盘，必须配停牌前最后成交日的因子：停牌期间除权时，
        # 来源当天已发布新因子，直接相乘会凭空跳涨；因子变化应落在复牌日（原始价已除权）。
        # 长期停牌期间来源也可能不发布因子，同样沿用。
        traded = frame["adj_factor"].where(~not_traded).ffill()
        frame["adj_factor"] = frame["adj_factor"].where(~not_traded, traded)
        frame["is_st"] = st_flags(self._read("namechange", code), frame["trade_date"]).to_numpy()
        frame = frame[frame["trade_date"] >= pd.Timestamp(start)]
        keep = ["trade_date", "open", "high", "low", "close", "pre_close", "volume", "amount"]
        extra = ["adj_factor", "is_st", "is_suspended", "bar_status"]
        return normalize(frame[[*keep, *extra]], code)

    def _bar_status(self, code: str, frame: pd.DataFrame) -> pd.Series:
        """逐日状态：有成交为 traded；无成交时只有当天的全天停牌记录才算 suspended。"""
        events = self._read("suspend_d", code)
        full_day: set[pd.Timestamp] = set()
        if {"trade_date", "suspend_type"} <= set(events.columns) and not events.empty:
            timing = (
                events["suspend_timing"].astype("string").str.strip()
                if "suspend_timing" in events
                else pd.Series(pd.NA, index=events.index, dtype="string")
            )
            whole = timing.isna() | timing.isin(["", "None", "nan"])
            stopped = (events["suspend_type"] == "S") & whole
            full_day = set(_parse(events.loc[stopped, "trade_date"]).dropna())
        traded = frame["close"].notna()
        confirmed = frame["trade_date"].isin(full_day)
        status = pd.Series("missing", index=frame.index, dtype="string")
        status[traded] = "traded"
        status[~traded & confirmed] = "suspended"
        status[traded & confirmed] = "conflict"
        return status

    def fetch_index_members(self, index: str, as_of: date) -> pd.DataFrame:
        """as_of 当日或之前最近一次已发布的成分（不使用之后的月末权重）。"""
        if index not in INDEXES:
            raise ValueError(f"Tushare 快照目前只包含 {sorted(INDEXES)}")
        weights = self._file(f"index_weight_{INDEXES[index]}.parquet")
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
