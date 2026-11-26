"""命令行：BaoStock 或 Tushare 本地快照提供昨收、停牌、ST 与复权因子，并可导出到下游研究项目。"""

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from quant_agent.config import Settings
from quant_agent.data.cache import LocalCache
from quant_agent.data.crosscheck import compare_code, compare_yahoo, summarize, summarize_yahoo
from quant_agent.data.export import to_alpha_evidence_lab, to_quant_research_lab
from quant_agent.data.providers import (
    AKShareProvider,
    BaoStockProvider,
    DataProvider,
    RequestGate,
    SyntheticProvider,
)
from quant_agent.data.quality import DISCLAIMER, markdown_report, quality_report
from quant_agent.data.rules import apply_price_limits
from quant_agent.data.service import DataService
from quant_agent.data.tushare_snapshot import TushareSnapshotProvider, download, migrate_legacy
from quant_agent.data.tushare_source import TushareSource
from quant_agent.data.universe import load_snapshots, membership, monthly_snapshot_dates
from quant_agent.data.yahoo_source import YahooSource

FIELD_PROVIDERS = {"baostock", "tushare"}
INDEX_CODES = {"hs300": "399300.SZ"}


def run_snapshot(args: argparse.Namespace, settings: Settings) -> None:
    """联网下载 Tushare 原始数据到本地快照；之后 --provider tushare 离线读取。"""
    if args.migrate:
        manifest = migrate_legacy(settings.tushare_snapshot_dir)
        print(
            f"已为旧快照建立核验清单：{manifest['files']} 个文件，"
            f"完整性自 {manifest['integrity_established_at']} 起可核验（legacy）"
        )
        return
    if args.index not in INDEX_CODES:
        raise ValueError(f"snapshot 目前支持 {sorted(INDEX_CODES)}")
    # 计划里只记接口主机的哈希：能区分不同来源，又不写入地址本身。
    host = urlsplit(settings.tushare_http_url).hostname or "api.tushare.pro"
    endpoint_digest = hashlib.sha256(host.encode()).hexdigest()[:16]
    gate = RequestGate(settings.request_interval, settings.request_attempts)
    source = TushareSource(
        settings.tushare_token.get_secret_value(), settings.tushare_http_url, gate
    )
    manifest = download(
        source.query,
        settings.tushare_snapshot_dir,
        INDEX_CODES[args.index],
        args.start,
        args.end,
        source.source,
        endpoint_digest=endpoint_digest,
    )
    print(f"快照完成：{len(manifest['codes'])} 只历史成分股；目录 {settings.tushare_snapshot_dir}")


def build_provider(name: str, settings: Settings) -> DataProvider:
    gate = RequestGate(settings.request_interval, settings.request_attempts)
    if name == "synthetic":
        return SyntheticProvider()
    if name == "baostock":
        return BaoStockProvider(gate)
    if name == "tushare":
        return TushareSnapshotProvider(settings.tushare_snapshot_dir)
    return AKShareProvider(gate, settings.request_timeout)


def listing_window(instruments: pd.DataFrame, code: str) -> tuple[date | None, date | None]:
    """上市日与退市日（来源证券基础资料）；未知时为 None，涨跌停按未知处理。"""

    def lookup(column: str) -> date | None:
        if column not in instruments:
            return None
        match = instruments.loc[instruments["symbol"] == code, column]
        if match.empty or pd.isna(match.iloc[0]) or not str(match.iloc[0]).strip():
            return None
        return date.fromisoformat(str(match.iloc[0])[:10])

    return lookup("ipo_date"), lookup("out_date")


