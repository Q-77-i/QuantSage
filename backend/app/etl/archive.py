"""归档通道：调 `xiaoshi-data` CLI 拉「事件语料按日整片」，并读回执。

为什么不用 MCP（M2b 实测，见 SPEC v1.4）：MCP 单次硬顶 500 行、`cursor` 参数被 FastMCP
拒收（结构上不可翻页），而单日任一主要类型都已触顶——它只适合对话里的有界实时查询。

本模块只负责「把一天的归档拿到本地并如实报告结果」，不做归一化、不写查询层目录（那是
`store.py` 的事）。三条硬规则：

* **密钥只走环境变量**：CLI 的 `--api-key` 会把密钥放进 argv（进程列表可见），一律不传；
* **404 = 该日没有分片**，不是错误——节假日与「尚未发布」都会这样，如实回报给调用方；
* 其余非零退出、缺分片、sha 不符一律**抛错**，绝不静默当作「这天没数据」。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.core.config import get_settings
from app.data import xiaoshi_cli

DATASET = "event-timeline"
#: CLI 的内容寻址对象库与状态文件都落在这个目录（与 M2a 的行情下载共用一个 store）
ARCHIVE_SUBDIR = "raw"
STATE_FILE = ".xiaoshi-history-state.json"
_404_MARKER = "HTTP 404"
_TIMEOUT_SECONDS = xiaoshi_cli.DEFAULT_TIMEOUT_SECONDS


class ArchiveError(RuntimeError):
    """归档通道不可用或返回了无法解释的结果。"""


#: 平台限流信号本身不属于某个通道（行情整片同样会撞上），实体在 `data/xiaoshi_cli.py`。
#: 这里保留这个名字是因为调用方（`runner` 与用例）都按 `archive.RateLimited` 认它；
#: **它不再是 `ArchiveError` 的子类**——唯一会抛它的路径是 `download_day()`，而所有
#: `except ArchiveError` 的地方（都只调 `coverage()`）碰不到它，行为一字未变。
RateLimited = xiaoshi_cli.RateLimited


@dataclass(frozen=True, slots=True)
class ArchiveCoverage:
    """归档当前发布到哪一天——「本地是否漏拉」的判据来源。"""

    last_day: date
    files: int
    rows: int
    manifest_version: str


@dataclass(frozen=True, slots=True)
class Shard:
    """一个（日 × 事件类型）分片的本地对象。"""

    event_type: str
    object_key: str
    path: Path
    sha256: str
    size: int


def _cli() -> str:
    """归档通道的 CLI 路径。未配置时按**本通道**的语义报错（`ArchiveError`）。"""
    try:
        return xiaoshi_cli.cli_command()
    except xiaoshi_cli.CliError as exc:
        raise ArchiveError(str(exc)) from exc


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """跑一次 CLI。超时按本通道语义报 `ArchiveError`——`status()` 据此降级显示。"""
    try:
        return xiaoshi_cli.run(args, timeout=_TIMEOUT_SECONDS)
    except xiaoshi_cli.CliTimeout as exc:
        raise ArchiveError(str(exc)) from exc


def archive_dir(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    return base / ARCHIVE_SUBDIR


def coverage(data_dir: Path | None = None) -> ArchiveCoverage:
    """归档已发布到哪一天（含分片数与行数），供漏拉判定与状态展示。"""
    result = _run(["coverage", "--dataset", DATASET])
    if result.returncode != 0:
        raise ArchiveError(f"归档 coverage 查询失败：{xiaoshi_cli.tail(result)}")
    payload = _load_json(result.stdout)
    window = payload.get("coverage") or {}
    last = window.get("last")
    if not last:
        raise ArchiveError(f"归档 coverage 缺少 last 字段：{result.stdout[:200]}")
    from datetime import datetime  # noqa: PLC0415 —— 只在此处需要

    return ArchiveCoverage(
        last_day=datetime.fromisoformat(last).astimezone().date(),
        files=int(payload.get("counts", {}).get("files") or window.get("files") or 0),
        rows=int(payload.get("counts", {}).get("rows") or window.get("rows") or 0),
        manifest_version=str(payload.get("manifest_version") or ""),
    )


def download_day(day: date, data_dir: Path | None = None) -> bool:
    """拉取某一天的整片归档。返回 True 表示有数据、False 表示该日无分片（404）。

    日期按 `event_time` 的北京日切分（归档与在线接口同轴，见 SPEC §3 M2b）。
    """
    result = _run(
        [
            "download",
            "--dataset",
            DATASET,
            "--quality",
            "complete",
            # ⚠ `--quality complete` **必须显式传，不能吃默认值**（2026-10-17 CLI 升级当天实测）：
            # `download` 的 `--quality` 在 2026.10.6.2 新增了 `auto` 并**成为默认**，而 auto 对
            # **事件语料**解析成 `available`（有缺口、平台明写不喂回测的那条轨）。升级前默认是
            # `complete`，所以不带这个 flag 的话，下一步 ETL 会**静默换轨**——实测对照：
            # 同一命令旧版得 `quality_track=complete`、新版得 `available`。
            # 本项目固定用 canonical（宁慢不脏），这条口径不随 CLI 版本漂移。
            "--date",
            day.isoformat(),
            "--data-dir",
            str(archive_dir(data_dir)),
        ]
    )
    if result.returncode == 0:
        return True
    output = result.stdout + result.stderr
    if xiaoshi_cli.rate_limit_hit(output):
        raise RateLimited(
            f"{day} 触发平台限流：{xiaoshi_cli.tail(result)}",
            retry_after=xiaoshi_cli.retry_after(output),
        )
    if _404_MARKER in output:
        return False
    raise ArchiveError(f"{day} 下载失败（退出码 {result.returncode}）：{xiaoshi_cli.tail(result)}")


def day_shards(day: date, data_dir: Path | None = None) -> dict[str, Shard]:
    """从回执状态文件里取某天的分片：`event_type → Shard`。

    回执是 `object_key → {path, sha256, size}` 的映射（M2a 已确认），物化直接用它——
    不自己拼对象路径：内容寻址的目录布局是 CLI 的实现细节，不是我们的契约。
    """
    state_path = archive_dir(data_dir) / STATE_FILE
    if not state_path.exists():
        raise ArchiveError(f"缺少归档回执状态文件：{state_path}（先跑一次 download）")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    prefix = f"/data/{DATASET}/date={day.isoformat()}/"
    shards: dict[str, Shard] = {}
    for key, entry in (state.get("files") or {}).items():
        if prefix not in key:
            continue
        # 形如 .../date=2026-09-29/event_type=news/data.parquet → news
        event_type = key.split(prefix, 1)[1].split("/", 1)[0].removeprefix("event_type=")
        path = Path(entry["path"])
        if not path.exists():
            raise ArchiveError(f"回执指向的对象不存在：{path}（重跑 download 修复）")
        shards[event_type] = Shard(
            event_type=event_type,
            object_key=key,
            path=path,
            sha256=str(entry.get("sha256") or ""),
            size=int(entry.get("size") or 0),
        )
    return shards


def _load_json(text: str) -> dict:
    """CLI 输出的 JSON 解析（机制在 `xiaoshi_cli`，这里只翻成本通道的异常语义）。"""
    try:
        return xiaoshi_cli.load_json(text)
    except xiaoshi_cli.CliError as exc:
        raise ArchiveError(str(exc)) from exc
