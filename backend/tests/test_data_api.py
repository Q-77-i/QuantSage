"""E3a 数据看板端点（`/api/v1/data/health*`）。

三条判据：

  * **门与 ETL 面同一道**：未登录 401、普通用户 403、admin 200——鉴权先于角色，
    夹具刻意用普通用户而不是 `signed_in_admin`（后者会让「非 admin 被拒」永远绿）；
  * **「还没跑过」是答案不是错误**：`last` 回 `null` 而不是 404（同 `/market/{symbol}/probe`
    的 `has_data: false` 口径）；
  * **红灯由服务端算**：`check_status` 出在响应里，前端不重算（重算就会有两套口径）。

数据目录用 `DATA_DIR` 注入——这是唯一能同时穿过配置层与 `health_log` 默认路径的开关。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.data import data_health as dh
from app.data import health_log
from app.main import app

client = TestClient(app)


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def seed_run(
    root: Path,
    *,
    at: datetime,
    levels: tuple[str, ...] = ("info",),
    duration_ms: int = 5490,
) -> dict:
    """在数据目录里落一轮体检（走真实落档路径，不手搓 JSON）。"""
    checks = [
        dh.Check(id=f"C{i}", level=level, title=f"第 {i} 项", detail=f"细节 {i}")  # type: ignore[arg-type]
        for i, level in enumerate(levels)
    ]
    return health_log.record(
        root, source="scheduler", checks=checks, duration_ms=duration_ms, at=at
    )


# ── 门 ─────────────────────────────────────────────────────


def test_anonymous_gets_401_not_403(jwt_secret: Any, data_dir: Path) -> None:
    assert client.get("/api/v1/data/health").status_code == 401
    assert client.get("/api/v1/data/health/history").status_code == 401


def test_regular_user_gets_403(signed_in: Any, data_dir: Path) -> None:
    """普通用户被挡（403）——**门在服务端**，前端隐藏入口不算门。"""
    assert client.get("/api/v1/data/health").status_code == 403
    assert client.get("/api/v1/data/health/history").status_code == 403


# ── 首屏 ───────────────────────────────────────────────────


def test_last_is_null_before_any_run(signed_in_admin: Any, data_dir: Path) -> None:
    """**还没跑过是答案不是错误**——200 + `last: null`，而不是 404。"""
    body = client.get("/api/v1/data/health").json()

    assert body["last"] is None
    assert body["scheduler"]["hour"] == 22  # 时刻照常给（它不依赖跑没跑过）


def test_admin_gets_the_last_run_with_server_side_status(
    signed_in_admin: Any, data_dir: Path
) -> None:
    seed_run(data_dir, at=datetime(2026, 10, 16, 22, 10, 7), levels=("info", "warn"))
    body = client.get("/api/v1/data/health").json()

    assert body["last"]["duration_ms"] == 5490
    assert body["last"]["warn"] == 1
    # 红灯**由服务端算**，前端不重算
    assert body["last"]["check_status"] == "yellow"


def test_error_level_turns_the_board_red(signed_in_admin: Any, data_dir: Path) -> None:
    seed_run(data_dir, at=datetime(2026, 10, 16, 22, 10, 7), levels=("error", "warn"))
    body = client.get("/api/v1/data/health").json()

    assert body["last"]["check_status"] == "red"


def test_missing_data_directory_still_answers(signed_in_admin: Any, data_dir: Path) -> None:
    """数据没落盘 ⇒ `freshness` 回 null，但**端点照常 200**——那种情况下「体检报了什么」
    正是看板最该显示的东西（体检本身不依赖 DuckDB 能不能连上）。"""
    body = client.get("/api/v1/data/health").json()

    assert body["freshness"] is None
    assert "stale_source" in body


def test_source_stall_uses_the_check_itself(signed_in_admin: Any, data_dir: Path) -> None:
    """源侧边界直接用 X7 当读数源——没有 `bars_sync.json` 时是「无从判断」，不是 0。"""
    body = client.get("/api/v1/data/health").json()

    assert body["stale_source"]["stalled_trading_days"] is None
    assert body["stale_source"]["source_last"] is None


# ── 历史 ───────────────────────────────────────────────────


def test_history_is_newest_first(signed_in_admin: Any, data_dir: Path) -> None:
    seed_run(data_dir, at=datetime(2026, 10, 14, 22, 10), duration_ms=11)
    seed_run(data_dir, at=datetime(2026, 10, 15, 22, 10), duration_ms=22)
    seed_run(data_dir, at=datetime(2026, 10, 16, 22, 10), duration_ms=33)

    body = client.get("/api/v1/data/health/history").json()

    assert body["total"] == 3
    assert [r["duration_ms"] for r in body["runs"]] == [33, 22, 11]


def test_history_paginates(signed_in_admin: Any, data_dir: Path) -> None:
    for i in range(5):
        seed_run(data_dir, at=datetime(2026, 10, 10 + i, 22, 10), duration_ms=i)

    body = client.get("/api/v1/data/health/history", params={"limit": 2, "offset": 1}).json()

    assert body["total"] == 5  # total 是总行数，不受 limit 影响（前端靠它算总页数）
    assert body["limit"] == 2 and body["offset"] == 1
    assert [r["duration_ms"] for r in body["runs"]] == [3, 2]


def test_history_skipped_row_has_no_status(signed_in_admin: Any, data_dir: Path) -> None:
    """被写锁跳过的一轮**不假装有结论**：`check_status` 为 null，但行必须在。"""
    health_log.append_skipped(data_dir, source="scheduler", reason="locked")
    body = client.get("/api/v1/data/health/history").json()

    assert body["total"] == 1
    assert body["runs"][0]["skipped"] == "locked"
    assert body["runs"][0]["check_status"] is None


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_history_rejects_out_of_range(
    signed_in_admin: Any, data_dir: Path, params: dict[str, int]
) -> None:
    assert client.get("/api/v1/data/health/history", params=params).status_code == 422