def run_export(args: argparse.Namespace, service: DataService, cache: LocalCache) -> None:
    if args.target is None:
        raise ValueError("export 需要 --target quant-research-lab 或 alpha-evidence-lab")
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("导出目录非空；请使用新目录，避免覆盖已有输入")
    snapshots = load_snapshots(args.universe_file) if args.universe_file else None
    if snapshots is not None and args.target != "quant-research-lab":
        raise ValueError("--universe-file 只用于多股票回测格式（quant-research-lab）")
    if snapshots is not None and not args.codes:
        # 快照中出现过的全部证券，包括之后被调出或退市的，避免只用幸存者。
        codes = sorted(snapshots["symbol"].astype(str).unique())
    else:
        codes = args.codes or [args.code]
    calendar = service.get_calendar(args.refresh)
    instruments = service.get_instruments(args.refresh)
    frames, quality, versions = [], {}, {}
    unavailable: dict[str, str] = {}
    for code in codes:
        try:
            bars = service.get_daily_bars(code, args.start, args.end, args.refresh)
        except ValueError as error:
            if not args.skip_unavailable:
                raise ValueError(
                    f"{code} 取不到行情（{error}）；确认后可加 --skip-unavailable"
                ) from error
            unavailable[code] = str(error)[:120]
            continue
        listed_from, listed_to = listing_window(instruments, code)
        limited = apply_price_limits(bars, calendar, listed_from)
        frames.append(limited)
        quality[code] = quality_report(
            limited, calendar, args.start, args.end, listed_from=listed_from, listed_to=listed_to
        )
        versions[code] = cache.provenance(service.provider.name, "bars", code)
    if not frames:
        raise ValueError("没有任何证券取到行情")
    frame = pd.concat(frames, ignore_index=True)
    exported = [code for code in codes if code not in unavailable]
    listed = ", ".join(exported) if len(exported) <= 20 else f"{len(exported)} 只证券"
    source = f"{service.provider.source}；{listed}；{args.start}至{args.end}"
    universe = None
    if snapshots is not None:
        universe = membership(snapshots, frame)
        days = sorted(snapshots["snapshot_date"].dt.strftime("%Y-%m-%d").unique())
        source += (
            f"；股票池为 {snapshots['index'].iloc[0]} 历史成分快照（{len(days)} 次，"
            f"{days[0]} 至 {days[-1]}，两次快照之间视为不变）"
        )
    summary: dict[str, Any] = (
        to_quant_research_lab(
            frame, source, args.output, args.price_mode, universe, args.allow_gap_placeholders
        )
        if args.target == "quant-research-lab"
        else to_alpha_evidence_lab(frame, args.output, args.price_mode, args.allow_gap_placeholders)
    )
    summary.update(
        provider=service.provider.name,
        source=source,
        quality={code: report["status"] for code, report in quality.items()},
        quality_issues={code: report["issues"] for code, report in quality.items()},
        data_versions=versions,
        unavailable_codes=unavailable,
        disclaimer=DISCLAIMER,
    )
    (args.output / "export_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"导出 {summary['rows']} 行、{len(exported)} 只证券到 {args.output}"
        f"{f'（{len(unavailable)} 只取不到行情，已记录）' if unavailable else ''}；"
        f"涨跌停未知 {summary['unknown_limit_rows']} 行、"
        f"规则不符 {summary['rule_mismatch_rows']} 行"
    )


