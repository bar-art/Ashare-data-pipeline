# 第一阶段设计与验收边界

用户选择：AKShare 默认；明确标记缺口，先验收框架与小样本。全市场 2015 年至今、完整退市历史不是本阶段的完成声明。

## 目录与选型

`quant_agent/data/`：适配器、标准化、缓存、服务、质量检查；`cli.py`：数据命令；`config/`：规则草案；`tests/`：离线测试；`output/`：报告。`universe/ factors/ backtest/ risk/ agent/ reports/` 为后续阶段预留。

pandas 便于对接 AKShare 的 DataFrame 和金融时序处理；Parquet 保存不可变数据分片，DuckDB 保存查询清单和来源信息，无外部数据库。Python 目标 3.11+；实际测试版本记录于验收报告。后续采用事件驱动撮合，逐单记录 T+1、涨跌停、停牌和未成交原因。

## 接口

- `DataProvider.fetch_daily_bars(code: str, start: date, end: date) -> pd.DataFrame`
- `DataProvider.fetch_calendar() -> pd.DataFrame`
- `DataProvider.fetch_instruments() -> pd.DataFrame`
- `DataService.get_daily_bars(code: str, start: date, end: date, refresh: bool = False) -> pd.DataFrame`
- `quality_report(frame, calendar, start, end) -> dict`

数据流：CLI → DataService → 按 provider/code/日期查询 DuckDB 清单 → 仅请求未缓存区间 → AKShare → 字段验证与标准化 → 不可变 Parquet + 来源清单 → 质量报告。离线 synthetic 数据经过同一条链路，但 provider 和缓存完全隔离。

## 边界

未实现回测、PIT 财务、因子和 LLM。Tushare/BaoStock 为后续适配器，不自动切换数据源。禁止以当前名单回填历史股票池。未知状态保存为空值。交易规则草案禁止直接用于回测，第二阶段再逐项核验完整生效区间。

缓存记录的是请求范围，不是完整市场覆盖证明。空响应不缓存，避免把接口故障永久当作无行情。非空但稀疏响应由质量报告标记，需要显式 refresh 重新下载。获取日期不代表历史可得日期，当前下载数据不构成 PIT 证明。
