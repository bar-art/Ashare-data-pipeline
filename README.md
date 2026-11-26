# A-share Quant Research Agent

面向 A 股的量化研究项目。当前完成范围为**第一阶段：数据基础设施与小样本**。后续分阶段增加历史股票池、PIT、回测、因子、风险报告和 LLM。仅供研究，不构成投资建议。

## 安装（Python 3.11+）

```bash
cd a-share-quant-agent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
cp .env.example .env
```

版本锁见 `requirements.lock`；测试工具也包含在内。不要提交 `.env`；本阶段无需任何 token。`DATA_DIR` 指定本地 Parquet/DuckDB 目录。请求间隔、重试次数、连接/读取超时均可配置（新版通过 AKShare 统一 HTTP 配置设置）；重试仅覆盖网络类异常，字段错误直接失败。

## 初始化数据与报告

先跑完全离线的人工样本（不是真实历史业绩）：

```bash
quant-agent fetch --provider synthetic --start 2024-01-02 --end 2024-01-05
quant-agent calendar --provider synthetic
quant-agent instruments --provider synthetic
```

默认 AKShare，需要能够访问国内数据源，不需要海外 API：

```bash
quant-agent fetch --code 600000.SH --start 2024-01-02 --end 2024-01-05
quant-agent calendar
quant-agent instruments
```

`output/` 包含行情 CSV、质量报告 Markdown/JSON 和数据版本清单；`data/` 保存 Parquet 与 DuckDB。重复请求命中缓存，扩展日期只请求未缓存区间。`--refresh` 显式重新抓取，旧版本保留。数据质量问题会写报告，命令成功只表示流水线执行成功，不表示所有检查通过。

交易日历和股票名单独立缓存；首次 fetch 也获取日历。不要把当前证券名单用于历史回测。日线缺少的 pre_close、复权因子、ST、停牌和涨跌停价保留空值并报告。完整清单见 [覆盖说明](docs/data_coverage.md)。

## 补齐历史字段并导出到研究项目

BaoStock（免费，无需 token，国内服务）逐日提供交易所昨收（除权参考价）、停牌状态（tradestatus）、历史 ST 标记；同时取不复权和后复权收盘价得到复权因子。涨跌停价不由来源提供，而是由昨收按已核验的板块规则计算（`quant_agent/data/rules.py`），并逐日检查实际最高/最低价没有超出；超出即判定规则不适用、改为未知。规则覆盖不到的情形（如2023-04-10前主板新股上市首日按发行价计算）一律留空且不豁免，下游回测据此拒绝成交。

```bash
# quant-research-lab：bars.csv + manifest.json
quant-agent export --provider baostock --codes 600000.SH 000001.SZ 300750.SZ \
  --start 2020-01-02 --end 2024-12-31 --target quant-research-lab --output output/qrl_input
# alpha-evidence-lab：每只股票一个 CSV（停牌日移除）
quant-agent export --provider baostock --codes 600000.SH 000001.SZ 300750.SZ 600519.SH 688981.SH \
  --start 2020-01-02 --end 2024-12-31 --target alpha-evidence-lab --output output/alpha_input
```

- 默认 `--price-mode hfq`：价格和涨跌停价同乘后复权因子，除权日不产生虚假亏损；`raw` 输出交易所原始价格。
- `available_at` 记为交易日 15:00（上海时间），与两个下游项目的收盘后决策时点一致；这是收盘价可得的假设，不是来源发布时间证明。
- 停牌行成交量为空时按 0 导出（来源已标记停牌、无成交）；其他未知字段一律停止导出。
- 北交所不在 BaoStock 覆盖范围内。股票池需自行指定，导出 manifest 声明 `historical_universe_complete: false`。
- 每次导出写 `export_report.json`：行数、停牌/ST/豁免行、各涨跌停规则计数、质量问题和缓存版本。

实测记录见 [字段补齐验收](docs/phase2_fields_acceptance.md)。

## Tushare 本地快照（沪深300历史成分 2015-2026）

BaoStock 使用 10030 端口，部分网络会拦截。Tushare 走 HTTP，能提供月度历史指数成分（`index_weight`）。token 有有效期，所以先把原始返回整体落盘，之后全部离线读取，结果不依赖账号是否仍然可用：

