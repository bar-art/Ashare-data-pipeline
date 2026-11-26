"""检查 Tushare 连接：token 与可选接口地址从 .env 读取；不打印 token。"""

from datetime import date

import tushare as ts

from quant_agent.config import Settings
from quant_agent.data.tushare_source import TushareSource

settings = Settings()
pro = ts.pro_api(settings.tushare_token.get_secret_value())
if settings.tushare_http_url:
    pro._DataApi__http_url = settings.tushare_http_url

print("1. 到期时间：")
print(pro.token_expire())
print("2. 常规接口 index_basic：")
print(pro.index_basic(limit=5))
print("3. pro_bar（需传入 api=pro）：")
print(ts.pro_bar(api=pro, ts_code="000001.SZ", limit=3))

print("4. 本项目核对用的三个接口（600518.SH 2019-05-17 至 2019-05-23）：")
source = TushareSource(settings.tushare_token.get_secret_value(), settings.tushare_http_url)
for name in ["daily", "stk_limit", "suspend_d"]:
    frame = getattr(source, name)("600518.SH", date(2019, 5, 17), date(2019, 5, 23))
    print(name, frame.shape)
    print(frame.head(3).to_string())