def run_crosscheck(
    args: argparse.Namespace, service: DataService, cache: LocalCache, settings: Settings
) -> None:
    gate = RequestGate(settings.request_interval, settings.request_attempts)
    checker: TushareSource | YahooSource = (
        YahooSource(gate)
        if args.check_source == "yahoo"
        else TushareSource(
            settings.tushare_token.get_secret_value(), settings.tushare_http_url, gate
        )
    )
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("核对输出目录非空；请使用新目录")
    codes = args.codes or [args.code]
    calendar = service.get_calendar(args.refresh)
    instruments = service.get_instruments(args.refresh)

    def cached(dataset: str, code: str) -> pd.DataFrame:
        frame = (
            None
            if args.refresh
            else cache.read_range(checker.name, dataset, code, args.start, args.end)
        )
        if frame is None:
            frame = getattr(checker, dataset)(code, args.start, args.end)
            # 空结果也缓存：它本身是需要报告的来源状态；--refresh 可重新抓取。
            cache.put(checker.name, dataset, code, args.start, args.end, frame, checker.source)
        return frame

    per_code = {}
    for code in codes:
        bars = service.get_daily_bars(code, args.start, args.end, args.refresh)
        limited = apply_price_limits(bars, calendar, listing_window(instruments, code)[0])
        if isinstance(checker, YahooSource):
            per_code[code] = compare_yahoo(limited, cached("history", code))
        else:
            per_code[code] = compare_code(
                limited, cached("daily", code), cached("stk_limit", code), cached("suspend_d", code)
            )
    report = {
        "baseline": service.provider.source,
        "check_source": checker.source,
        "check_provider": checker.name,
        "start": str(args.start),
        "end": str(args.end),
        "summary": summarize_yahoo(per_code)
        if isinstance(checker, YahooSource)
        else summarize(per_code),
        "per_code": per_code,
        "note": "两个来源相互核对，不合并、不覆盖；不一致需要人工判断哪一方正确。",
        "disclaimer": DISCLAIMER,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "crosscheck_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"核对完成：{json.dumps(report['summary'], ensure_ascii=False)}；来源 {checker.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"A股数据基础设施；{DISCLAIMER}")
    parser.add_argument(
        "command",
        choices=[
            "fetch",
            "calendar",
            "instruments",
            "export",
            "universe",
            "crosscheck",
            "snapshot",
        ],
    )
    parser.add_argument(
        "--provider", choices=["akshare", "baostock", "tushare", "synthetic"], default="akshare"
    )
    parser.add_argument("--index", choices=["hs300", "zz500", "sz50"], default="hs300")
    parser.add_argument("--snapshot-dates", type=date.fromisoformat, nargs="+")
    parser.add_argument(
        "--monthly", action="store_true", help="universe：取 --start 至 --end 每月第一个交易日"
    )
    parser.add_argument("--universe-file", type=Path, help="export：universe 命令输出的成分快照")
    parser.add_argument(
        "--skip-unavailable", action="store_true", help="export：跳过取不到行情的证券并记录"
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="snapshot：为旧格式快照离线建立核验清单（标记 legacy），不联网",
    )
    parser.add_argument(
        "--allow-gap-placeholders",
        action="store_true",
        help="export：无行情也无停牌证据的日子导出为不可成交的估值占位，并逐日记录",
    )
    parser.add_argument("--code", default="600000.SH")
    parser.add_argument("--codes", nargs="+", help="export 时的多只证券代码")
    parser.add_argument("--target", choices=["quant-research-lab", "alpha-evidence-lab"])
    parser.add_argument("--price-mode", choices=["hfq", "raw"], default="hfq")
    parser.add_argument(
        "--check-source",
        choices=["yahoo", "tushare"],
        default="yahoo",
        help="crosscheck 的核对来源",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 1, 5))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("output"))
    args = parser.parse_args(argv)
    print(DISCLAIMER)
    try:
        settings = Settings()
        if args.command == "snapshot":
            run_snapshot(args, settings)
            return 0
        provider = build_provider(args.provider, settings)
        cache = LocalCache(settings.data_dir)
        service = DataService(provider, cache)
        if args.command == "universe":
            if not isinstance(provider, BaoStockProvider | TushareSnapshotProvider):
                raise ValueError("universe 需要 --provider baostock 或 tushare")
            if args.monthly:
                args.snapshot_dates = monthly_snapshot_dates(
                    service.get_calendar(args.refresh), args.start, args.end
                )
            if not args.snapshot_dates:
                raise ValueError(
                    "universe 需要 --snapshot-dates 日期列表，或 --monthly 配合 --start/--end"
                )
            target = args.output / f"{args.index}_historical_members.csv"
            if target.exists():
                raise ValueError("历史快照已存在；请使用新输出目录")
            frames = [
                provider.fetch_index_members(args.index, day)
                for day in sorted(set(args.snapshot_dates))
            ]
            args.output.mkdir(parents=True, exist_ok=True)
            pd.concat(frames, ignore_index=True).to_csv(target, index=False)
            print(f"历史成分快照：{target}；不代表完整原始发布版本或价格覆盖")
            return 0
        if args.command == "crosscheck":
            if args.provider not in FIELD_PROVIDERS:
                raise ValueError("crosscheck 需要 --provider baostock 或 tushare 作为基准")
            if args.provider == args.check_source:
                raise ValueError("基准与核对来源相同，核对没有意义")
            run_crosscheck(args, service, cache, settings)
            return 0
        if args.command == "export":
            if args.provider not in FIELD_PROVIDERS:
                raise ValueError(
                    "只有 BaoStock 与 Tushare 快照提供停牌、ST 与昨收；"
                    "export 需要 --provider baostock 或 tushare"
                )
            run_export(args, service, cache)
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        prefix = f"{args.provider}_{args.code}_{args.start}_{args.end}"
        if args.command == "fetch":
            bars = service.get_daily_bars(args.code, args.start, args.end, args.refresh)
            calendar = service.get_calendar(args.refresh)
            report = quality_report(bars, calendar, args.start, args.end)
            report["provider"] = args.provider
            report["synthetic"] = args.provider == "synthetic"
            report["data_versions"] = cache.provenance(provider.name, "bars", args.code)
            (args.output / f"{prefix}_quality.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (args.output / f"{prefix}_quality.md").write_text(
                markdown_report(report), encoding="utf-8"
            )
            bars.to_csv(args.output / f"{prefix}_bars.csv", index=False)
            cache.export_manifest(args.output / f"{prefix}_manifest.json", provider.name, args.code)
            print(f"{len(bars)} 行；{report['status']}；报告目录：{args.output}")
        elif args.command == "calendar":
            frame = service.get_calendar(args.refresh)
            frame.to_csv(args.output / f"{args.provider}_calendar.csv", index=False)
            print(f"日历范围：{frame.trade_date.min()} 至 {frame.trade_date.max()}")
        else:
            frame = service.get_instruments(args.refresh)
            frame.to_csv(args.output / f"{args.provider}_instruments.csv", index=False)
            print(f"{len(frame)} 条当前快照；不含完整退市历史，不可直接作为历史股票池")
        return 0
    except Exception as error:
        # 第三方异常可能含 URL/token，控制台只输出类型和可操作指引；本项目自身的校验信息可以显示。
        detail = f"：{str(error)[:200]}" if isinstance(error, ValueError) else ""
        print(
            f"执行失败（{type(error).__name__}{detail}）。请检查日期/字段、网络与数据源状态；"
            "安装依赖用 python -m pip install -r requirements.lock；"
            "离线验证用 --provider synthetic --start 2024-01-02 --end 2024-01-05。"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