```bash
# 1. 联网一次：月度成分 + 所有出现过的成分股的 daily/adj_factor/stk_limit/suspend_d/namechange（可断点续传）
quant-agent snapshot --index hs300 --start 2014-01-01 --end 2026-11-24
# 2. 之后离线：成分快照与导出
quant-agent universe --provider tushare --index hs300 --monthly \
  --start 2015-01-01 --end 2026-11-24 --output output/hs300-tushare
quant-agent export --provider tushare --start 2015-01-05 --end 2026-11-24 \
  --universe-file output/hs300-tushare/hs300_historical_members.csv \
  --target quant-research-lab --output output/qrl_hs300_tushare
```

- 需要在 `.env` 设置 `TUSHARE_TOKEN`，可选 `TUSHARE_HTTP_URL`。快照目录由 `TUSHARE_SNAPSHOT_DIR` 指定（默认 `data/tushare_raw`），不提交到仓库。来源说明只记“官方/第三方转发”，不写入转发地址。
- 口径统一为 BaoStock 字段：daily 不含全天停牌日，按交易日历补停牌行（价格沿用停牌前收盘，成交量 0），一直补到退市日（含）；成交量由手换算为股，成交额由千元换算为元；ST 由 `namechange` 的名称区间推断，没有名称记录的日子为未知；停牌行沿用停牌前最后成交日的复权因子：停牌期间除权时来源当天就发布新因子，直接相乘会让后复权价在无成交时凭空跳涨（实测曾出现 +200%），因子变化应落在复牌日，此时原始价已除权。
- 成分：某查询日只使用当日或之前发布的最近一次月末权重，不使用之后的数据。

实测（2026-11-26 快照，668 只历史成分股，含之后调出和退市的）：

| 检查 | 结果 |
|---|---|
| 与 BaoStock 逐日对照（7 只股票，2020-2024，约 7,300 行） | 开高低收、昨收、成交量、停牌、ST 全部一致；唯一差异是 BaoStock 在 000001.SZ 2020-12-31 的复权因子无公司行动却下跳 17%，Tushare 无此变化，应为 BaoStock 错误 |
| 规则计算的涨跌停价 vs 交易所发布的 `stk_limit`（1,699,551 个交易行） | 445 行不一致（0.03%）：411 行是未完成股改的 S 股 5% 限制（600733.SH 2015-2016，规则尚未覆盖，且当时不在沪深300）；22 行是沪市 2015-2017 跌停价恰在半分位时的舍入方向；7 行 ST 状态与限幅不一致，原因未明；5 行是 Tushare 在 2019-10-28 给科创板的占位值 100000 |
| 核对中发现并修复 | 创业板代码段原只识别 300/301，302 开头（更名换号后启用）被按主板 10% 计算；停牌期间除权导致的后复权价虚假跳涨（修复后停牌行价格变化为 0，单日涨跌超 21% 的 6 行中 5 行是长期停牌后复牌、1 行是科创板新股无涨跌幅期） |
| 沪深300 月度快照 | 143 次（2014-12 至 2026-10 月末），每次 300 只，与 000300.SH 口径逐行一致 |
| 导出 | 1,737,796 行，2015-01-05 至 2026-11-24；涨跌停未知 141 行（新股首日按发行价计算的规则未覆盖），规则不符 6 行 |

## 用第二来源交叉核对

`crosscheck` 以 BaoStock 为基准，逐日对照第二来源。两个来源只核对、不合并、不覆盖；第二来源的原始返回按区间缓存，可离线复现报告。

```bash
# 默认 Yahoo Finance（免费、无需账号）
quant-agent crosscheck --provider baostock --codes 600000.SH 000001.SZ 600518.SH \
  --start 2020-01-02 --end 2024-12-31 --output output/crosscheck_yahoo
```

Yahoo 核对四项：原始收盘价（先按其后的拆股比例还原 Yahoo 的拆股调整价）、停牌/无成交日、公司行动日期（BaoStock 复权因子变化日 vs Yahoo 分红拆股日）、用 Yahoo 最高/最低价独立检验按规则计算的涨跌停价。Yahoo 不提供昨收、ST、涨跌停价，只能核对、不能替代 BaoStock；仅支持沪深。

