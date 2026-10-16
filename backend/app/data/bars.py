"""小石 `cn-daily` 全市场行情：下载、校验、整片物化与**按需刷新**（P2-M2a + M2d）。

原 `scripts/download_bars.py` 的四段逻辑搬到这里（脚本改薄壳），对齐既有范式
`scripts/download_events.py ↔ app.etl.runner`：脚本只管参数与打印。

**行情没有日增量可拉**：`cn-daily` 的源侧维度是 `{year, adjust}`（`xiaoshi_client/plans.py`
里没有 `date`），发布形态就是「按年整片、内容寻址」。所以 M2d 做的是**「便宜探测 → 按需
整片重下」**，**不是实时**——且源侧的滞后**是观测值不是契约**：canonical 轨道实测连停过两个
交易日（平台另有 available 轨道，更快但有缺口，我们不用；见 SPEC §3 M2d）。

四段（M2a 原样，行为未改）:

    download_partitions → verify_raw → materialize（先暂存、校验通过再换入）→ write_meta（指纹）

M2d 新增的是**门禁**与**预算**，它们都长在 `refresh()` 里：

  ① **本地早退**（零 API 调用）：本地行情已覆盖「今天及以前最后一个交易日」就直接收工
     ——周末与长假自然命中。日历越界时跳过这一条、照常探测（日历到期不该拖垮刷新）。
  ② **一级门禁**（1 次调用）：`coverage` 的发布标识与 `bars_sync.json` 记的比，不同才往下。
  ③ **二级门禁**（零网络）：跑满 21 次 `download` 后，把回执里 21 片的 sha 与
     `_meta/bars.json` 的 `fingerprint.shards` 比——**全同就不动物化**（`data/bars/` 零写入）。
     只比交易日会漏掉**原地内容修订**（前复权在新除权后整体重算），所以判据落在 sha 上。
  ④ **整轮预算**：`per-call timeout = min(单次上限, 剩余预算)`，超时**杀掉子进程**。
     依据不是纸面的：M2a 那条 `_run_cli` 原本没有 timeout，实测 2026-10-12 手敲那轮
     卡在网络 20 分钟零写入，而它一直握着数据目录的锁。

**回执不能当判据**：`data/raw/.xiaoshi-history-state.json` 由两条通道共用、按最新
publication 重写——实测 10-12 夜里语料那轮把行情的 21 条条目冲掉了。所以门禁记忆另存
`_meta/bars_sync.json`，且每轮都跑满 21 个 spec（不做「只补变了的那年」的优化）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from app.core.config import get_settings
from app.data import calendar as cal
from app.data import ledger, snapshots, xiaoshi_cli
from app.data.data_lock import LOCK, Busy

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
#: 门禁记忆（M2d）。**不放回执**（会被另一条通道冲掉），也不塞进 `bars.json`
#: （那是数据指纹契约，体检 B1 逐片重算它）
SYNC_NAME = "bars_sync.json"

DEFAULT_BUDGET_SECONDS = 1200
#: 剩余预算低于这个数就不再发起新的一次调用——免得刚起就被杀
MIN_CALL_SECONDS = 30

#: 一轮的 scope（台账按它区分两条通道；`runner` 的 last_run 要排除它）
SCOPE = ledger.BARS_SCOPE

#: 注入点：测试传假 CLI，线上就是 `xiaoshi_cli.run`
RunCli = Callable[..., subprocess.CompletedProcess[str]]


class BarsError(RuntimeError):
    """行情通道不可用或返回了无法解释的结果。"""

    def __init__(self, message: str, *, calls: int = 0) -> None:
        super().__init__(message)
        #: 抛出前已经发出的下载会话数——**失败路径也要如实记账**，见 `_with_calls`
        self.calls = calls


def _with_calls(exc: Exception, calls: int) -> Exception:
    """把「已发出几次会话」挂到异常上。

    `download_calls` 若只在 `download_partitions()` 返回时才赋值，那么**中途失败那一轮
    会一律记成 0**（429 / 超时 / CLI 报错三条路都是抛出去的）。台账是运维判断「明天要不要
    人工看一眼」的依据，少记会让人以为一次都没试。
    """
    exc.calls = calls  # type: ignore[attr-defined]
    return exc


def _calls_of(exc: Exception) -> int:
    return int(getattr(exc, "calls", 0) or 0)


class BudgetExceeded(BarsError):
    """整轮预算用尽——已下几片如实记，次日重跑即续（未变的分片按 sha 复用）。"""

    def __init__(self, message: str, *, calls: int) -> None:
        super().__init__(message)
        self.calls = calls


@dataclass(slots=True)
class BarsReport:
    """一轮刷新/重建的结果。`status` 是判据，其余是证据。"""

    status: str  # updated | up_to_date | skipped_no_gap | rate_limited | incomplete | failed
    started_at: str
    finished_at: str = ""
    manifest_version: str = ""
    source_last: str = ""
    local_last: str = ""
    expected_session: str = ""
    #: 实际发出的 `download` CLI 调用数（0 = 一级门禁或本地早退挡下了）
    download_calls: int = 0
    total_partitions: int = len(PARTITIONS)
    #: 与本地指纹**不一致**的分片名（空 = 源侧没换数据）
    changed_shards: list[str] = field(default_factory=list)
    retry_after_seconds: int | None = None
    elapsed_seconds: float = 0.0
    note: str = ""
    error: str = ""
    scope: str = SCOPE

    def summary(self) -> str:
        parts = [f"行情刷新 {self.status}"]
        if self.local_last:
            parts.append(f"本地 {self.local_last}")
        if self.source_last:
            parts.append(f"源侧 {self.source_last}")
        if self.download_calls:
            parts.append(f"{self.download_calls}/{self.total_partitions} 次下载会话")
        if self.changed_shards:
            parts.append(f"换入 {len(self.changed_shards)} 片")
        if self.retry_after_seconds:
            parts.append(f"限流等待约 {self.retry_after_seconds // 60} 分钟")
        parts.append(f"{self.elapsed_seconds:.1f}s")
        if self.note:
            parts.append(self.note)
        if self.error:
            parts.append(self.error)
        return "；".join(parts)


@dataclass(frozen=True, slots=True)
class SyncState:
    """门禁记忆：上一轮探测看到了什么、上一轮**成功同步**到哪个发布。"""

    manifest_version: str = ""
    source_last: str = ""
    last_probe_at: str = ""
    last_change_at: str = ""
    status: str = ""
    local_last: str = ""


@dataclass(frozen=True, slots=True)
class BarsPaths:
    data_dir: Path
    raw_dir: Path
    bars_dir: Path
    meta_dir: Path


def paths(data_dir: Path | None = None) -> BarsPaths:
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    return BarsPaths(
        data_dir=base,
        raw_dir=base / "raw",
        bars_dir=base / "bars",
        meta_dir=base / "_meta",
    )


# ── 门禁记忆 ────────────────────────────────────────────────


def read_sync_state(meta_dir: Path | None = None) -> SyncState:
    """读门禁记忆。**缺文件或坏文件一律当空**——它只是「上一轮看到过什么」，
    丢了最多多探一次，不该让刷新失败（与 `_meta` 里其它清单的定位一致）。"""
    target = (Path(meta_dir) if meta_dir else paths().meta_dir) / SYNC_NAME
    if not target.is_file():
        return SyncState()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return SyncState()
    if not isinstance(payload, dict):
        return SyncState()
    return SyncState(
        manifest_version=str(payload.get("manifest_version") or ""),
        source_last=str(payload.get("source_last") or ""),
        last_probe_at=str(payload.get("last_probe_at") or ""),
        last_change_at=str(payload.get("last_change_at") or ""),
        status=str(payload.get("status") or ""),
        local_last=str(payload.get("local_last") or ""),
    )


def write_sync_state(meta_dir: Path, state: SyncState) -> Path:
    """原子写（临时文件 + `os.replace`）：这份文件会被 API 进程读来展示源侧边界。"""
    meta_dir.mkdir(parents=True, exist_ok=True)
    target = meta_dir / SYNC_NAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


# ── 判据（纯函数，可逐条手搓断言）────────────────────────────


def expected_session(today: date) -> date | None:
    """「今天及以前最后一个交易日」。**日历越界返回 None**（不是抛错也不是猜）。

    刷新遇到 None 时跳过本地早退、照常探测：日历到期这颗炸弹不该由行情刷新引爆
    （X1 已在报 warn，`runway_note` 会把它写进台账）。
    """
    try:
        return cal.last_session_on_or_before(today)
    except cal.CalendarOutOfRange:
        return None


def decide(
    *,
    local_last: date | None,
    expected: date | None,
    synced_manifest: str,
    current_manifest: str,
    force: bool = False,
) -> str:
    """这一轮该做什么：`skipped_no_gap` / `up_to_date` / `refresh`。

    顺序即代价顺序：先比**本地**（零调用），再比**发布标识**（一次调用），最后才下载。
    """
    if force:
        return "refresh"
    if expected is not None and local_last is not None and local_last >= expected:
        return "skipped_no_gap"
    if synced_manifest and current_manifest and synced_manifest == current_manifest:
        return "up_to_date"
    return "refresh"


# ── 数据面（M2a 原样搬入；除「调用方式」外行为不变）──────────────


def _cli_env() -> dict[str, str]:
    """密钥只经环境变量传给子进程，绝不进 argv。"""
    return {**os.environ, "XIAOSHI_API_KEY": get_settings().xiaoshi_api_key.get_secret_value()}


def download_partitions(
    raw_dir: Path,
    *,
    run_cli: RunCli = xiaoshi_cli.run,
    call_timeout: int | None = None,
    budget_seconds: int | None = None,
) -> int:
    """逐片串行下载，返回**实际发出的调用数**。

    已存在且 sha 对得上的分片 CLI 会跳过网络下载（只重算校验和），故整体可重跑；预算用尽
    抛 `BudgetExceeded`（调用方把已下片数记进台账），限流抛 `RateLimited`（**停止信号**）。

    每轮都跑满 21 个 spec：回执里可能已经没有别的年份了（被另一条通道冲掉），
    「只补变了的那年」这种优化建立在一个随时会消失的前提上。
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    budget = DEFAULT_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    started = time.monotonic()
    calls = 0
    for year, adjust in PARTITIONS:
        remaining = budget - (time.monotonic() - started)
        if remaining < MIN_CALL_SECONDS:
            raise BudgetExceeded(
                f"整轮预算 {budget}s 用尽（已发出 {calls} 次下载会话）", calls=calls
            )
        limit = int(remaining if call_timeout is None else min(call_timeout, remaining))
        print(f"下载 cn-daily year={year} adjust={adjust} …", flush=True)
        # 会话**已发出**：即便随后被杀，它也是一次真实会话，台账照记（`_with_calls`）
        calls += 1
        try:
            result = run_cli(
                [
                    "download",
                    "--dataset", "cn-daily",
                    "--year", str(year),
                    "--adjust", adjust,
                    "--data-dir", str(raw_dir),
                ],
                timeout=limit,
            )
        except xiaoshi_cli.CliTimeout as exc:
            raise _with_calls(exc, calls) from None
        if result.returncode != 0:
            output = (result.stdout or "") + (result.stderr or "")
            if xiaoshi_cli.rate_limit_hit(output):
                raise _with_calls(
                    xiaoshi_cli.RateLimited(
                        f"cn-daily {year}/{adjust} 触发平台限流：{xiaoshi_cli.tail(result)}",
                        retry_after=xiaoshi_cli.retry_after(output),
                    ),
                    calls,
                )
            raise BarsError(
                f"cn-daily {year}/{adjust} 下载失败（退出码 {result.returncode}）："
                f"{xiaoshi_cli.tail(result)}",
                calls=calls,
            )
    return calls


