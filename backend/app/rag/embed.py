"""离线批处理：把 `data/events/` 的日分区嵌进 Qdrant，可断点续跑、可增量、可对账。

四条口径：

1. **断点单位是日分区**，判据是该日 Parquet 的 **sha256**（与 `_meta/events.json` 同源）。
   平台会修订既有事件（M2b 实测一天内改过 255/353 条），所以判据必须是内容指纹而不是时间戳——
   按时间戳判会漏掉「老日期被改」，按 sha 判则天然接得住。
2. **sha 变了先删该日再重嵌**：不删就会留下平台上已下线的事件（陈旧点），
   而查询层看不出来——它只会把过时的内容当证据递出去。
3. **长度分桶批处理**：本语料长短差 70 倍（29–2,065 字），不分桶就是让每批都 padding
   到最长样本，白烧算力。分桶后同批内长度接近，padding 开销接近零。
4. **日末 `wait=True`**：状态文件写「这天嵌完了」之前，必须确认 Qdrant 已可见——
   否则状态与可检索性是两回事，断点续跑会跳过一批其实没落地的点。

稀疏向量为空的行**省略稀疏腿**（Qdrant 的命名向量按点可选）：空稀疏向量不是「零分」，
是「搜不到」，留着它只会让这个点对稀疏分支永远隐身。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.rag import RagNotReady
from app.rag import collection as col
from app.rag.encoder import Embedder, get_embedder
from app.rag.text import build_embedding_text

log = logging.getLogger(__name__)

STATE_FILE = "embeddings.json"
DAY_PREFIX = "cn-events_"
#: 每次 upsert 的点数（与编码批大小无关：一个管算得快，一个管写得稳）
UPSERT_CHUNK = 256
#: 落盘 schema 版本：payload 形状变了就升版本，旧状态文件自然失效（不静默沿用）
STATE_SCHEMA = "quantsage.rag_state/v1"

#: 进 payload 的列（与 `collection.PAYLOAD_FIELDS` 对应）
_PAYLOAD_COLUMNS = (
    "event_id", "event_type", "title", "summary",
    "event_time", "available_at",
    "direction_norm", "importance_score", "symbols", "industries",
    "source", "original_source", "source_url", "content_hash",
)


@dataclass(frozen=True, slots=True)
class DayTask:
    """一天的嵌入任务及其动作。"""

    day: str
    path: Path
    rows: int
    sha256: str
    action: str  # "embed" | "skip" | "reembed"
    reason: str  # 人读的一句话（写进日志与报告）


@dataclass(frozen=True, slots=True)
class SyncReport:
    embedded: tuple[DayTask, ...]
    skipped: tuple[DayTask, ...]
    points: int
    seconds: float


def state_path(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    return base / "_meta" / STATE_FILE


def load_state(data_dir: Path | None = None) -> dict:
    path = state_path(data_dir)
    if not path.exists():
        return {"schema": STATE_SCHEMA, "collection": col.collection_name(), "days": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != STATE_SCHEMA or payload.get("collection") != col.collection_name():
        log.warning("嵌入状态文件 schema/collection 不匹配，按空状态处理：%s", path)
        return {"schema": STATE_SCHEMA, "collection": col.collection_name(), "days": {}}
    payload.setdefault("days", {})
    return payload


def save_state(payload: Mapping, data_dir: Path | None = None, *, merge: bool = True) -> Path:
    """写状态文件；`merge=True` 时与磁盘现有条目合并（同键以后写的为准）。

    合并不是「防御罕见竞态」：并行的两个嵌入进程各持一份内存状态，若各自整file覆盖，
    后写的必然把对方的记录**整段抹掉**。合并后，状态文件只用于跳过已嵌日，
    即便真有交错也最多让某天被重嵌一次，而不会丢掉记录。

    `recreate=True`（删库重建）时传 `merge=False`——那种场景下磁盘上的旧条目正是要清掉的。
    """
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    if merge and path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            current = {}
        same_target = (
            current.get("schema") == STATE_SCHEMA
            and current.get("collection") == body.get("collection")
        )
        if same_target:
            body["days"] = {**(current.get("days") or {}), **(body.get("days") or {})}
    body["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def load_manifest(data_dir: Path | None = None) -> dict[str, dict]:
    """`_meta/events.json` 的逐日条目 → `{day: {sha256, rows, file}}`。

    这是**语料的真源**（`read_day_entries` 的产物）：文件被手工删掉时清单不会继续宣称它有。
    """
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    manifest = base / "_meta" / "events.json"
    if not manifest.exists():
        raise RagNotReady(f"语料清单不存在：{manifest}（先跑 M2b 的 ETL）")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    days: dict[str, dict] = {}
    for item in payload.get("days", []):
        days[str(item["date"])] = {
            "sha256": str(item["sha256"]),
            "rows": int(item["rows"]),
            "file": str(item["file"]),
        }
    return days


def plan(
    manifest: Mapping[str, Mapping],
    state: Mapping,
    *,
    data_dir: Path | None = None,
    only: Iterable[str] | None = None,
    force: bool = False,
) -> list[DayTask]:
    """按清单与状态算出每天的动作。**只读**，可离线测。"""
    known = dict(state.get("days") or {})
    wanted = set(only) if only else None
    tasks: list[DayTask] = []
    for day in sorted(manifest):
        if wanted is not None and day not in wanted:
            continue
        entry = manifest[day]
        path = _day_path(day, data_dir)
        sha = str(entry["sha256"])
        rows = int(entry["rows"])
        if rows < 0:
            tasks.append(DayTask(day, path, rows, sha, "skip", "缺溯源旁注（rows=-1），先修数据"))
            continue
        previous = known.get(day)
        if force or previous is None:
            action = "embed" if previous is None else "reembed"
            reason = "首次嵌入" if previous is None else "强制重嵌"
        elif str(previous.get("sha256")) != sha:
            action = "reembed"
            reason = f"日分区内容变了（sha {str(previous.get('sha256'))[:8]} → {sha[:8]}）"
        else:
            tasks.append(DayTask(day, path, rows, sha, "skip", "sha 未变"))
            continue
        tasks.append(DayTask(day, path, rows, sha, action, reason))
    return tasks


def _day_path(day: str, data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    return base / "events" / f"{DAY_PREFIX}{day}.parquet"


def _read_rows(day: str, columns: Sequence[str], data_dir: Path | None = None) -> list[dict]:
    """读一天的全部行（集合式 SQL、一次连接；不逐标的、不去重）。"""
    import duckdb

    path = _day_path(day, data_dir)
    if not path.exists():
        raise RagNotReady(f"日分区不存在：{path}")
    con = duckdb.connect()
    try:
        sql = f"SELECT {', '.join(columns)} FROM read_parquet(?)"
        rows = con.execute(sql, [str(path)]).to_arrow_table().to_pylist()
    finally:
        con.close()
    return rows


def _payload_of(row: Mapping, day: str) -> dict:
    """一行 → payload。时间戳存 RFC3339 字符串（Qdrant 的 DatetimeRange 按此解析）。"""
    payload: dict = {"day": day, "text": build_embedding_text(row)}
    for key in _PAYLOAD_COLUMNS:
        value = row.get(key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        elif key == "symbols" or key == "industries":
            payload[key] = [str(x) for x in (value or ())]
        else:
            payload[key] = value
    return payload


def _length_order(texts: Sequence[str]) -> list[int]:
    """按长度升序给出编码顺序：同批内长度接近，padding 开销接近零。

    **排序后交给编码器一次性编码**，不要自己切成小批多次调用——实测（同机、同批大小）
    分 50 次调用比一次调用慢一倍，每次都把 tokenization 与调度开销重复付一遍。
    """
    return sorted(range(len(texts)), key=lambda i: (len(texts[i]), i))


def embed_day(
    day: str,
    *,
    client,
    embedder: Embedder,
    data_dir: Path | None = None,
) -> int:
    """嵌一天：读行 → 按长度排序一次性编码 → 分批 upsert（末批 wait）→ 返回点数。"""
    rows = _read_rows(day, _PAYLOAD_COLUMNS, data_dir)
    if not rows:
        return 0
    payloads = [_payload_of(row, day) for row in rows]
    texts = [p["text"] for p in payloads]

    order = _length_order(texts)
    encoded = embedder.encode_documents([texts[i] for i in order])
    points = [
        col.build_point(payloads[index], dense, sparse)
        for index, dense, sparse in zip(order, encoded.dense, encoded.sparse, strict=True)
    ]

    col.delete_day(client, day)  # 先删后写：不留平台上已下线的事件
    for start in range(0, len(points), UPSERT_CHUNK):
        chunk = points[start : start + UPSERT_CHUNK]
        last = start + UPSERT_CHUNK >= len(points)
        col.upsert_points(client, chunk, wait=last)  # 末批等确认，之后才敢写状态
    return len(points)


def sync(
    *,
    data_dir: Path | None = None,
    client=None,
    embedder: Embedder | None = None,
    only: Iterable[str] | None = None,
    force: bool = False,
    recreate: bool = False,
    progress=None,
) -> SyncReport:
    """增量同步：嵌「缺失或 sha 变化」的日分区。`recreate=True` 时清库重来。"""
    import time

    started = time.perf_counter()
    settings = get_settings()
    base = Path(data_dir) if data_dir is not None else settings.data_dir

    manifest = load_manifest(base)
    state = load_state(base)
    if recreate:
        state = {"schema": STATE_SCHEMA, "collection": col.collection_name(), "days": {}}

    client = client or col.get_client()
    col.ensure_collection(client, recreate=recreate)
    if recreate:
        save_state(state, base, merge=False)  # 重建 = 清空记录，不合并旧条目

    embedder = embedder or get_embedder()
    tasks = plan(manifest, state, data_dir=base, only=only, force=force)
    todo = [t for t in tasks if t.action in ("embed", "reembed")]
    skipped = tuple(t for t in tasks if t.action == "skip")

    state = dict(state)
    days_state = dict(state.get("days") or {})
    points_total = 0
    for task in todo:
        count = embed_day(task.day, client=client, embedder=embedder, data_dir=base)
        points_total += count
        days_state[task.day] = {
            "rows": task.rows,
            "sha256": task.sha256,
            "points": count,
            "action": task.action,
            "embedded_at": datetime.now(timezone.utc).isoformat(),
        }
        state["days"] = days_state
        state["schema"] = STATE_SCHEMA
        state["collection"] = col.collection_name()
        state["model"] = "BAAI/bge-m3"
        state["dim"] = col.DENSE_DIM
        save_state(state, base, merge=not recreate)  # 逐日落盘：中断只丢当天；并发时靠合并保记录
        if progress:
            progress(task, count)

    return SyncReport(
        embedded=tuple(todo), skipped=skipped, points=points_total,
        seconds=time.perf_counter() - started,
    )


@dataclass(frozen=True, slots=True)
class StatusReport:
    missing: dict[str, int]      # 本地有、索引无
    stale: dict[str, int]        # 两边都有但点数对不上
    extra: dict[str, int]        # 索引有、本地无（陈旧点）
    ready_days: int

    @property
    def clean(self) -> bool:
        return not (self.missing or self.stale or self.extra)


def status(*, data_dir: Path | None = None, client=None) -> StatusReport:
    """逐日对账：本地清单 vs Qdrant 实际点数。**两个方向的差集都要报**。"""
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    manifest = load_manifest(base)
    client = client or col.get_client()
    indexed = col.count_by_day(client)

    missing: dict[str, int] = {}
    stale: dict[str, int] = {}
    for day, entry in manifest.items():
        rows = int(entry["rows"])
        if rows < 0:
            continue
        got = indexed.get(day)
        if got is None:
            missing[day] = rows
        elif got != rows:
            stale[day] = got - rows
    extra = {day: count for day, count in indexed.items() if day not in manifest}
    ready = sum(1 for day in manifest if indexed.get(day) == int(manifest[day]["rows"]))
    return StatusReport(missing=missing, stale=stale, extra=extra, ready_days=ready)