`--check-source tushare` 改用 Tushare（daily、stk_limit、suspend_d），需要在 `.env` 设置 `TUSHARE_TOKEN`，可选 `TUSHARE_HTTP_URL`。

实测（7只股票，2020-2024）：Yahoo 最高/最低价没有一次超出规则涨跌停价（7,101行）；149行收盘价不一致集中在深市2020年1-5月，另有2024-07-05沪市被Yahoo标为无成交，经新浪数据裁决均为 Yahoo 数据错误，BaoStock 正确。

## 检查

跨项目格式联合检查（用本地缓存导出，再用两个下游项目各自的导入代码读取；只读，不修改下游项目）：

```bash
sh scripts/contract_check.sh            # 默认 600000.SH 600518.SH
```

下游任何一方改了输入格式，这里会直接报错。

```bash
ruff check .
ruff format --check .
mypy quant_agent
pytest
```

测试不联网。核心代码覆盖率要求80%。架构、签名和数据流见 [设计](docs/design.md)。`config/market_rules.yaml` 是禁止用于回测的草案，第二阶段核验历史规则后启用。

## 后续计划

回测、组合与大模型研究在独立项目 [quant-research-lab](https://github.com/bar-art/quant-research-lab) 中完成；本项目专注于可核对的数据。仍待补充：未完成股权分置改革的 S 股 5% 涨跌停规则、主板新股上市首日（2023-04-10 前）的发行价规则、公告日口径的财务数据。

开发环境：Python 3.11，`.venv/`、`.python/` 等本地环境目录不提交。第一阶段的范围与验收记录见 [设计](docs/design.md) 和 [验收记录](docs/phase1_acceptance.md)。

## 历史指数成分快照（2026-11-25）

```bash
python -m quant_agent.cli universe --provider baostock --index hs300 \
  --snapshot-dates 2018-06-12 2020-06-12 --output output/hs300-history
```

也支持 `zz500`、`sz50`。每次显式传查询日期；空结果、重复代码、来源更新日期晚于查询日均停止，不回退当前成分。快照保留查询日期、来源更新日期与代码，后续退市证券不会按今天的上市状态过滤。接口签名已对照本地安装的 BaoStock 包源码核查；测试使用离线替身，尚未对这些日期完成在线完整性验证。来源历史快照也不等于当时首次发布的原始版本。

### 用历史成分构建股票池

```bash
# 1. 每月第一个交易日取一次快照（日期来自来源交易日历）
python -m quant_agent.cli universe --provider baostock --index hs300 --monthly \
  --start 2015-01-05 --end 2026-11-20 --output output/hs300-monthly
# 2. 导出快照中出现过的全部证券（含之后调出、退市的），逐日写入 universe_member
python -m quant_agent.cli export --provider baostock \
  --universe-file output/hs300-monthly/hs300_historical_members.csv \
  --start 2015-01-05 --end 2026-11-20 --target quant-research-lab --output output/qrl_hs300
```

- 某交易日使用不晚于该日的最近一次快照；两次快照之间视为不变，月度快照下调入调出最多滞后一个月。首个快照晚于行情起点时停止，不向前回填。
- 默认任何证券取不到行情就停止；`--skip-unavailable` 跳过并写入 `export_report.json` 的 `unavailable_codes`，manifest 来源说明记录快照次数和日期范围。
- 不在股票池的日子仍保留行情（用于持仓估值和卖出），只是不再作为新的买入对象。端到端验证：人工设定某股票调出后，下游回测不再买入它。
- 这降低了幸存者偏差，但月度快照不是每日精确成分，manifest 仍声明 `historical_universe_complete: false`。

alpha 导出保留停牌行、涨跌停与豁免字段。需要配套更新后的 alpha 导入器/评估器。`tests/test_baostock_export.py` 的联合契约测试直接调用相邻目录 `alpha-evidence-lab/src` 和 `quant-research-lab/src` 的真实导入器；应将三个项目并列检出后运行。它验证格式与状态传递，尚不证明完整 A 股交易成本和公司行动处理正确。

尚待处理：公告日财务因子的历史修订版本、双源价格自动核对、交易所规则正式文本出处、完整交易单位及日期分段成本、T+1 可得敏感性比较。
