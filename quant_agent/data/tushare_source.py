"""Tushare 接口：每日涨跌停价（stk_limit）、停复牌（suspend_d）与日线（daily），用于交叉核对。

默认访问 tushare.pro；设置 TUSHARE_HTTP_URL 时改用该地址。来源统一记为 tushare，
来源说明只区分官方或第三方转发，不写入转发地址（地址属于本机配置，不进入导出文件）。
"""

import importlib
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from quant_agent.data.providers import RequestGate


class TushareSource:
    def __init__(self, token: str, http_url: str = "", gate: RequestGate | None = None) -> None:
        if not token.strip():
            raise ValueError("需要在 .env 中设置 TUSHARE_TOKEN")
        self.token = token
        self.http_url = http_url.strip()
        self.gate = gate or RequestGate()
        self.name = "tushare"
        channel = "官方"
        if self.http_url:
            parts = urlsplit(self.http_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("TUSHARE_HTTP_URL 必须是 http(s)://主机:端口")
            if parts.username or parts.password or parts.query:
                raise ValueError("TUSHARE_HTTP_URL 不得包含凭据或查询参数")
            channel = (
                "官方" if parts.hostname.endswith(("tushare.pro", "waditu.com")) else "第三方转发"
            )
        self.source = f"Tushare Pro 接口（{channel}）"

    def _pro(self) -> Any:
        api = importlib.import_module("tushare").pro_api(self.token)
        if self.http_url:
            # tushare SDK 没有公开的地址参数，只能改写这个内部属性。
            api._DataApi__http_url = self.http_url
        return api

    def query(self, api_name: str, **params: Any) -> pd.DataFrame:
        result = self.gate.call(lambda: getattr(self._pro(), api_name)(**params))
        if not isinstance(result, pd.DataFrame):
            raise ValueError(f"{api_name} 返回的不是表格")
        return result

    @staticmethod
    def _span(start: date, end: date) -> dict[str, str]:
        return {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}

    def stk_limit(self, code: str, start: date, end: date) -> pd.DataFrame:
        return self.query("stk_limit", ts_code=code, **self._span(start, end))

    def suspend_d(self, code: str, start: date, end: date) -> pd.DataFrame:
        return self.query("suspend_d", ts_code=code, **self._span(start, end))

    def daily(self, code: str, start: date, end: date) -> pd.DataFrame:
        return self.query("daily", ts_code=code, **self._span(start, end))
