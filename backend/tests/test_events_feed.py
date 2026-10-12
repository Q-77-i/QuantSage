"""M10b 事件日历与快讯流两条端点：形状、聚合、分页确定性、类型白名单。

三条口径各有对应用例：

  * **只含真有事件的日子**——「那天没有事件」与「那天不在窗口里」是两回事，补零会把前者
    画成零条的柱子，读起来像数据缺失；
  * **分页是确定的**——只按 `importance_score` 排序时同分条目的顺序由数据库定，翻页会出现
    「第 2 页重复了第 1 页」。排序键必须是 `(importance_score DESC, event_id)`；
  * **类型白名单非法取值 422，不静默忽略**——静默忽略会让「怎么一条都没有」变成谜。

离线继续用**合成 Parquet**，不读真实数据。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client
from app.main import app
from tests.conftest import ts, write_events_parquet

client = TestClient(app)

DAY1 = date(2026, 8, 3)
DAY2 = date(2026, 8, 4)
#: 第三天**一条事件都没有**——用它钉「空日子不出现在日期带里」
DAY3 = date(2026, 8, 5)

#: 同一分数组里放三条（`d3`/`d4`/`d5` 都是 70），专门用来逼出「只按分数排 ⇒ 顺序不稳」。
EVENTS = [
    {"event_id": "n1", "event_type": "news", "importance_score": 90.0, "title": "头条新闻",
     "event_time": ts("2026-08-03 09:30:00"), "available_at": ts("2026-08-03 09:31:00"),
     "source": "xiaoshi-archive", "original_source": "财联社", "content_hash": "hash-n1",
     "industries": ["半导体"], "symbols": ["600519"], "direction_norm": "bullish"},
    {"event_id": "n2", "event_type": "news", "importance_score": 50.0, "title": "次条新闻",
     "event_time": ts("2026-08-03 14:00:00"), "available_at": ts("2026-08-03 14:02:00")},
    {"event_id": "a1", "event_type": "announcement", "importance_score": 100.0, "title": "满分公告",
     "event_time": ts("2026-08-03 08:00:00"), "available_at": ts("2026-08-03 08:00:30")},
    {"event_id": "p1", "event_type": "policy", "importance_score": 5.0, "title": "低分政策",
     "event_time": ts("2026-08-03 10:00:00"), "available_at": ts("2026-08-03 10:00:00")},
    {"event_id": "d3", "event_type": "news", "importance_score": 70.0, "title": "并列甲",
     "event_time": ts("2026-08-04 09:00:00"), "available_at": ts("2026-08-04 09:00:00")},
    {"event_id": "d4", "event_type": "news", "importance_score": 70.0, "title": "并列乙",
     "event_time": ts("2026-08-04 10:00:00"), "available_at": ts("2026-08-04 10:00:00")},
    {"event_id": "d5", "event_type": "news", "importance_score": 70.0, "title": "并列丙",
     "event_time": ts("2026-08-04 11:00:00"), "available_at": ts("2026-08-04 11:00:00")},
]


@pytest.fixture
def events_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一份事件语料 + 一份最小行情（`dc.connect` 要求 bars/ 与 events/ 都有 Parquet）。"""
    write_events_parquet(tmp_path / "events", "cn", EVENTS)
    from tests.conftest import write_bars_parquet

    write_bars_parquet(
        tmp_path / "bars", "600519", [{"trade_date": DAY1, "open": 100.0, "change_pct": 0.0}]
    )
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    return tmp_path


# ── 日期带 ────────────────────────────────────────────────────────────────


def test_calendar_aggregates_per_day(events_dir: Path) -> None:
    body = client.get(
        "/api/v1/events/calendar", params={"start": "2026-08-01", "end": "2026-08-07"}
    ).json()
    days = {row["date"]: row for row in body["days"]}
    assert days["2026-08-03"]["total"] == 4
    assert days["2026-08-03"]["by_type"] == {"news": 2, "announcement": 1, "policy": 1}
    assert days["2026-08-04"]["total"] == 3


def test_calendar_omits_days_without_events(events_dir: Path) -> None:
    """8-05 一条都没有 ⇒ 它**不在** days 里（补零会被读成「那天数据缺失」）。"""
    body = client.get(
        "/api/v1/events/calendar", params={"start": "2026-08-01", "end": "2026-08-07"}
    ).json()
    assert "2026-08-05" not in {row["date"] for row in body["days"]}
    assert len(body["days"]) == 2


def test_calendar_high_score_threshold_is_the_callers(events_dir: Path) -> None:
    """「高分」是展示口径，不是数据事实——阈值由查询参数给。"""
    day3 = "2026-08-03"
    default = client.get(
        "/api/v1/events/calendar", params={"start": day3, "end": day3}
    ).json()["days"][0]
    assert default["high"] == 2  # 90 与 100
    strict = client.get(
        "/api/v1/events/calendar", params={"start": day3, "end": day3, "high_score": 95}
    ).json()["days"][0]
    assert strict["high"] == 1  # 只剩 100


def test_calendar_defaults_to_corpus_end(events_dir: Path) -> None:
    """`end` 缺省取语料里最后一个有事件的日子——查出来的，不写死今天。"""
    body = client.get("/api/v1/events/calendar").json()
    assert body["end"] == DAY2.isoformat()
    assert body["start"] == "2026-07-06"  # DAY2 往前 29 个自然日