def verify_raw(raw_dir: Path, meta_dir: Path, *, run_cli: RunCli = xiaoshi_cli.run) -> dict:
    """完整性校验留证据；valid 为假或存在 issues 一律中止，不带着可疑数据往下走。"""
    meta_dir.mkdir(parents=True, exist_ok=True)
    evidence = meta_dir / "verify_raw.json"
    result = run_cli(["verify", "--data-dir", str(raw_dir), "--output", str(evidence)])
    if result.returncode != 0:
        raise BarsError(f"分片校验未跑成（退出码 {result.returncode}）：{xiaoshi_cli.tail(result)}")
    verdict = json.loads(evidence.read_text(encoding="utf-8"))
    if not verdict.get("valid") or verdict.get("issues"):
        raise BarsError(f"分片校验未通过：{verdict.get('issues')}")
    return verdict


def read_receipt(raw_dir: Path) -> dict:
    path = raw_dir / RECEIPT_NAME
    if not path.is_file():
        raise BarsError(f"缺少下载回执 {path}；先跑一次下载（或去掉 --skip-download）")
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


def receipt_entries(raw_dir: Path) -> list[tuple[str, str, int, str, Path, dict]]:
    """从回执里挑出 cn-daily 分片：(adjust, market, year, object_key, 本地路径, 回执条目)。

    回执是 CLI 每次调用累积重写的，跨 manifest 版本会丢掉旧条目——所以**分片齐不齐由
    `materialize` 按 PARTITIONS 逐一核对来兜底**，不依赖回执恰好记得全。
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
    return entries


def changed_shards(raw_dir: Path, meta_dir: Path) -> list[str]:
    """与 `_meta/bars.json` 的指纹比对，返回**内容变了的**分片名（升序）。

    这是二级门禁：回执里 21 片的 sha 与「盘上那 21 片落地时的 sha」全同 ⇒ 源侧没换数据，
    不必重物化。清单缺失或读数不齐都**按「全变」处理**（保守方向：宁多重物化，不静默漏）。
    """
    recorded = {
        item.get("object_key"): item.get("sha256") for item in _read_fingerprint(meta_dir)
    }
    if not recorded:
        return sorted(BARS_NAME.format(adjust=a, year=y) for y, a in PARTITIONS)
    changed: list[str] = []
    current: dict[str, str] = {}
    for _adjust, _market, _year, object_key, _path, value in receipt_entries(raw_dir):
        current[object_key] = str(value.get("sha256") or "")
    for object_key, sha in recorded.items():
        if current.get(object_key) != sha:
            name = _name_from_object_key(object_key)
            changed.append(name or object_key)
    return sorted(changed)


def _read_fingerprint(meta_dir: Path) -> list[dict]:
    target = Path(meta_dir) / "bars.json"
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return []
    shards = (payload.get("fingerprint") or {}).get("shards") or []
    return [item for item in shards if isinstance(item, dict)]


def _name_from_object_key(object_key: str) -> str | None:
    matched = OBJECT_KEY_PATTERN.search(object_key)
    if not matched:
        return None
    return BARS_NAME.format(adjust=matched["adjust"], year=int(matched["year"]))


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
    entries = receipt_entries(raw_dir)
    if not entries:
        raise BarsError("回执里没有 cn-daily 分片；下载步骤可能没跑成")
    staging = bars_dir / "_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()

    outputs: list[dict] = []
    con = duckdb.connect()
    try:
        for adjust, market, year, object_key, source, value in entries:
            if not source.is_file():
                raise BarsError(f"回执指向的分片不存在：{source}")
            staged = staging / BARS_NAME.format(adjust=adjust, year=year)
            shutil.copy2(source, staged)

            digest = _sha256(staged)
            if digest != value["sha256"]:
                raise BarsError(f"{staged.name} 落盘后 sha 与源不一致：{digest} != {value['sha256']}")

            rows, symbols, first, last = con.execute(
                "SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date)"
                f" FROM read_parquet('{staged.as_posix()}')"
            ).fetchone()
            if rows == 0:
                raise BarsError(f"{staged.name} 没有数据")
            print(f"  {staged.name}: {rows} 行 / {symbols} 标的 {first} → {last}", flush=True)

            outputs.append(
                {
                    # 相对**数据目录的父目录**记路径（线上即仓库根的 `data/bars/…`），
                    # 与 M2a 的清单体例一致——体检 B1 直接拿它开文件
                    "path": str((bars_dir / staged.name).relative_to(bars_dir.parent.parent)),
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
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        con.close()

    missing = sorted(set(PARTITIONS) - {(o["year"], o["adjust"]) for o in outputs})
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        raise BarsError(f"分片不齐，缺 {len(missing)} 片：{missing}")

    # 校验全过，才动 data/bars/：先清 P1 遗留（不清会被 glob 双计），再整体换入
    removed = _clean_stale(bars_dir)
    if removed:
        print(f"  清理 P1 遗留的按标的文件 {len(removed)} 个：{'、'.join(removed)}")
    for staged in sorted(staging.glob("*.parquet")):
        os.replace(staged, bars_dir / staged.name)
    staging.rmdir()
    return outputs


def write_meta(raw_dir: Path, meta_dir: Path, receipt: dict, verdict: dict, outputs: list[dict]) -> Path:
    """写 `_meta/bars.json`（数据版本指纹）。**体例不动**——体检 B1 逐片重算它。"""
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema": "quantsage.bars_meta/v2",
        "generated_at": datetime.now(UTC).isoformat(),
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


def rebuild(
    raw_dir: Path | None = None,
    bars_dir: Path | None = None,
    meta_dir: Path | None = None,
    *,
    run_cli: RunCli = xiaoshi_cli.run,
) -> list[dict]:
    """用**本地已有分片**重物化（零网络会话）——M2a 的 `--skip-download` 路径，行为一字未改。

    用途：改了口径或盘上产物受损时，用 `data/raw` 里的内容寻址对象重建 `data/bars/`。
    """
    paths_ = paths()
    raw = Path(raw_dir) if raw_dir else paths_.raw_dir
    bars = Path(bars_dir) if bars_dir else paths_.bars_dir
    meta = Path(meta_dir) if meta_dir else paths_.meta_dir
    verdict = verify_raw(raw, meta, run_cli=run_cli)
    receipt = read_receipt(raw)
    outputs = materialize(raw, bars)
    write_meta(raw, meta, receipt, verdict, outputs)
    snapshots.take(Path(raw).parent, scope="bars-rebuild", reason=f"本地重物化 {len(outputs)} 片")
    return outputs


# ── M2d：按需刷新 ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Probe:
    """`coverage` 探到的东西（只读清单，不下载）。"""

    manifest_version: str
    quality_status: str
    source_last: str  # cn-daily 的 coverage.last（源侧最新交易日）


def probe(*, run_cli: RunCli = xiaoshi_cli.run) -> Probe:
    """问源侧「你到哪了」。**1 次调用、不下载**——这是便宜门禁的全部成本。"""
    result = run_cli(["coverage", "--dataset", "cn-daily"], timeout=xiaoshi_cli.DEFAULT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        if xiaoshi_cli.rate_limit_hit(output):
            raise xiaoshi_cli.RateLimited(
                f"coverage 触发平台限流：{xiaoshi_cli.tail(result)}",
                retry_after=xiaoshi_cli.retry_after(output),
            )
        raise BarsError(f"coverage 查询失败（退出码 {result.returncode}）：{xiaoshi_cli.tail(result)}")
    payload = xiaoshi_cli.load_json(result.stdout)
    window = payload.get("coverage") or {}
    return Probe(
        manifest_version=str(payload.get("manifest_version") or ""),
        quality_status=str(payload.get("quality_status") or ""),
        source_last=str(window.get("last") or ""),
    )


def local_last_trade_date(data_dir: Path | None = None) -> date | None:
    """本地行情末端。**测量出来的，不是读清单**——与体检 X5 同一条 SQL 的口径。

    刻意**不走 `duckdb_client.connect()`**：那个连接要求 `bars/` 与 `events/` 都有分片，
    而行情刷新不该因为「语料还没回填」就判不出本地末端（那只会白多探一次源侧，
    但把两件事耦合在一起迟早会咬人）。
    """
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    if not any((base / "bars").glob("*.parquet")):
        return None
    pattern = str(base / "bars" / "*.parquet").replace("'", "''")
    con = duckdb.connect()
    try:
        row = con.execute(f"SELECT max(trade_date) FROM read_parquet('{pattern}')").fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else None


def refresh(
    *,
    data_dir: Path | None = None,
    force: bool = False,
    budget_seconds: int | None = None,
    call_timeout: int | None = None,
    run_cli: RunCli = xiaoshi_cli.run,
    today: date | None = None,
    write_ledger_line: bool = True,
) -> BarsReport:
    """按需刷新行情：门禁 → 下载 → 二级门禁 → 物化。**持数据目录的锁**（见 `data_lock`）。

    幂等：源侧没变则一个字节都不写；已下过的分片按 sha 复用，中断后重跑即续。
    """
    if not LOCK.acquire(blocking=False):
        raise Busy("已有一次数据同步在执行（事件语料或行情刷新）")
    paths_ = paths(data_dir)
    settings = get_settings()
    budget = settings.bars_refresh_budget_seconds if budget_seconds is None else budget_seconds
    started_monotonic = time.monotonic()
    report = BarsReport(status="failed", started_at=datetime.now(UTC).isoformat())
    state = read_sync_state(paths_.meta_dir)
    try:
        today = today or date.today()
        local_last = local_last_trade_date(paths_.data_dir)
        expected = expected_session(today)
        report.local_last = local_last.isoformat() if local_last else ""
        report.expected_session = expected.isoformat() if expected else ""

        # ① 本地早退：零 API 调用
        action = decide(
            local_last=local_last,
            expected=expected,
            synced_manifest="",
            current_manifest="",
            force=force,
        )
        if action == "skipped_no_gap":
            report.status = "skipped_no_gap"
            report.note = f"本地已覆盖最近交易日 {report.expected_session}，未探测源侧"
            return _finish(report, paths_, state, started_monotonic, write_ledger_line)

        # ② 一级门禁：1 次调用问源侧
        found = probe(run_cli=run_cli)
        report.manifest_version = found.manifest_version
        report.source_last = found.source_last
        action = decide(
            local_last=local_last,
            expected=expected,
            synced_manifest=state.manifest_version,
            current_manifest=found.manifest_version,
            force=force,
        )
        if action == "up_to_date":
            report.status = "up_to_date"
            report.note = f"发布标识未变（{found.manifest_version}），未发起下载会话"
            return _finish(report, paths_, state, started_monotonic, write_ledger_line)

        # ③ 下载（二级门禁靠 sha 比对，见下）
        report.download_calls = download_partitions(
            paths_.raw_dir,
            run_cli=run_cli,
            call_timeout=call_timeout,
            budget_seconds=budget,
        )

        # ④ 二级门禁：源侧换数据了吗（按分片 sha，不是按交易日）
        changed = changed_shards(paths_.raw_dir, paths_.meta_dir)
        report.changed_shards = changed
        if not changed:
            report.status = "up_to_date"
            report.note = "分片 sha 与本地指纹全同，未动物化"
            return _finish(report, paths_, state, started_monotonic, write_ledger_line)

        verdict = verify_raw(paths_.raw_dir, paths_.meta_dir, run_cli=run_cli)
        receipt = read_receipt(paths_.raw_dir)
        outputs = materialize(paths_.raw_dir, paths_.bars_dir)
        write_meta(paths_.raw_dir, paths_.meta_dir, receipt, verdict, outputs)
        # 清单刚被改写 ⇒ 打一份快照（E3b）。清单是单槽覆盖写的，不抄这一份，旧版本的
        # 指针就永远找不回来了（字节还在 raw/o，但没有清单就不知道当时该是什么样）
        snapshots.take(
            paths_.data_dir, scope="bars-refresh", reason=f"换入 {len(outputs)} 片"
        )
        report.status = "updated"
        report.local_last = max((item["last_date"] for item in outputs), default=report.local_last)
        report.note = f"换入 {len(outputs)} 片"
        return _finish(report, paths_, state, started_monotonic, write_ledger_line)
    except xiaoshi_cli.RateLimited as exc:
        report.status = "rate_limited"
        report.retry_after_seconds = exc.retry_after
        report.download_calls = _calls_of(exc)
        minutes = f"{exc.retry_after / 60:.0f} 分钟" if exc.retry_after else "未知时长"
        report.error = str(exc)[:300]
        report.note = f"平台限流，需等待约 {minutes}；**不重试**，次日再来"
        return _finish(report, paths_, state, started_monotonic, write_ledger_line)
    except xiaoshi_cli.CliTimeout as exc:
        report.status = "incomplete"
        report.download_calls = _calls_of(exc)
        report.error = str(exc)[:300]
        report.note = "单次调用超时（子进程已终止）；已下分片按 sha 复用，重跑即续"
        return _finish(report, paths_, state, started_monotonic, write_ledger_line)
    except BudgetExceeded as exc:
        report.status = "incomplete"
        report.download_calls = exc.calls
        report.error = str(exc)[:300]
        report.note = "整轮预算用尽；已下分片按 sha 复用，重跑即续"
        return _finish(report, paths_, state, started_monotonic, write_ledger_line)
    except Exception as exc:  # noqa: BLE001 —— 一轮失败不该带崩调度器；状态如实记
        report.status = "failed"
        report.download_calls = _calls_of(exc)
        report.error = f"{type(exc).__name__}: {exc}"[:300]
        return _finish(report, paths_, state, started_monotonic, write_ledger_line)
    finally:
        LOCK.release()


def _finish(
    report: BarsReport,
    paths_: BarsPaths,
    previous: SyncState,
    started_monotonic: float,
    write_ledger_line: bool,
) -> BarsReport:
    """收尾：算耗时、写门禁记忆、记台账。**一切收尾都不该把一轮的结论改掉**。"""
    report.finished_at = datetime.now(UTC).isoformat()
    report.elapsed_seconds = round(time.monotonic() - started_monotonic, 1)
    if runway := cal.runway_note():
        report.note = f"{report.note}；{runway}" if report.note else runway

    now = report.finished_at
    # 只有**成功对上源侧**的那两类才更新门禁记忆的发布标识：否则失败一轮就把标识记下，
    # 次日会以为「已同步」而永远不再下。「分片全同」也算对上——源侧换了发布但没换数据。
    synced = report.status in {"up_to_date", "updated"}
    state = replace(
        previous,
        manifest_version=(
            report.manifest_version if synced and report.manifest_version else previous.manifest_version
        ),
        source_last=report.source_last or previous.source_last,
        local_last=report.local_last or previous.local_last,
        last_probe_at=now if report.download_calls or report.manifest_version else previous.last_probe_at,
        last_change_at=now if report.status == "updated" else previous.last_change_at,
        status=report.status,
    )
    try:
        write_sync_state(paths_.meta_dir, state)
    except OSError as exc:  # noqa: BLE001 —— 记忆写不进去只是下次多探一次
        report.note = f"{report.note}；门禁记忆写入失败：{exc}"[:300]
    if write_ledger_line:
        try:
            ledger.append(paths_.meta_dir, _ledger_payload(report))
        except OSError as exc:  # noqa: BLE001 —— 同上：台账不是判据
            report.note = f"{report.note}；台账写入失败：{exc}"[:300]
    return report


def _ledger_payload(report: BarsReport) -> dict[str, Any]:
    payload = asdict(report)
    # 台账字段名与事件通道**刻意不撞**：X6 靠 `archive_last` 找归档边界，
    # 行情这边叫 `source_last`（源侧最新交易日），不会被它误读。
    return payload


def last_run(data_dir: Path | None = None) -> dict | None:
    """台账里最后一次行情刷新（供 `/api/v1/etl/status`）。"""
    return ledger.last(paths(data_dir).meta_dir, scope=SCOPE)
