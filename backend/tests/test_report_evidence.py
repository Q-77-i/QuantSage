"""M7a 证据链组装（`app.report.evidence`）：决策的来源快照 → 本地语料行。

三条口径在这里钉死：① 快照字段优先（决策当时看到的就是这些）；② 语料行只**补**
摘要与行业（快照里没有），并给修订标注；③ 查无此行如实 `found=False`，不编。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.report.evidence import evidence_key, resolve_evidence
from tests.conftest import make_backtest_dir, ts, write_events_parquet

SNAPSHOT = {
    "event_id": "news:1",
    "title": "标题（决策时快照）",
    "event_time": "2026-09-28 15:48:00+08:00",
    "available_at": "2026-09-28 16:23:03+08:00",
    "source": "xiaoshi-archive",
    "original_source": "东方财富个股",
    "content_hash": "hash-a",
    "source_url": "https://example.com/a",
}

CORPUS_ROW = {
    "event_id": "news:1",
    "event_time": ts("2026-09-28 15:48:00"),
    "available_at": ts("2026-09-28 16:23:03"),
    "title": "平台上的当前标题",
    "summary": "语料行的摘要",
    "industries": ["银行"],
    "direction_norm": "bullish",
    "source": "xiaoshi-archive",
    "original_source": "东方财富个股",
    "source_url": "https://example.com/a",
    "content_hash": "hash-a",
}


def _corpus(tmp_path: Path, rows: list[dict], day: str = "d1") -> Path:
    make_backtest_dir(
        tmp_path,
        bars=[{"trade_date": date(2026, 9, 28), "open": 10.0}],
        events=rows,
    )
    return tmp_path


def test_evidence_key_needs_both_event_id_and_day() -> None:
    """`event_id` 不是全局唯一键（M2b）——键必须带事发日；缺一即 None。"""
    assert evidence_key("news:1", "2026-09-28 15:48:00+08:00") == "news:1|2026-09-28"
    assert evidence_key(None, "2026-09-28 15:48:00+08:00") is None
    assert evidence_key("news:1", None) is None
    assert evidence_key("news:1", "不是时间") is None


def test_resolved_evidence_keeps_snapshot_and_adds_corpus_fields(tmp_path: Path) -> None:
    """命中语料行：标题等取**快照**（决策当时所见），摘要/行业由语料行补上。"""
    resolved = resolve_evidence([SNAPSHOT], data_dir=_corpus(tmp_path, [CORPUS_ROW]))

    item = resolved["news:1|2026-09-28"]
    assert item.found
    assert not item.revised
    assert item.title == "标题（决策时快照）"
    assert item.summary == "语料行的摘要"
    assert item.industries == ("银行",)
    assert item.direction_norm == "bullish"
    assert item.content_hash == "hash-a"
    assert item.corpus_hash == "hash-a"
    assert item.day == date(2026, 9, 28)


def test_revision_flag_fires_when_platform_changed_the_content(tmp_path: Path) -> None:
    """平台修订过内容（语料行 content_hash 与快照不一致）⇒ 标注，两个 hash 都留着。

    真实数据实测 22/22 一致（噪声为零）——这条是**廉价保险**，不是日常路径。
    """
    changed = {**CORPUS_ROW, "content_hash": "hash-b", "summary": "被改过的摘要"}
    resolved = resolve_evidence([SNAPSHOT], data_dir=_corpus(tmp_path, [changed]))

    item = resolved["news:1|2026-09-28"]
    assert item.found
    assert item.revised
    assert item.content_hash == "hash-a"  # 快照里的那个
    assert item.corpus_hash == "hash-b"  # 语料行现在的那个


def test_missing_row_is_reported_not_invented(tmp_path: Path) -> None:
    """本地语料查无此行：如实 `found=False`，摘要留空，快照字段照常展示。"""
    other = {**CORPUS_ROW, "event_id": "news:999"}
    resolved = resolve_evidence([SNAPSHOT], data_dir=_corpus(tmp_path, [other]))

    item = resolved["news:1|2026-09-28"]
    assert not item.found
    assert not item.revised
    assert item.summary is None
    assert item.industries == ()
    assert item.title == "标题（决策时快照）"


def test_same_event_id_on_two_days_stays_two_items(tmp_path: Path) -> None:
    """平台对同题事件复用 `event_id`（M2b 实测 18 例）——两条快照各成一键，不互相覆盖。"""
    write_events_parquet(
        tmp_path / "events",
        "second",
        [
            {
                **CORPUS_ROW,
                "event_time": ts("2026-08-28 15:48:00"),
                "summary": "八月的同号事件",
            }
        ],
    )
    august = {
        **SNAPSHOT,
        "event_time": "2026-08-28 15:48:00+08:00",
        "content_hash": "hash-old",
    }
    resolved = resolve_evidence(
        [SNAPSHOT, august], data_dir=_corpus(tmp_path, [CORPUS_ROW])
    )

    assert set(resolved) == {"news:1|2026-09-28", "news:1|2026-08-28"}
    assert resolved["news:1|2026-08-28"].summary == "八月的同号事件"
    assert resolved["news:1|2026-09-28"].summary == "语料行的摘要"


def test_empty_and_none_sources_are_skipped(tmp_path: Path) -> None:
    """卖出决策没有来源（如实留空）——不产生证据项，也不报错。"""
    assert resolve_evidence([None, {}], data_dir=_corpus(tmp_path, [CORPUS_ROW])) == {}
