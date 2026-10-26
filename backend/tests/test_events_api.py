"""S1：`/events` 的有界读路径——默认窗口、limit/truncated、顺序、422、窗口关闭、缓存。

离线用合成 Parquet，不读真实 `data/`。缓存是进程级状态，每条用例前后清 L1。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import events as events_api
from app.core.config import get_settings
from app.data import cache, duckdb_client
from app.main import app
from tests.conftest import ts, write_bars_parquet, write_events_parquet

client = TestClient(app)

#: 冻结「今天」：默认窗口的起点由它推出，测试不随真实日期漂
TODAY = date(2026, 8, 15)

#: 6 条事件：一条在 12 个月窗口外，两条同 `event_time` 用来钉 event_id 次序
EVENTS = [
    {"event_id": "old", "title": "旧事件", "event_time": ts("2024-01-01 09:00:00")},
    {"event_id": "e2", "title": "七月", "event_time": ts("2026-07-01 09:00:00")},
    {"event_id": "e3", "title": "八月一", "event_time": ts("2026-08-01 09:00:00")},
    {"event_id": "e4", "title": "八月二", "event_time": ts("2026-08-02 09:00:00")},
    {"event_id": "evt-z", "title": "同日 Z", "event_time": ts("2026-08-03 10:00:00")},
    {"event_id": "evt-a", "title": "同日 A", "event_time": ts("2026-08-03 10:00:00")},
]


@pytest.fixture
def events_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一份事件语料 + 最小行情；冻结 today、默认窗口 12 个月、清 L1。"""
    cache.clear_local()
    write_events_parquet(tmp_path / "events", "600519", EVENTS)
    write_bars_parquet(
        tmp_path / "bars", "600519", [{"trade_date": TODAY, "open": 100.0}]
    )
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    # `raising=False`：红灯阶段这两个符号还不存在，用例应因「行为缺失」失败，
    # 而不是在 fixture 里先抛 AttributeError
    monkeypatch.setattr(events_api, "_today", lambda: TODAY, raising=False)
    monkeypatch.setenv("EVENTS_DEFAULT_WINDOW_MONTHS", "12")
    get_settings.cache_clear()
    yield tmp_path
    cache.clear_local()
    duckdb_client.reset_connections()
    get_settings.cache_clear()


def _get(**params: object) -> dict:
    return client.get("/api/v1/events", params={"symbol": "600519", **params}).json()


# ── 默认窗口 ────────────────────────────────────────────────


def test_default_window_is_twelve_months_and_is_echoed(events_dir: Path) -> None:
    body = _get()

    assert body["window"] == {"start": "2025-08-15", "end": "2026-08-15"}
    assert body["limit"] == 500
    assert body["truncated"] is False
    assert body["count"] == 5
    # 升序；同 event_time 按 event_id 升序
    assert [event["event_id"] for event in body["events"]] == [
        "e2",
        "e3",
        "e4",
        "evt-a",
        "evt-z",
    ]


def test_explicit_window_reaches_older_events(events_dir: Path) -> None:
    body = _get(start="2023-01-01", end="2024-12-31")

    assert body["window"] == {"start": "2023-01-01", "end": "2024-12-31"}
    assert [event["event_id"] for event in body["events"]] == ["old"]
    assert body["truncated"] is False


def test_default_window_can_be_disabled(events_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`EVENTS_DEFAULT_WINDOW_MONTHS=0`：两侧都无界，退回全史（仍受 limit 约束）。"""
    monkeypatch.setenv("EVENTS_DEFAULT_WINDOW_MONTHS", "0")
    get_settings.cache_clear()

    body = _get()

    assert body["window"] == {"start": None, "end": None}
    assert body["count"] == 6


# ── limit / offset / 截断 ───────────────────────────────────


def test_limit_returns_latest_rows_and_marks_truncated(events_dir: Path) -> None:
    body = _get(limit=2)

    assert body["truncated"] is True
    assert body["count"] == 2
    # DESC 取最近 2 条再反转；同日按 event_id 升序
    assert [event["event_id"] for event in body["events"]] == ["evt-a", "evt-z"]


def test_offset_skips_latest_rows(events_dir: Path) -> None:
    body = _get(limit=2, offset=1)

    assert [event["event_id"] for event in body["events"]] == ["e4", "evt-a"]
    assert body["truncated"] is True


@pytest.mark.parametrize(("params", "status"), [
    ({"limit": 0}, 422),
    ({"limit": 2001}, 422),
    ({"offset": -1}, 422),
    ({"start": "2026-08-10", "end": "2026-08-01"}, 422),
])
def test_out_of_contract_params_are_422(
    events_dir: Path, params: dict, status: int
) -> None:
    response = client.get("/api/v1/events", params={"symbol": "600519", **params})
    assert response.status_code == status


# ── 结果缓存 ────────────────────────────────────────────────


def test_same_query_is_served_from_cache_and_invalidated_by_generation(
    events_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    producer = duckdb_client._events_page_query

    def counted(*args: object, **kwargs: object) -> list[dict]:
        calls.append(1)
        return producer(*args, **kwargs)

    monkeypatch.setattr(duckdb_client, "_events_page_query", counted)

    _get()
    _get()
    assert len(calls) == 1

    # 数据代际变化（清单变）→ 换键 → 重新查
    (events_dir / "_meta").mkdir()
    (events_dir / "_meta" / "events.json").write_text("{}")
    _get()
    assert len(calls) == 2
