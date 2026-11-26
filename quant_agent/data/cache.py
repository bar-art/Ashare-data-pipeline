"""不可变 Parquet 分片和 DuckDB 来源清单；仅支持单进程写入。"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd


class LocalCache:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = str(self.root / "catalog.duckdb")
        with duckdb.connect(self.database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS manifest (
                provider VARCHAR, dataset VARCHAR, code VARCHAR, start_date DATE,
                end_date DATE, path VARCHAR, fetched_at VARCHAR, sha256 VARCHAR,
                source VARCHAR, row_count INTEGER)""")

    def entries(self, provider: str, dataset: str, code: str) -> list[tuple[object, ...]]:
        with duckdb.connect(self.database) as db:
            return db.execute(
                """SELECT start_date,end_date,path,fetched_at,sha256,source,row_count
                FROM manifest WHERE provider=? AND dataset=? AND code=? ORDER BY fetched_at""",
                [provider, dataset, code],
            ).fetchall()

    def gaps(self, provider: str, code: str, start: date, end: date) -> list[tuple[date, date]]:
        remaining = [(start, end)]
        for item in self.entries(provider, "bars", code):
            left, right = date.fromisoformat(str(item[0])), date.fromisoformat(str(item[1]))
            updated = []
            for a, b in remaining:
                if right < a or left > b:
                    updated.append((a, b))
                else:
                    if a < left:
                        updated.append((a, left - timedelta(days=1)))
                    if right < b:
                        updated.append((right + timedelta(days=1), b))
            remaining = updated
        return remaining

    def put(
        self,
        provider: str,
        dataset: str,
        code: str,
        start: date,
        end: date,
        frame: pd.DataFrame,
        source: str,
    ) -> None:
        path = self.root / f"{uuid4().hex}.parquet"
        temporary = path.with_suffix(".tmp")
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with duckdb.connect(self.database) as db:
            db.execute(
                "INSERT INTO manifest VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    provider,
                    dataset,
                    code,
                    start,
                    end,
                    str(path),
                    datetime.now(UTC).isoformat(),
                    digest,
                    source,
                    len(frame),
                ],
            )

    def read(self, provider: str, dataset: str, code: str) -> pd.DataFrame | None:
        entries = self.entries(provider, dataset, code)
        if not entries:
            return None
        frames = []
        for item in entries:
            path = Path(str(item[2]))
            if hashlib.sha256(path.read_bytes()).hexdigest() != item[4]:
                raise ValueError("缓存文件校验失败，请恢复数据或使用新的 DATA_DIR")
            with duckdb.connect(self.database) as db:
                frames.append(db.execute("SELECT * FROM read_parquet(?)", [str(path)]).df())
        combined = pd.concat(frames, ignore_index=True)
        if dataset == "bars":
            return combined.drop_duplicates(["ts_code", "trade_date"], keep="last")
        # 日历和股票名单是完整快照，不把旧名单并入新名单。
        return frames[-1]

    def read_range(
        self, provider: str, dataset: str, code: str, start: date, end: date
    ) -> pd.DataFrame | None:
        """读取请求区间完全相同的最近一次缓存；用于按区间整体抓取的数据集。"""
        for item in reversed(self.entries(provider, dataset, code)):
            if str(item[0]) == start.isoformat() and str(item[1]) == end.isoformat():
                path = Path(str(item[2]))
                if hashlib.sha256(path.read_bytes()).hexdigest() != item[4]:
                    raise ValueError("缓存文件校验失败，请恢复数据或使用新的 DATA_DIR")
                with duckdb.connect(self.database) as db:
                    return db.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
        return None

    def provenance(self, provider: str, dataset: str, code: str) -> list[dict[str, object]]:
        keys = ["start", "end", "path", "fetched_at", "sha256", "source", "rows"]
        return [
            dict(zip(keys, [str(value) for value in item], strict=True))
            for item in self.entries(provider, dataset, code)
        ]

    def export_manifest(self, path: Path, provider: str, code: str) -> None:
        payload = {
            dataset: self.provenance(provider, dataset, key)
            for dataset, key in [("bars", code), ("calendar", ""), ("instruments", "")]
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
