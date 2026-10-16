"""E3a 体检落档的离线用例（SPEC §4.1）。

一条纪律钉在这里：**台账只放摘要级 checks（id/level/title），detail 不进**——台账要能长年
只增不删，而 detail 里写着行数、标的数、路径，会随数据一起长。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.data import data_health as dh
from app.data import health_log


def checks() -> list[dh.Check]:
    return [
        dh.Check(id="B1", level="info", title="分片对账通过", detail="21 片、2,400 万行"),
        dh.Check(id="X6", level="warn", title="归档上界停在 2026-10-08", detail="8 天前"),
        dh.Check(id="E3", level="error", title="available_at 倒挂", detail="3 行"),
    ]


def test_record_writes_summary_row_and_full_last(tmp_path: Path) -> None:
    row = health_log.record(
        tmp_path, source="scheduler", checks=checks(), duration_ms=5490, delisting={"stopped": 226}
    )

    assert row["error"] == 1 and row["warn"] == 1 and row["info"] == 1
    assert row["source"] == "scheduler"
    assert row["duration_ms"] == 5490
    assert [c["id"] for c in row["checks"]] == ["B1", "X6", "E3"]
    # **detail 不进台账**——这是本模块存在的核心理由之一
    assert set(row["checks"][0]) == {"id", "level", "title"}

    last = health_log.read_last(tmp_path)
    assert last is not None
    # 全文里 detail 必须在（它是「最近一次到底怎么了」的唯一去处）
    assert last["checks"][0]["detail"] == "21 片、2,400 万行"
    assert last["delisting"] == {"stopped": 226}


def test_row_carries_cst_timestamp(tmp_path: Path) -> None:
    at = datetime(2026, 10, 16, 22, 10, 7)
    row = health_log.record(tmp_path, source="scheduler", checks=checks(), duration_ms=1, at=at)
    assert row["at"].endswith("+08:00")
    assert row["at"].startswith("2026-10-16T22:10:07")


def test_runs_append_and_read_newest_first(tmp_path: Path) -> None:
    for i in range(3):
        health_log.record(tmp_path, source="scheduler", checks=checks(), duration_ms=i)

    total, rows = health_log.read_runs(tmp_path, limit=10, offset=0)
    assert total == 3
    assert [r["duration_ms"] for r in rows] == [2, 1, 0]  # 新的在前
    # 分页：total 是总行数，不受 limit 影响
    total, page = health_log.read_runs(tmp_path, limit=1, offset=1)
    assert total == 3
    assert [r["duration_ms"] for r in page] == [1]


def test_read_missing_or_empty_returns_nothing(tmp_path: Path) -> None:
    assert health_log.read_runs(tmp_path) == (0, [])
    assert health_log.read_last(tmp_path) is None


def test_bad_lines_are_skipped_not_raised(tmp_path: Path) -> None:
    """半截写入的坏行不该让看板整体打不开（与 X3 的台账口径一致）。"""
    health_log.record(tmp_path, source="scheduler", checks=checks(), duration_ms=1)
    path = health_log.runs_path(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{不是 JSON\n")
        handle.write("\n")  # 空行也不该算一行

    total, rows = health_log.read_runs(tmp_path)
    assert total == 1
    assert len(rows) == 1


def test_skipped_run_lands_a_row_without_counts(tmp_path: Path) -> None:
    """被写锁挡下的一轮也要留痕——否则「今天怎么没跑」在台账里查不到。"""
    row = health_log.append_skipped(tmp_path, source="scheduler", reason="locked")

    assert row["skipped"] == "locked"
    assert "error" not in row
    total, rows = health_log.read_runs(tmp_path)
    assert total == 1
    assert rows[0]["skipped"] == "locked"


def test_last_json_is_written_atomically(tmp_path: Path) -> None:
    health_log.record(tmp_path, source="scheduler", checks=checks(), duration_ms=1)
    meta = tmp_path / "_meta"
    assert not list(meta.glob("*.partial*"))  # 换入是原子的，不留半截
    assert json.loads(health_log.last_path(tmp_path).read_text(encoding="utf-8"))["source"] == "scheduler"


def test_bad_run_row_does_not_break_last(tmp_path: Path) -> None:
    """台账坏了不影响全文——两者是分开的家。"""
    health_log.record(tmp_path, source="scheduler", checks=checks(), duration_ms=1)
    health_log.runs_path(tmp_path).write_text("坏\n", encoding="utf-8")
    assert health_log.read_runs(tmp_path) == (0, [])
    assert health_log.read_last(tmp_path) is not None


@pytest.mark.parametrize("level", ["error", "warn", "info"])
def test_overall_status_reads_the_worst_level(level: str) -> None:
    """红灯纯函数：error > warn > info，看板与 API 共用这一处判断。"""
    assert dh.overall_status([dh.Check(id="A", level=level, title="t", detail="")]) == {
        "error": "red",
        "warn": "yellow",
        "info": "green",
    }[level]


def test_overall_status_on_empty_is_green() -> None:
    assert dh.overall_status([]) == "green"


def test_read_last_tolerates_a_corrupt_file(tmp_path: Path) -> None:
    """全文文件坏了返回 None，不抛——看板据此显示「还没跑过」，而不是整页崩掉。"""
    health_log.record(tmp_path, source="scheduler", checks=checks(), duration_ms=1)
    health_log.last_path(tmp_path).write_text("{半截", encoding="utf-8")
    assert health_log.read_last(tmp_path) is None
