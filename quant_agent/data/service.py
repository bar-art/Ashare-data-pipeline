"""数据请求范围与实际覆盖分开记录。"""

from datetime import date

import pandas as pd

from quant_agent.data.cache import LocalCache
from quant_agent.data.providers import DataProvider
from quant_agent.data.schema import normalize, validate_request


class DataService:
    def __init__(self, provider: DataProvider, cache: LocalCache) -> None:
        self.provider, self.cache = provider, cache

    def get_daily_bars(
        self, code: str, start: date, end: date, refresh: bool = False
    ) -> pd.DataFrame:
        validate_request(code, start, end)
        gaps = [(start, end)] if refresh else self.cache.gaps(self.provider.name, code, start, end)
        for left, right in gaps:
            frame = normalize(self.provider.fetch_daily_bars(code, left, right), code)
            if frame.empty:
                raise ValueError("接口返回空行情；未写入缓存，请核对代码、上市区间和数据源状态")
            if not frame.trade_date.between(pd.Timestamp(left), pd.Timestamp(right)).all():
                raise ValueError("接口返回超出请求区间的行情")
            self.cache.put(
                self.provider.name, "bars", code, left, right, frame, self.provider.source
            )
        cached = self.cache.read(self.provider.name, "bars", code)
        if cached is None:
            raise ValueError("没有可用行情")
        frame = normalize(cached, code)
        return frame[frame.trade_date.between(pd.Timestamp(start), pd.Timestamp(end))].reset_index(
            drop=True
        )

    def get_calendar(self, refresh: bool = False) -> pd.DataFrame:
        cached = None if refresh else self.cache.read(self.provider.name, "calendar", "")
        if cached is not None:
            return cached
        frame = self.provider.fetch_calendar().copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise").dt.normalize()
        if frame.empty or frame.trade_date.isna().any() or frame.trade_date.duplicated().any():
            raise ValueError("交易日历为空、日期缺失或存在重复日期")
        frame = frame.sort_values("trade_date").reset_index(drop=True)
        self.cache.put(
            self.provider.name,
            "calendar",
            "",
            frame.trade_date.min().date(),
            frame.trade_date.max().date(),
            frame,
            self.provider.source,
        )
        return frame

    def get_instruments(self, refresh: bool = False) -> pd.DataFrame:
        cached = None if refresh else self.cache.read(self.provider.name, "instruments", "")
        if cached is not None:
            return cached
        frame = self.provider.fetch_instruments()
        if frame.empty or not {"symbol", "name", "coverage"}.issubset(frame.columns):
            raise ValueError("证券快照缺失必要字段")
        self.cache.put(
            self.provider.name,
            "instruments",
            "",
            date.today(),
            date.today(),
            frame,
            self.provider.source,
        )
        return frame