def test_calendar_high_score_out_of_range_is_422(events_dir: Path) -> None:
    assert client.get("/api/v1/events/calendar", params={"high_score": 120}).status_code == 422


# ── 快讯流 ────────────────────────────────────────────────────────────────


def test_feed_orders_by_importance_desc(events_dir: Path) -> None:
    body = client.get(
        "/api/v1/events/feed", params={"date": DAY1.isoformat()}
    ).json()
    assert [row["event_id"] for row in body["items"]] == ["a1", "n1", "n2", "p1"]
    assert body["total"] == 4 and body["count"] == 4


def test_feed_pagination_is_deterministic_and_disjoint(events_dir: Path) -> None:
    """**同分条目**必须给出确定的顺序——否则翻页会重复或漏条。

    夹具里 d3/d4/d5 都是 70 分。只按分数排时顺序由数据库定，这个用例会间歇性红。
    """
    pages = [
        client.get(
            "/api/v1/events/feed", params={"date": DAY2.isoformat(), "limit": 2, "offset": off}
        ).json()
        for off in (0, 2)
    ]
    first = [row["event_id"] for row in pages[0]["items"]]
    second = [row["event_id"] for row in pages[1]["items"]]
    assert first == ["d3", "d4"]  # event_id 升序兜底
    assert second == ["d5"]
    assert not set(first) & set(second)

    # 再取一次第一页：逐条相等（分页是确定的，不是「这次恰好如此」）
    again = client.get(
        "/api/v1/events/feed", params={"date": DAY2.isoformat(), "limit": 2, "offset": 0}
    ).json()
    assert [row["event_id"] for row in again["items"]] == first


def test_feed_type_whitelist(events_dir: Path) -> None:
    body = client.get(
        "/api/v1/events/feed",
        params={"date": DAY1.isoformat(), "types": ["news", "policy"]},
    ).json()
    assert [row["event_id"] for row in body["items"]] == ["n1", "n2", "p1"]
    assert body["total"] == 3


def test_feed_excluding_announcement_is_what_makes_it_a_feed(events_dir: Path) -> None:
    """**默认排除公告是调用方的事**：不传 types 时公告会霸榜（满分 100 压住新闻的 90）。"""
    all_types = client.get("/api/v1/events/feed", params={"date": DAY1.isoformat()}).json()
    assert all_types["items"][0]["event_type"] == "announcement"
    feed = client.get(
        "/api/v1/events/feed",
        params={"date": DAY1.isoformat(), "types": ["news", "policy"]},
    ).json()
    assert feed["items"][0]["event_type"] == "news"


def test_feed_unknown_type_is_422(events_dir: Path) -> None:
    """**不静默忽略**：静默会让「怎么一条都没有」变成无从追查的谜。"""
    response = client.get(
        "/api/v1/events/feed", params={"date": DAY1.isoformat(), "types": ["sports"]}
    )
    assert response.status_code == 422
    assert "sports" in response.json()["detail"]


def test_feed_min_importance(events_dir: Path) -> None:
    body = client.get(
        "/api/v1/events/feed", params={"date": DAY1.isoformat(), "min_importance": 60}
    ).json()
    assert [row["event_id"] for row in body["items"]] == ["a1", "n1"]


def test_feed_carries_the_evidence_fields(events_dir: Path) -> None:
    """字段集必须够 M7 的 `evidenceRows` 直接吃——两处展示同一条事件的来源，不各写一套。"""
    row = client.get("/api/v1/events/feed", params={"date": DAY1.isoformat()}).json()["items"][1]
    assert row["event_id"] == "n1"
    assert row["event_time"] == "2026-08-03T09:30:00+08:00"
    assert row["available_at"] == "2026-08-03T09:31:00+08:00"
    assert row["source"] == "xiaoshi-archive"
    assert row["original_source"] == "财联社"
    assert row["content_hash"] == "hash-n1"
    assert row["industries"] == ["半导体"]
    assert row["direction_norm"] == "bullish"
    # 标的带名称；字典里没有的就给 null，不编
    assert row["symbols"] == [{"code": "600519", "name": None}]


def test_feed_defaults_to_last_day_with_events(events_dir: Path) -> None:
    body = client.get("/api/v1/events/feed").json()
    assert body["date"] == DAY2.isoformat()
    assert body["total"] == 3


def test_feed_past_the_end_returns_empty_not_404(events_dir: Path) -> None:
    """某天没有事件是**有意义的空**，不是错误（同 `/events` 的既有口径）。"""
    body = client.get("/api/v1/events/feed", params={"date": DAY3.isoformat()}).json()
    assert body["total"] == 0 and body["items"] == []


def test_feed_limit_out_of_range_is_422(events_dir: Path) -> None:
    assert client.get("/api/v1/events/feed", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/events/feed", params={"limit": 500}).status_code == 422


def test_feed_503_when_data_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据没落盘是「依赖没就绪」（503），不是 500。"""
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path / "空")
    assert client.get("/api/v1/events/feed").status_code == 503
    assert client.get("/api/v1/events/calendar").status_code == 503
