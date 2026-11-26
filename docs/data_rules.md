# 数据处理规则与命令详解

README 介绍项目是做什么的；这里写给实际使用数据的人，说明每个字段怎么来、每条命令怎么用。

## 历史股票池

每月取一次指数成分。某一天使用不晚于当天的最近一次快照，导出时逐日写入 `universe_member`。调出后的股票仍保留行情，用于估值和卖出，但不再作为新的买入对象。月度快照意味着调入调出最多滞后一个月。

```bash
# BaoStock：支持 hs300、zz500、sz50
quant-agent universe --provider baostock --index hs300 --monthly \
  --start 2015-01-05 --end 2026-11-20 --output output/hs300-monthly
quant-agent export --provider baostock --universe-file output/hs300-monthly/hs300_historical_members.csv \
  --start 2015-01-05 --end 2026-11-20 --target quant-research-lab --output output/qrl_hs300
```

导出快照里出现过的全部股票，包括之后调出和退市的。任何股票取不到行情时默认停止；加 `--skip-unavailable` 会跳过并在报告中记录。

## 停牌

每个交易日有一个状态 `bar_status`：

| 状态 | 含义 | `is_suspended` |
|---|---|---|
| `traded` | 有成交（包括盘中停牌后恢复交易的日子） | 否 |
| `suspended` | 无成交，且当天有全天停牌记录 | 是 |
| `missing` | 无成交，也没有停牌记录 | 未知 |
| `conflict` | 有全天停牌记录，却有成交 | 未知 |

默认导出遇到 `missing` 或 `conflict` 会停止，并列出股票和日期。加 `--allow-gap-placeholders` 后，这些日子导出为不可成交的估值行，并在 `export_report.json` 中逐日记录。

## 涨跌停价

由交易所昨收按板块规则计算，四舍五入到分：

| 板块 | 规则 |
|---|---|
| 主板 | 10%；ST 股 5%，2026-07-06 起改为 10% |
| 创业板 | 2020-08-24 起 20%（含 ST）；此前 10%，ST 5% |
| 科创板 | 20% |
| 北交所 | 2021-11-15 起 30% |
| 新股 | 科创板、2020-08-24 起上市的创业板、2023-04-10 起上市的主板，前 5 个交易日不设限；北交所上市首日不设限 |

规则覆盖不到的情况（例如更早的新股上市首日按发行价计算），涨跌停价留空，下游回测据此拒绝成交。如果某天的实际最高价或最低价越过了计算出的涨跌停价，说明规则不适用，当天的涨跌停价也改为未知。规则来源见 [data_coverage.md](data_coverage.md)。

## 复权、ST 与可得时间

- **复权：** 默认导出后复权价格（`--price-mode hfq`），涨跌停价乘以同一个因子，除权日不会出现虚假亏损；`raw` 输出交易所原始价格。使用 Tushare 快照时，停牌期间沿用停牌前最后一个交易日的复权因子，因子变化落在复牌日。
- **ST：** BaoStock 直接提供历史 ST 标记；Tushare 快照由股票名称的历史变化推断，某天没有名称记录时 ST 状态为未知。
- **可得时间：** 每行日线的 `available_at` 记为当天 15:00（北京时间），表示收盘后才可用于决策。

## 数据来源

| 来源 | 用途 | 需要 |
|---|---|---|
| AKShare | 快速取单只股票日线（`fetch`），默认来源 | 能访问国内数据源 |
| BaoStock | 昨收、停牌、ST、复权因子、历史成分 | 免费，无需账号；使用 10030 端口，部分网络会拦截 |
| Tushare | 本地快照：月度历史成分、日线、复权、涨跌停、停牌、名称变更 | `.env` 中的 `TUSHARE_TOKEN` |
| Yahoo Finance | 只用于交叉核对 | 免费 |

### Tushare 本地快照

token 会过期，所以先把原始返回完整保存下来，之后离线使用：

