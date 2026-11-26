"""Yahoo Finance 日线（通过 yfinance）：作为第二来源核对价格、停牌、公司行动日期和涨跌停规则。

Yahoo 的开高低收已按送转（Yahoo 记作拆股）向前调整；分红不调整。核对时按其后发生的拆股比例还原为
原始价格。Yahoo 不提供昨收、ST、涨跌停价，不能替代 BaoStock；对 A 股也可能用前值填充无成交日。
数据仅供个人研究，遵守 Yahoo 使用条款，不再分发原始数据。
"""

import importlib
from datetime import date, timedelta

import pandas as pd

from quant_agent.data.providers import RequestGate

SUFFIX = {"SH": "SS", "SZ": "SZ"}


class YahooSource:
    name = "yahoo"
    source = "Yahoo Finance 日线（yfinance，auto_adjust=False，含分红与拆股）"

    def __init__(self, gate: RequestGate | None = None) -> None:
        self.gate = gate or RequestGate()

    @staticmethod
    def symbol(code: str) -> str:
        number, exchange = code.split(".")
        if exchange not in SUFFIX:
            raise ValueError("Yahoo 核对目前只支持沪深证券")
        return f"{number}.{SUFFIX[exchange]}"

    def history(self, code: str, start: date, end: date) -> pd.DataFrame:
        api = importlib.import_module("yfinance")
        raw = self.gate.call(
            lambda: api.Ticker(self.symbol(code)).history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),  # yfinance 的 end 不含当日
                auto_adjust=False,
                actions=True,
            )
        )
        if not isinstance(raw, pd.DataFrame):
            raise ValueError("Yahoo 返回的不是表格")
        if raw.empty:
            return pd.DataFrame(
                columns=[
                    "trade_date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "adj_close",
                    "volume",
                    "dividends",
                    "splits",
                ]
            )
        frame = raw.reset_index().rename(
            columns={
                "Date": "trade_date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Adj Close": "adj_close",
                "Volume": "volume",
                "Dividends": "dividends",
                "Stock Splits": "splits",
            }
        )
        # 交易所本地日期；去掉时区后再存储，避免跨时区移位。
        frame["trade_date"] = (
            pd.to_datetime(frame["trade_date"]).dt.tz_localize(None).dt.normalize()
        )
        columns = ["open", "high", "low", "close", "adj_close", "volume", "dividends", "splits"]
        for column in columns:
            if column not in frame:
                frame[column] = 0.0
            frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
        return frame[["trade_date", *columns]].sort_values("trade_date").reset_index(drop=True)
