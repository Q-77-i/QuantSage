"""E2b：RBAC 与 admin 视图（配额视图 + 拒绝记录）。

三条判据：

  * **403 而不是 404**——本轮刻意破例（全仓此前零 403）：既有 404 口径保护的是「别人的资源
    是否存在」（防同侪探测），而 admin 面缺的是**角色**、其存在不是秘密。判据与理由写在
    `require_admin` 的 docstring 里，用例只负责钉住它不被改回 404；
  * **视图的读数各有来源**：在飞任务数直查任务表、今日 Agent 调用读计数表、今日被拒读拒绝表——
    三列分别造数据，逐列断言（否则「三列都返回 0」的实现也骗得过）；
  * **未登录仍是 401**（鉴权先于角色），普通用户才是 403。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import quota
from app.main import app

client = TestClient(app)


def seed(db: Any, email: str, *, tasks: int = 0, calls: int = 0, rejections: int = 0) -> dict[str, Any]:
    """给某个账号造三样读数：在飞任务 / 今日调用 / 今日被拒。"""
    user = asyncio.run(db.create_user(email, "hash"))
    for index in range(tasks):
        asyncio.run(
            db.create_task(f"task-{email}-{index}", user["id"], "backtest", {"symbol": "600519"})
        )
    for _ in range(calls):
        asyncio.run(db.bump_usage_counter(user["id"], quota.METRIC_AGENT_CALLS, quota.period_key(), 200))
    for _ in range(rejections):
        asyncio.run(db.record_quota_rejection(user["id"], quota.METRIC_AGENT_CALLS, 200, 200))
    return user


# ── 门 ─────────────────────────────────────────────────────


def test_anonymous_gets_401_not_403(jwt_secret: Any) -> None:
    """鉴权先于角色：没登录是 401，轮不到判角色。"""
    assert client.get("/api/v1/admin/usage").status_code == 401
    assert client.get("/api/v1/admin/rejections").status_code == 401


def test_ordinary_user_gets_403_with_a_readable_detail(signed_in: Any) -> None:
    for path in ("/api/v1/admin/usage", "/api/v1/admin/rejections"):
        response = client.get(path)

        assert response.status_code == 403, path
        assert response.json()["detail"] == "需要管理员权限"  # 前端照抄这句


def test_admin_passes_the_gate(signed_in_admin: Any) -> None:
    assert client.get("/api/v1/admin/usage").status_code == 200
    assert client.get("/api/v1/admin/rejections").status_code == 200


# ── 配额视图 ───────────────────────────────────────────────


def test_usage_view_reports_each_column_from_its_own_source(signed_in_admin: Any) -> None:
    seed(signed_in_admin, "alice@example.com", tasks=2, calls=3, rejections=1)
    seed(signed_in_admin, "bob@example.com")

    body = client.get("/api/v1/admin/usage").json()

    rows = {row["email"]: row for row in body["users"]}
    assert rows["alice@example.com"] == {
        "id": rows["alice@example.com"]["id"],
        "email": "alice@example.com",
        "role": "user",
        "active_tasks": 2,
        "agent_calls_today": 3,
        "rejected_today": 1,
    }
    # 没用量的一行是 0，不是缺列（前端不必写 if）
    assert rows["bob@example.com"]["active_tasks"] == 0
    assert rows["bob@example.com"]["agent_calls_today"] == 0
    assert rows["bob@example.com"]["rejected_today"] == 0


def test_usage_view_reports_the_limits_once_at_the_top(signed_in_admin: Any) -> None:
    """上限是**全局配置**，按用户重复一遍是噪音（实施期收敛，SPEC §3.5 已改）。"""
    body = client.get("/api/v1/admin/usage").json()

    assert body["limits"] == {"active_tasks": 3, "agent_calls_per_day": 200}
    for row in body["users"]:
        assert "limits" not in row


def test_usage_view_counts_only_in_flight_tasks(signed_in_admin: Any) -> None:
    """`done` 的任务不该再占额度——gauge 的语义就是「在飞」。"""
    alice = seed(signed_in_admin, "alice@example.com", tasks=1)
    signed_in_admin.tasks["task-alice@example.com-0"]["status"] = "done"

    rows = client.get("/api/v1/admin/usage").json()["users"]

    assert [row for row in rows if row["id"] == alice["id"]][0]["active_tasks"] == 0


# ── 拒绝记录（审计半边）─────────────────────────────────────


def test_rejections_view_lists_newest_first(signed_in_admin: Any) -> None:
    seed(signed_in_admin, "alice@example.com", rejections=2)

    body = client.get("/api/v1/admin/rejections").json()

    assert len(body["rejections"]) == 2
    for row in body["rejections"]:
        assert row["email"] == "alice@example.com"
        assert row["metric"] == "agent_calls"
        assert (row["used"], row["limit_value"]) == (200, 200)  # 列名原样透出（SPEC §3.5）
        assert "at" in row


def test_rejections_view_accepts_a_limit(signed_in_admin: Any) -> None:
    seed(signed_in_admin, "alice@example.com", rejections=3)

    assert len(client.get("/api/v1/admin/rejections?limit=2").json()["rejections"]) == 2
    # 越界由 FastAPI 的 Query 约束挡下（与全仓既有的参数校验同姿态）
    assert client.get("/api/v1/admin/rejections?limit=0").status_code == 422


def test_rejections_view_is_empty_when_nothing_was_rejected(signed_in_admin: Any) -> None:
    assert client.get("/api/v1/admin/rejections").json() == {"rejections": []}


@pytest.mark.usefixtures("jwt_secret")
def test_admin_role_travels_through_the_dependency_chain(signed_in_admin: Any) -> None:
    """`require_admin` 依赖 `require_user`——测试替身换的是后者，前者也必须跟着换。

    这条守的是「夹具与生产依赖链一致」：若哪天 `require_admin` 自己去解 cookie，
    `signed_in_admin` 会静默失效（403），而所有 admin 用例会一起变红。
    """
    assert signed_in_admin is not None
    assert client.get("/api/v1/admin/usage").status_code == 200
