"""证据链组装（M7a）：决策的来源快照 → 本地语料行。

**快照字段优先**：决策单上的 `sources` 是决策当时看到的东西（M6 有意留的契约），
展示时标题 / 双时间戳 / 来源三元组一律取它；语料行只**补**快照里没有的两列
（`summary` / `industries`）并给出修订标注。理由：报告是事后复盘，但「当时看到的是什么」
必须能自证——平台改过内容时（M2b：归档是当前版本快照）把差异标出来，而不是悄悄换成新版本。

键是 `(event_id, 事发日)`：`event_id` **只在日分区内唯一**（平台对同题事件复用 id，实测 18 例），
单拿 id 当键会让八月的 CPI 覆盖九月的 CPI。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.data import duckdb_client as dc


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """一条证据：快照字段 + 语料行补充字段 + 两者的对照结论。"""

    event_id: str
    day: date
    title: str | None = None
    summary: str | None = None
    event_time: str | None = None
    available_at: str | None = None
    source: str | None = None
    original_source: str | None = None
    content_hash: str | None = None
    source_url: str | None = None
    industries: tuple[str, ...] = ()
    direction_norm: str | None = None
    #: 本地语料里有没有这一行（没有也要能展示快照，如实标记）
    found: bool = False
    #: 平台改过内容：语料行的 content_hash 与快照不一致
    revised: bool = False
    #: 语料行**现在**的 content_hash（与快照的那个并列展示）
    corpus_hash: str | None = None
    #: 决策 id 列表（同一事件可能驱动多张决策单）
    decision_ids: tuple[str, ...] = field(default=())

    def to_payload(self) -> dict[str, Any]:
        """冻结进报告的 JSON 形状（键名与 M3 检索返回、M6 决策卡一致，前端不学第二套）。"""
        return {
            "event_id": self.event_id,
            "day": self.day.isoformat(),
            "title": self.title,
            "summary": self.summary,
            "event_time": self.event_time,
            "available_at": self.available_at,
            "source": self.source,
            "original_source": self.original_source,
            "content_hash": self.content_hash,
            "source_url": self.source_url,
            "industries": list(self.industries),
            "direction_norm": self.direction_norm,
            "found": self.found,
            "revised": self.revised,
            "corpus_hash": self.corpus_hash,
            "decision_ids": list(self.decision_ids),
        }


def evidence_key(event_id: str | None, event_time: str | None) -> str | None:
    """`(event_id, 事发日)` 组成的证据键；缺一或时间无法解析即 `None`（不成证据项）。"""
    if not event_id or not event_time:
        return None
    try:
        day = datetime.fromisoformat(str(event_time)).date()
    except ValueError:
        return None
    return f"{event_id}|{day.isoformat()}"


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def resolve_evidence(
    snapshots: Iterable[Mapping[str, Any] | None],
    *,
    data_dir: Path | None = None,
    decision_ids: Iterable[str] = (),
) -> dict[str, EvidenceItem]:
    """一批来源快照 → 键到证据项的映射。

    `snapshots` 是决策单上的 `sources`（可为 `None` / 空——卖出决策如实留空）。
    `decision_ids` 与 `snapshots` **一一对应**（同一批来源由哪些决策单引用，用于报告里
    「这条证据驱动了哪几笔决策」）。查无此行的快照照样成项（`found=False`）。
    """
    ids = list(decision_ids) or [""] * 0
    pairs: list[tuple[str, Mapping[str, Any], str]] = []
    for index, snapshot in enumerate(snapshots):
        if not snapshot:
            continue
        key = evidence_key(_text(snapshot.get("event_id")), _text(snapshot.get("event_time")))
        if key is None:
            continue
        owner = ids[index] if index < len(ids) else ""
        pairs.append((key, snapshot, owner))

    if not pairs:
        return {}

    wanted = {key for key, _, _ in pairs}
    wanted_ids = sorted({snapshot["event_id"] for _, snapshot, _ in pairs})
    corpus = dc.events_by_ids([str(eid) for eid in wanted_ids], data_dir=data_dir)
    by_key: dict[str, Mapping[str, Any]] = {}
    for row in corpus:
        row_key = evidence_key(_text(row.get("event_id")), _text(row.get("event_time")))
        if row_key in wanted and row_key not in by_key:
            by_key[row_key] = row

    items: dict[str, EvidenceItem] = {}
    for key, snapshot, owner in pairs:
        event_id = str(snapshot["event_id"])
        day = datetime.fromisoformat(str(snapshot["event_time"])).date()
        row = by_key.get(key)
        snapshot_hash = _text(snapshot.get("content_hash"))
        corpus_hash = _text(row.get("content_hash")) if row else None
        existing = items.get(key)
        owners = (*existing.decision_ids, owner) if existing else ((owner,) if owner else ())
        items[key] = EvidenceItem(
            event_id=event_id,
            day=day,
            # 快照字段优先（决策当时所见）
            title=_text(snapshot.get("title")),
            event_time=_text(snapshot.get("event_time")),
            available_at=_text(snapshot.get("available_at")),
            source=_text(snapshot.get("source")),
            original_source=_text(snapshot.get("original_source")),
            content_hash=snapshot_hash,
            source_url=_text(snapshot.get("source_url")),
            # 语料行补充（快照里没有的两列）+ 对照结论
            summary=_text(row.get("summary")) if row else None,
            industries=tuple(row.get("industries") or ()) if row else (),
            direction_norm=_text(row.get("direction_norm")) if row else None,
            found=row is not None,
            revised=bool(row) and snapshot_hash is not None and corpus_hash != snapshot_hash,
            corpus_hash=corpus_hash,
            decision_ids=owners,
        )
    return items
