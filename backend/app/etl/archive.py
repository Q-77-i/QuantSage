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
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.core.config import get_settings

DATASET = "event-timeline"
#: CLI 的内容寻址对象库与状态文件都落在这个目录（与 M2a 的行情下载共用一个 store）
ARCHIVE_SUBDIR = "raw"
STATE_FILE = ".xiaoshi-history-state.json"
_404_MARKER = "HTTP 404"
#: 平台要求停下的信号（与 P1 的 MCP 拉取同族语义）
_RATE_LIMIT_MARKERS = ("rate_limited_no_retry", "HTTP 429", "bulk_download_required")
_TIMEOUT_SECONDS = 300


class ArchiveError(RuntimeError):
    """归档通道不可用或返回了无法解释的结果。"""


class RateLimited(ArchiveError):
    """平台限流（429 / `rate_limited_no_retry`）。

    **这是停止信号，不是单日失败**：`Retry-After` 是平台明确给出的等待秒数，
    继续发请求只会把窗口越推越远（本项目的红线里也写着「429 按 Retry-After 停」）。
    调用方必须立刻中止整轮，而不是换下一天接着试。
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
    command = get_settings().xiaoshi_cli_command
    if not command:
        raise ArchiveError("XIAOSHI_CLI_COMMAND 未配置（见 .env.example）")
    return command


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    key = get_settings().xiaoshi_api_key.get_secret_value()
    if key:
        env["XIAOSHI_API_KEY"] = key
    return subprocess.run(  # noqa: S603 —— argv 由本模块构造，密钥不进 argv
        [_cli(), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def archive_dir(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    return base / ARCHIVE_SUBDIR


def coverage(data_dir: Path | None = None) -> ArchiveCoverage:
    """归档已发布到哪一天（含分片数与行数），供漏拉判定与状态展示。"""
    result = _run(["coverage", "--dataset", DATASET])
    if result.returncode != 0:
        raise ArchiveError(f"归档 coverage 查询失败：{_tail(result)}")
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
            "--date",
            day.isoformat(),
            "--data-dir",
            str(archive_dir(data_dir)),
        ]
    )
    if result.returncode == 0:
        return True
    output = result.stdout + result.stderr
    if any(marker in output for marker in _RATE_LIMIT_MARKERS):
        raise RateLimited(f"{day} 触发平台限流：{_tail(result)}", retry_after=_retry_after(output))
    if _404_MARKER in output:
        return False
    raise ArchiveError(f"{day} 下载失败（退出码 {result.returncode}）：{_tail(result)}")


def _retry_after(output: str) -> int | None:
    """从 `Retry-After=<秒>` 里取出平台给的等待秒数。"""
    match = re.search(r"Retry-After=(\d+)", output)
    return int(match.group(1)) if match else None


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
    start = text.find("{")
    if start < 0:
        raise ArchiveError(f"CLI 输出不是 JSON：{text[:200]}")
    try:
        return json.loads(text[start:])
    except ValueError as exc:
        raise ArchiveError(f"CLI 输出无法解析为 JSON：{exc}") from exc


def _tail(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text[-300:]
