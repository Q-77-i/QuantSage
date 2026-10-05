"""T2 行情样例数据：下载 cn-daily 年度分片 → 本地过滤 3 标的 → data/bars/。

小石不提供按标的的行情下载维度（服务端契约要求 filter_locally），因此流程为
「下载整市场年度分片 → 本地 SQL 过滤 → 落盘」。原始分片留档 data/raw/ 供溯源，
校验证据与清单写入 data/_meta/。

用法：cd backend && uv run python scripts/download_bars.py [--skip-download]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from app.core.config import REPO_ROOT, get_settings  # noqa: E402
from app.data.xiaoshi import assert_installed  # noqa: E402

SYMBOLS: dict[str, str] = {"600519": "贵州茅台", "300750": "宁德时代", "600036": "招商银行"}
# cn-daily 维度为 (year, adjust)；market 服务端恒为 CN，无需传
PARTITIONS: tuple[tuple[int, str], ...] = ((2025, "raw"), (2025, "qfq"), (2026, "raw"), (2026, "qfq"))

DEFAULT_RAW_DIR = REPO_ROOT / "data" / "raw"
DEFAULT_BARS_DIR = REPO_ROOT / "data" / "bars"
DEFAULT_META_DIR = REPO_ROOT / "data" / "_meta"


def _cli_env() -> dict[str, str]:
    """密钥只经环境变量传给子进程，绝不进 argv。"""
    return {**os.environ, "XIAOSHI_API_KEY": get_settings().xiaoshi_api_key.get_secret_value()}


def _run_cli(args: list[str]) -> str:
    cli = assert_installed().cli_command
    proc = subprocess.run([str(cli), *args], env=_cli_env(), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"小石 CLI 失败（exit {proc.returncode}）：{' '.join(args)}\n{proc.stderr.strip()[:800]}"
        )
    return proc.stdout


def download_partitions(raw_dir: Path) -> None:
    """逐片串行下载。遇 429 由 CLI 自行按 Retry-After 处理，这里不重试。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    for year, adjust in PARTITIONS:
        print(f"下载 cn-daily year={year} adjust={adjust} …", flush=True)
        _run_cli(
            [
                "download",
                "--dataset", "cn-daily",
                "--year", str(year),
                "--adjust", adjust,
                "--data-dir", str(raw_dir),
            ]
        )


def verify_raw(raw_dir: Path, meta_dir: Path) -> dict:
    """完整性校验留证据；valid 为假或存在 issues 一律中止，不带着可疑数据往下走。"""
    meta_dir.mkdir(parents=True, exist_ok=True)
    evidence = meta_dir / "verify_raw.json"
    _run_cli(["verify", "--data-dir", str(raw_dir), "--output", str(evidence)])
    verdict = json.loads(evidence.read_text(encoding="utf-8"))
    if not verdict.get("valid") or verdict.get("issues"):
        raise RuntimeError(f"分片校验未通过：{verdict.get('issues')}")
    return verdict


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def materialize(raw_dir: Path, bars_dir: Path) -> list[dict]:
    """按 (标的 × 复权) 切分落盘；输出文件名即 data/bars/{symbol}.{adjust}.parquet。"""
    bars_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(raw_dir / "**" / "*.parquet")
    adjusts = sorted({adjust for _, adjust in PARTITIONS})
    outputs: list[dict] = []

    con = duckdb.connect()
    try:
        for code, name in SYMBOLS.items():
            for adjust in adjusts:
                target = bars_dir / f"{code}.{adjust}.parquet"
                con.execute(
                    f"COPY (SELECT * FROM read_parquet({_sql_str(pattern)})"
                    f" WHERE symbol = {_sql_str(code)} AND adjustment = {_sql_str(adjust)}"
                    f" ORDER BY trade_date)"
                    f" TO {_sql_str(str(target))} (FORMAT PARQUET)"
                )
                rows, first, last, dup = con.execute(
                    "SELECT count(*), min(trade_date), max(trade_date),"
                    " (SELECT count(*) FROM (SELECT trade_date FROM read_parquet(?)"
                    "   GROUP BY trade_date HAVING count(*) > 1))"
                    " FROM read_parquet(?)",
                    [str(target), str(target)],
                ).fetchone()
                if rows == 0:
                    raise RuntimeError(f"{target.name} 没有数据，检查过滤条件是否与落盘格式一致")
                outputs.append(
                    {
                        "path": str(target.relative_to(REPO_ROOT)),
                        "symbol": code,
                        "name": name,
                        "adjustment": adjust,
                        "rows": rows,
                        "first_date": str(first),
                        "last_date": str(last),
                        "duplicate_dates": dup,
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                )
                print(f"  {target.name}: {rows} 行 {first} → {last}", flush=True)
    finally:
        con.close()
    return outputs


def write_meta(raw_dir: Path, meta_dir: Path, verdict: dict, outputs: list[dict]) -> Path:
    meta_dir.mkdir(parents=True, exist_ok=True)
    receipt = json.loads((raw_dir / ".xiaoshi-history-state.json").read_text(encoding="utf-8"))
    meta = {
        "schema": "quantsage.bars_meta/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "dataset": "cn-daily",
            "manifest_version": receipt.get("manifest_version"),
            "data_version": receipt.get("data_version"),
            "quality_track": receipt.get("quality_track"),
            "last_success_at": receipt.get("last_success_at"),
            "verify": {
                "valid": verdict.get("valid"),
                "provenance_verified": verdict.get("provenance_verified"),
                "file_count": verdict.get("file_count"),
                "row_count": verdict.get("row_count"),
                "issues": verdict.get("issues"),
            },
            "partitions": [
                {
                    "object_key": item["object_key"],
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                    "rows": item["rows"],
                    "first_time": item["first_time"],
                    "last_time": item["last_time"],
                }
                for item in verdict.get("files", [])
            ],
        },
        "symbols": SYMBOLS,
        "filter": "symbol IN (600519, 300750, 600036)，字段为分片全字段",
        "outputs": outputs,
    }
    path = meta_dir / "bars.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-download", action="store_true", help="复用已有 data/raw/ 分片")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--bars-dir", type=Path, default=DEFAULT_BARS_DIR)
    parser.add_argument("--meta-dir", type=Path, default=DEFAULT_META_DIR)
    args = parser.parse_args()

    if not args.skip_download:
        download_partitions(args.raw_dir)
    verdict = verify_raw(args.raw_dir, args.meta_dir)
    outputs = materialize(args.raw_dir, args.bars_dir)
    meta_path = write_meta(args.raw_dir, args.meta_dir, verdict, outputs)

    total = sum(item["rows"] for item in outputs)
    print(f"\n落盘 {len(outputs)} 个文件，共 {total} 行；清单 {meta_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
