# ashare-data-pipeline

[![tests](https://github.com/bar-art/ashare-data-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/bar-art/ashare-data-pipeline/actions/workflows/tests.yml)

把 A 股历史日线整理成可以放心回测的数据：下载、检查、和第二个数据源逐日核对，再导出成 CSV。

## 为什么需要它

直接拿免费行情做回测，常见四个坑，每个都会让结果看起来比实际好：

1. **只用今天的成分股。** 用今天的沪深300名单回测过去十年，等于提前排除了后来被调出、退市的股票，收益会被高估（幸存者偏差）。本项目按每个月当时的成分构建股票池。
2. **停牌当成正常交易。** 停牌的股票买不进也卖不出。本项目只把有停牌记录的日子标为停牌；既没有成交、也没有停牌记录的日子单独标出来，不去猜。
3. **忽略涨跌停。** 涨停时很难买到，跌停时很难卖出。本项目按各板块当年的规则算出每天的涨跌停价，回测程序据此判断能否成交。
4. **除权日价格断层。** 分红送股后股价会"跳水"，但持有人并没有亏钱。本项目导出复权价格消除这种断层，并修正了停牌期间除权导致的价格错误跳涨。

## 核对结果

用 2015–2026 年所有当过沪深300成分的 668 只股票（含后来调出和退市的）检验：

- **和交易所公布的涨跌停价对比：** 约 170 万个交易日中 99.97% 一致；不一致的 445 行原因都已查明或列出，其中 411 行来自同一只特殊股票。
- **停牌：** 没有行情的 43,903 个交易日里，97.1% 能找到当天的停牌记录；其余 1,545 天作为缺口单独列出。
- **和 BaoStock 对比**（7 只股票，2020–2024）：价格、成交量、停牌、ST 全部一致，还发现了 BaoStock 自己的一处复权错误。
- **和 Yahoo Finance 对比：** 发现 Yahoo 有 149 个错误收盘价。
- **核对中修好的问题：** 一类创业板代码被错算成 10% 涨跌停；停牌期间除权导致复权价凭空上涨，最多 +200%。

最终导出 1,737,796 行日线。完整明细见 [docs/data_rules.md](docs/data_rules.md)。

## 快速开始

需要 Python 3.11。

```bash
git clone https://github.com/bar-art/ashare-data-pipeline.git
cd ashare-data-pipeline
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
cp .env.example .env

# 用离线生成的样本数据确认安装成功
quant-agent fetch --provider synthetic --start 2024-01-02 --end 2024-01-05
```

## 导出真实数据

最简单的方式是用 BaoStock（免费，不需要账号）：

```bash
quant-agent export --provider baostock --codes 600000.SH 000001.SZ 300750.SZ \
  --start 2020-01-02 --end 2024-12-31 --target quant-research-lab --output output/my_data
```

输出目录里有：

- `bars.csv`：每只股票每天一行，包含价格、是否停牌、涨跌停价、是否 ST，以及收盘后才可用的时间戳；
- `manifest.json`：数据来源和覆盖范围；
- `export_report.json`：本次导出的检查报告，列出停牌、缺口和发现的问题。

`--target alpha-evidence-lab` 则改为每只股票一个 CSV。两种格式分别给 [quant-research-lab](https://github.com/bar-art/quant-research-lab) 和 [alpha-evidence-lab](https://github.com/bar-art/alpha-evidence-lab) 使用。

想用历史成分做股票池、改用 Tushare 本地快照，或者和 Yahoo 交叉核对，见 [docs/data_rules.md](docs/data_rules.md)。那里也写了停牌状态、各板块涨跌停规则、复权和 ST 的具体处理方式。

## 还没做到的

- 少数特殊情况的涨跌停规则还没覆盖：未完成股改的 S 股、较早新股上市首日；
- 沪市少数跌停价和交易所公布值差 1 分钱（舍入方向问题）；
- 成分股是月度快照，调入调出最多晚一个月；
- BaoStock 不含北交所；
- 没有财务数据；退市整理期、重新上市等情形没有单独处理。

## 开发

```bash
ruff check . && ruff format --check . && mypy quant_agent && pytest      # 全部离线运行
```

设计说明见 [docs/design.md](docs/design.md)，字段覆盖见 [docs/data_coverage.md](docs/data_coverage.md)。仓库原名 a-share-quant-agent，所以 Python 包名仍是 `quant_agent`，命令仍是 `quant-agent`。

仅供研究，不构成投资建议。
