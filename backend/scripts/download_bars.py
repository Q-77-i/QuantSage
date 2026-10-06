"""P2-M2a 全市场行情：下载 cn-daily 全部年度分片 → 整片物化到 data/bars/。

小石不提供按标的的行情下载维度（服务端契约要求 `filter_locally`），行情一律**整片**下载。
P1 只保留 3 个标的，按「标的 × 复权」切分成小文件；M2a 起覆盖数据源给的全市场，
落盘改为**整片物化**，命名扁平：

    data/bars/cn-daily_CN_{adjust}_{year}.parquet     # 21 片 = 7 年 × 3 复权

扁平命名是为了让查询层 `data/bars/*.parquet` 的 glob 语义**保持不变**——视图、测试夹具、
`data/_meta` 的消费方都不用改。代价是旧按标的文件必须清掉，否则 glob 会把同一标的
算两遍（`_clean_stale` 负责这件事，清理对象可从仍在的 data/raw/ 完整重建）。

原始分片留档 data/raw/（内容寻址，重复片按 sha 去重不重复占盘），校验证据与清单写 data/_meta/。

用法：cd backend && uv run python scripts/download_bars.py [--skip-download]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from app.core.config import REPO_ROOT, get_settings  # noqa: E402
from app.data.xiaoshi import assert_installed  # noqa: E402

# cn-daily 维度为 (year, adjust)；market 服务端恒为 CN，无需传
YEARS: tuple[int, ...] = tuple(range(2020, 2027))
ADJUSTS: tuple[str, ...] = ("raw", "qfq", "hfq")
PARTITIONS: tuple[tuple[int, str], ...] = tuple((y, a) for y in YEARS for a in ADJUSTS)

#: 物化文件名。查询层只认 `*.parquet`，命名本身不进契约，但清单与文档都引用它
BARS_NAME = "cn-daily_CN_{adjust}_{year}.parquet"

#: P1 遗留的按标的文件名形状（`600519.qfq.parquet`）。只清这一种，不碰别的
LEGACY_PATTERN = re.compile(r"^\d{6}\.(?:raw|qfq|hfq)\.parquet$")

#: 从回执的 object_key 里切出 (market, adjust, year)
OBJECT_KEY_PATTERN = re.compile(
    r"/data/cn-daily/market=(?P<market>[^/]+)/adjustment=(?P<adjust>[^/]+)/year=(?P<year>\d{4})/"
)

RECEIPT_NAME = ".xiaoshi-history-state.json"

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
    """逐片串行下载。遇 429 由 CLI 自行按 Retry-After 处理，这里不重试。

    已存在且 sha 对得上的分片 CLI 会跳过网络下载（只重算校验和），故整体可重跑。
    """
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


def read_receipt(raw_dir: Path) -> dict:
    path = raw_dir / RECEIPT_NAME
    if not path.is_file():
        raise RuntimeError(f"缺少下载回执 {path}；去掉 --skip-download 重新下载")
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_stale(bars_dir: Path) -> list[str]:
    """删掉 P1 的按标的文件。留着会被查询层的 glob 捞进去，同一标的算两遍。"""
    removed = sorted(p.name for p in bars_dir.glob("*.parquet") if LEGACY_PATTERN.match(p.name))
    for name in removed:
        (bars_dir / name).unlink()
    return removed


def _entries_from_receipt(raw_dir: Path) -> list[tuple[str, str, int, str, Path, dict]]:
    """从回执里挑出 cn-daily 分片：(adjust, market, year, object_key, 本地路径, 回执条目)。

    回执是 CLI 每次调用累积重写的，跨 manifest 版本会丢掉旧条目——所以**分片齐不齐由
    下面按 PARTITIONS 逐一核对来兜底**，不依赖回执恰好记得全。
    """
    receipt = read_receipt(raw_dir)
    entries: list[tuple[str, str, int, str, Path, dict]] = []
    for key, value in sorted(receipt.get("files", {}).items()):
        matched = OBJECT_KEY_PATTERN.search(key)
        if not matched:
            continue
        entries.append(
            (
                matched["adjust"],
                matched["market"],
                int(matched["year"]),
                key,
                Path(value["path"]),
                value,
            )
        )
    if not entries:
        raise RuntimeError("回执里没有 cn-daily 分片；下载步骤可能没跑成")
    return entries


def materialize(raw_dir: Path, bars_dir: Path) -> list[dict]:
    """按回执把每片**字节级**复制成扁平命名文件，再逐片核对 sha 与行数。

    不用 DuckDB COPY 重写：整片复制不需要 SQL，且字节一致才留得住「sha 与源相同」这条
    可校验的性质（DuckDB 重写会换压缩参数，sha 必然变）。

    **先全部落到暂存目录、校验通过后再换入**：直接往 data/bars/ 里边写边删的话，
    任何一片出错都会留下「旧文件已删 + 新文件不齐」——而视图只判「有没有 parquet」，
    不齐时不会报错，只会静默返回残缺历史。暂存用**子目录**而非 `.partial.parquet`：
    后者会被 `*.parquet` glob 捞到。
    """
    bars_dir.mkdir(parents=True, exist_ok=True)
    entries = _entries_from_receipt(raw_dir)
    staging = bars_dir / "_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()

    outputs: list[dict] = []
    con = duckdb.connect()
    try:
        for adjust, market, year, object_key, source, value in entries:
            if not source.is_file():
                raise RuntimeError(f"回执指向的分片不存在：{source}")
            staged = staging / BARS_NAME.format(adjust=adjust, year=year)
            shutil.copy2(source, staged)

            digest = _sha256(staged)
            if digest != value["sha256"]:
                raise RuntimeError(f"{staged.name} 落盘后 sha 与源不一致：{digest} != {value['sha256']}")

            rows, symbols, first, last = con.execute(
                "SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date)"
                f" FROM read_parquet('{staged.as_posix()}')"
            ).fetchone()
            if rows == 0:
                raise RuntimeError(f"{staged.name} 没有数据")

            outputs.append(
                {
                    "path": str((bars_dir / staged.name).relative_to(REPO_ROOT)),
                    "object_key": object_key,
                    "adjust": adjust,
                    "market": market,
                    "year": year,
                    "rows": rows,
                    "symbols": symbols,
                    "first_date": str(first),
                    "last_date": str(last),
                    "sha256": digest,
                }
            )
            print(f"  {staged.name}: {rows} 行 / {symbols} 标的 {first} → {last}", flush=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        con.close()

    missing = sorted(set(PARTITIONS) - {(o["year"], o["adjust"]) for o in outputs})
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"分片不齐，缺 {len(missing)} 片：{missing}")

    # 校验全过，才动 data/bars/：先清 P1 遗留（不清会被 glob 双计），再整体换入
    removed = _clean_stale(bars_dir)
    if removed:
        print(f"  清理 P1 遗留的按标的文件 {len(removed)} 个：{'、'.join(removed)}")
    for staged in sorted(staging.glob("*.parquet")):
        os.replace(staged, bars_dir / staged.name)
    staging.rmdir()
    return outputs


def write_meta(raw_dir: Path, meta_dir: Path, receipt: dict, verdict: dict, outputs: list[dict]) -> Path:
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema": "quantsage.bars_meta/v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": "cn-daily",
        "layout": "data/bars/cn-daily_CN_{adjust}_{year}.parquet（整片物化；查询层 *.parquet glob 不变）",
        # 数据版本指纹：价量口径不动，把「同一快照重放结论一致」建立在这里。
        # 锚点是**分片的 object_key + sha256**（内容寻址，永不变），不是回执顶层的
        # manifest_version —— 实测同一批分片来自多个 release（2020–2022 的 raw/hfq 出自
        # 20260908、qfq 出自 20260930），没有任何一片来自顶层那个版本号。
        # 前复权只在新的除权除息后整体重算，故跨版本比数字前先比指纹。
        "fingerprint": {
            "shards": [
                {"object_key": item["object_key"], "sha256": item["sha256"]}
                for item in sorted(outputs, key=lambda o: (o["year"], o["adjust"]))
            ],
            "receipt_data_version": receipt.get("data_version"),
            "receipt_manifest_version": receipt.get("manifest_version"),
            "quality_track": receipt.get("quality_track"),
        },
        "source": {
            "channel": "cli",
            "dataset": "cn-daily",
            "last_success_at": receipt.get("last_success_at"),
            "verify": {
                "valid": verdict.get("valid"),
                "provenance_verified": verdict.get("provenance_verified"),
                "file_count": verdict.get("file_count"),
                "row_count": verdict.get("row_count"),
                "issues": verdict.get("issues"),
            },
        },
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
    receipt = read_receipt(args.raw_dir)
    outputs = materialize(args.raw_dir, args.bars_dir)
    meta_path = write_meta(args.raw_dir, args.meta_dir, receipt, verdict, outputs)

    total = sum(item["rows"] for item in outputs)
    print(
        f"\n落盘 {len(outputs)} 片，共 {total} 行；"
        f"数据版本 {receipt.get('data_version')}；清单 {meta_path.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