```bash
quant-agent snapshot --index hs300 --start 2014-01-01 --end 2026-11-24      # 联网一次，可断点续传
quant-agent universe --provider tushare --index hs300 --monthly \
  --start 2015-01-01 --end 2026-11-24 --output output/hs300-tushare
quant-agent export --provider tushare --start 2015-01-05 --end 2026-11-24 \
  --universe-file output/hs300-tushare/hs300_historical_members.csv \
  --allow-gap-placeholders --target quant-research-lab --output output/qrl_hs300_tushare
```

快照可以校验：

- 首次下载前写入 `plan.json`，记录接口、指数、日期范围和来源类型，不含 token；参数改变时必须换新目录；
- `files.jsonl` 记录每个文件的请求参数、行数、覆盖日期和 SHA256；
- 文件写完才发布，续传只跳过登记过且哈希相符的文件，读取时逐个核对哈希；
- 返回结果会检查空表、疑似截断、重复键和区间外日期；
- 旧格式快照用 `quant-agent snapshot --migrate` 离线建立校验清单。

快照目录默认是 `data/tushare_raw`（可用 `TUSHARE_SNAPSHOT_DIR` 修改），不提交到仓库。

### 交叉核对

```bash
quant-agent crosscheck --provider baostock --codes 600000.SH 000001.SZ 600518.SH \
  --start 2020-01-02 --end 2024-12-31 --output output/crosscheck_yahoo
```

默认用 Yahoo Finance 核对收盘价、停牌、公司行动日期和涨跌停价；`--check-source tushare` 改用 Tushare。两个来源只做比较，不互相覆盖。

## 导出格式

| `--target` | 文件 | 说明 |
|---|---|---|
| `quant-research-lab` | `bars.csv` + `manifest.json` | 多股票长表，含 `universe_member`（如果提供了股票池） |
| `alpha-evidence-lab` | 每只股票一个 CSV | 保留停牌行和涨跌停字段 |

每次导出都会写 `export_report.json`：行数、停牌和 ST 行数、各条涨跌停规则的使用次数、缺口占位、质量问题和数据版本。`manifest.json` 中 `historical_universe_complete` 始终为 `false`，因为月度快照不等于每日精确成分。

## 核对结果明细（2015–2026 沪深300历史成分）

| 检查 | 结果 |
|---|---|
| 与 BaoStock 逐日对照（7 只股票，2020–2024，约 7,300 行） | 开高低收、昨收、成交量、停牌、ST 全部一致。唯一差异是 BaoStock 在 000001.SZ 2020-12-31 的复权因子在没有公司行动的情况下下跳 17%，判断为 BaoStock 的错误 |
| 规则计算的涨跌停价 vs 交易所公布值（1,699,551 个交易行） | 445 行不一致（0.03%）：411 行来自一只未完成股改的 S 股（600733.SH，2015–2016，5% 限制尚未覆盖）；22 行是沪市 2015–2017 年跌停价恰好落在半分位时的舍入方向；7 行 ST 状态与限幅不符，原因未明；5 行是 Tushare 在 2019-10-28 给科创板的占位值 100000 |
| 停牌证据 | 交易区间内缺失 43,903 行日线，其中 42,619 行（97.1%）有当天的全天停牌记录；有全天停牌记录却有成交的为 0 行；盘中停牌的 229 天都有正常日线。其余 1,545 行（29 只股票，主要是几段 6–15 个月的长期停牌，以及退市前最后成交到退市日之间）没有逐日记录，作为缺口单独列出 |
| 与 Yahoo Finance 对照（7 只股票，2020–2024） | Yahoo 的最高、最低价没有一次越过规则计算的涨跌停价（7,101 行）。Yahoo 有 149 个错误收盘价（集中在深市 2020 年 1–5 月），并把 2024-07-05 标成无成交，经新浪数据确认是 Yahoo 的错误 |
| 核对中修复的问题 | 创业板 302 开头的代码曾被按主板 10% 计算涨跌停；停牌期间除权曾导致后复权价凭空上涨（最多 +200%）；修复后停牌行价格变化为 0 |
| 导出 | 1,737,796 行，2015-01-05 至 2026-11-24；143 次月度成分快照，每次 300 只；涨跌停未知 141 行，规则不符 6 行 |
