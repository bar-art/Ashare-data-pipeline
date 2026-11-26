"""适配器与请求节流；离线样本不代表真实市场。"""

import importlib
import inspect
import time
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol, TypeVar, cast

import pandas as pd

from quant_agent.data.schema import normalize

T = TypeVar("T")


class DataProvider(Protocol):
    name: str
    source: str

    def fetch_daily_bars(self, code: str, start: date, end: date) -> pd.DataFrame: ...
    def fetch_calendar(self) -> pd.DataFrame: ...
    def fetch_instruments(self) -> pd.DataFrame: ...


class RequestGate:
    def __init__(
        self,
        interval: float = 1,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval, self.attempts = interval, attempts
        self.sleep, self.clock = sleep, clock
        self.last: float | None = None

    def call(self, action: Callable[[], T]) -> T:
        for attempt in range(self.attempts):
            if self.last is not None:
                self.sleep(max(0, self.interval - (self.clock() - self.last)))
            self.last = self.clock()
            try:
                return action()
            except (OSError, TimeoutError) as error:
                # 上游明确限流时停止，避免反复请求延长封禁。
                if any(cls.__name__ == "RateLimitError" for cls in type(error).__mro__):
                    raise
                if attempt + 1 == self.attempts:
                    raise
                self.sleep(2**attempt)
        raise RuntimeError("请求次数配置无效")


class AKShareProvider:
    name = "akshare"
    source = "AKShare/stock_zh_a_hist_em;calendar_trade_date_hist_sina;stock_info_a_code_name"

    def __init__(self, gate: RequestGate | None = None, timeout: float = 15) -> None:
        self.gate = gate or RequestGate()
        self.timeout = timeout

    def _api(self) -> Any:
        api = importlib.import_module("akshare")
        # 本项目为单进程 CLI；新版统一 HTTP 客户端从这里读取超时。
        if hasattr(api, "set_timeout"):
            api.set_timeout(self.timeout)
        return api

    def fetch_daily_bars(self, code: str, start: date, end: date) -> pd.DataFrame:
        api = self._api()
        # 新版接口增加来源后缀，兼容旧版名称；绝不静默换数据源。
        function = getattr(api, "stock_zh_a_hist_em", None) or api.stock_zh_a_hist
        kwargs: dict[str, Any] = {
            "symbol": code[:6],
            "period": "daily",
            "start_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
            "adjust": "",
        }
        if "timeout" in inspect.signature(function).parameters:
            kwargs["timeout"] = self.timeout
        raw = self.gate.call(lambda: function(**kwargs))
        mapped = raw.rename(
            columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        # 官方接口成交量单位为手，统一为股；不使用涨跌额倒推昨收。
        if "volume" in mapped:
            mapped["volume"] = pd.to_numeric(mapped["volume"], errors="raise") * 100
        return normalize(mapped, code)

    def fetch_calendar(self) -> pd.DataFrame:
        api = self._api()
        function = (
            getattr(api, "calendar_trade_date_hist_sina", None) or api.tool_trade_date_hist_sina
        )
        return cast(pd.DataFrame, self.gate.call(function))

    def fetch_instruments(self) -> pd.DataFrame:
        raw = self.gate.call(self._api().stock_info_a_code_name)
        raw = raw.rename(columns={"code": "symbol", "name": "name"}).copy()
        raw["coverage"] = "current_snapshot_only_not_historical_universe"
        return cast(pd.DataFrame, raw)


class SyntheticProvider:
    name = "synthetic"
    source = "人工构造的测试数据；不是历史行情；仅限2024-01-02至2024-01-05"

    def fetch_calendar(self) -> pd.DataFrame:
        return pd.DataFrame({"trade_date": pd.date_range("2024-01-02", "2024-01-05")})

    def fetch_daily_bars(self, code: str, start: date, end: date) -> pd.DataFrame:
        dates = self.fetch_calendar()["trade_date"]
        frame = pd.DataFrame(
            {
                "trade_date": dates,
                "open": [10.0, 11.0, 12.0, 13.0],
                "high": [11.0, 12.0, 13.0, 14.0],
                "low": [9.0, 10.0, 11.0, 12.0],
                "close": [10.5, 11.5, 12.5, 13.5],
                "volume": 10000.0,
                "amount": [105000.0, 115000.0, 125000.0, 135000.0],
            }
        )
        frame = frame[frame.trade_date.between(pd.Timestamp(start), pd.Timestamp(end))]
        return normalize(frame, code)

    def fetch_instruments(self) -> pd.DataFrame:
        return pd.DataFrame(
            {"symbol": ["600000"], "name": ["人工样本"], "coverage": ["synthetic_only"]}
        )


class BaoStockProvider:
    """BaoStock 日线：交易所昨收（除权参考价）、停牌状态、历史ST标记与后复权因子。

    停牌日由来源以 tradestatus=0 的行给出（价格为停牌前收盘、成交量0），不是由缺失日期推断。
    北交所不在 BaoStock 覆盖范围内，请求时直接拒绝。
    """

    name = "baostock"
    source = (
        "BaoStock/query_history_k_data_plus(adjustflag=3不复权与1后复权);"
        "query_trade_dates;query_stock_basic"
    )
    FIELDS = "date,code,open,high,low,close,preclose,volume,amount,tradestatus,isST"

    def __init__(self, gate: RequestGate | None = None) -> None:
        self.gate = gate or RequestGate()

    def _api(self) -> Any:
        return importlib.import_module("baostock")

    def _query(self, action: Callable[[Any], Any]) -> pd.DataFrame:
        api = self._api()

        def run() -> pd.DataFrame:
            login = api.login()
            if str(login.error_code) != "0":
                raise ConnectionError(f"BaoStock 登录失败：{login.error_code}")
            try:
                result = action(api)
                if str(result.error_code) != "0":
                    raise ValueError(f"BaoStock 查询失败：{result.error_code}")
                rows = []
                while result.next():
                    rows.append(result.get_row_data())
                return pd.DataFrame(rows, columns=result.fields)
            finally:
                api.logout()

        return self.gate.call(run)

    @staticmethod
    def symbol(code: str) -> str:
        number, exchange = code.split(".")
        if exchange == "BJ":
            raise ValueError("BaoStock 不覆盖北交所证券")
        return f"{exchange.lower()}.{number}"

    def _bars(self, code: str, start: date, end: date, adjust: str) -> pd.DataFrame:
        return self._query(
            lambda api: api.query_history_k_data_plus(
                self.symbol(code),
                self.FIELDS,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag=adjust,
            )
        )

    def fetch_daily_bars(self, code: str, start: date, end: date) -> pd.DataFrame:
        raw = self._bars(code, start, end, "3")
        if raw.empty:
            return raw.assign(
                trade_date=[], open=[], high=[], low=[], close=[], volume=[], amount=[]
            )
        adjusted = self._bars(code, start, end, "1")[["date", "close"]].rename(
            columns={"close": "hfq_close"}
        )
        merged = raw.merge(adjusted, on="date", how="left", validate="one_to_one")
        # 空字符串是来源缺失，不能当作0或"否"。
        merged = merged.replace("", pd.NA)
        numbers = ["open", "high", "low", "close", "preclose", "volume", "amount", "hfq_close"]
        for column in numbers:
            merged[column] = pd.to_numeric(merged[column], errors="raise")
        frame = merged.rename(columns={"date": "trade_date", "preclose": "pre_close"})
        frame["adj_factor"] = frame["hfq_close"] / frame["close"]
        status = {"1": False, "0": True}
        frame["is_suspended"] = frame["tradestatus"].map(status).astype("boolean")
        frame["is_st"] = frame["isST"].map({"1": True, "0": False}).astype("boolean")
        return normalize(frame.drop(columns=["code", "tradestatus", "isST", "hfq_close"]), code)

    def fetch_calendar(self) -> pd.DataFrame:
        raw = self._query(
            lambda api: api.query_trade_dates(
                start_date="1990-12-19", end_date=date.today().isoformat()
            )
        )
        days = raw[raw["is_trading_day"] == "1"]
        return pd.DataFrame({"trade_date": pd.to_datetime(days["calendar_date"])})

    def fetch_instruments(self) -> pd.DataFrame:
        raw = self._query(lambda api: api.query_stock_basic())
        frame = raw.rename(
            columns={
                "code": "symbol",
                "code_name": "name",
                "ipoDate": "ipo_date",
                "outDate": "out_date",
            }
        ).copy()
        parts = frame["symbol"].astype(str).str.split(".", n=1, expand=True)
        frame["symbol"] = parts[1] + "." + parts[0].str.upper()
        frame["coverage"] = "baostock_stock_basic_includes_delisted_not_index_membership"
        return frame

    def fetch_index_members(self, index: str, as_of: date) -> pd.DataFrame:
        """Source snapshot for one explicit historical date; never substitutes today's list."""
        names = {
            "hs300": "query_hs300_stocks",
            "zz500": "query_zz500_stocks",
            "sz50": "query_sz50_stocks",
        }
        if index not in names:
            raise ValueError("指数仅支持 hs300、zz500、sz50")
        raw = self._query(lambda api: getattr(api, names[index])(date=as_of.isoformat()))
        required = {"updateDate", "code", "code_name"}
        if raw.empty or required - set(raw):
            raise ValueError("历史成分返回空或缺字段；不得回退到当前成分")
        updates = pd.to_datetime(raw.updateDate, errors="raise")
        if updates.isna().any() or (updates > pd.Timestamp(as_of)).any():
            raise ValueError("来源更新日期晚于查询日或未知，拒绝未来成分")
        if not raw.code.str.fullmatch(r"(?:sh|sz)\.\d{6}").all() or raw.code.duplicated().any():
            raise ValueError("成分代码非法或重复")
        result = raw.rename(
            columns={"code_name": "name", "updateDate": "source_update_date"}
        ).copy()
        result["symbol"] = raw.code.str[3:] + "." + raw.code.str[:2].str.upper()
        result["snapshot_date"] = as_of.isoformat()
        result["index"] = index
        result["coverage"] = "source_historical_snapshot_not_original_release_vintage"
        return result.drop(columns="code")
