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
        "agent_calls": 3,
        "rejected": 1,
    }
    # 没用量的一行是 0，不是缺列（前端不必写 if）
    assert rows["bob@example.com"]["active_tasks"] == 0
    assert rows["bob@example.com"]["agent_calls"] == 0
    assert rows["bob@example.com"]["rejected"] == 0


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
    # 分页三键自 E2c 起随行（`total=0` 与空列表是同一件事，前端据此画「还没有记录」）
    assert client.get("/api/v1/admin/rejections").json() == {
        "total": 0,
        "offset": 0,
        "limit": 50,
        "rejections": [],
    }


@pytest.mark.usefixtures("jwt_secret")
def test_admin_role_travels_through_the_dependency_chain(signed_in_admin: Any) -> None:
    """`require_admin` 依赖 `require_user`——测试替身换的是后者，前者也必须跟着换。

    这条守的是「夹具与生产依赖链一致」：若哪天 `require_admin` 自己去解 cookie，
    `signed_in_admin` 会静默失效（403），而所有 admin 用例会一起变红。
    """
    assert signed_in_admin is not None
    assert client.get("/api/v1/admin/usage").status_code == 200


# ── 分页与搜索（E2c，用户 2026-10-16 拍板 C 案）────────────────


def test_usage_view_paginates(signed_in_admin: Any) -> None:
    for index in range(7):
        seed(signed_in_admin, f"user{index}@example.com")

    first = client.get("/api/v1/admin/usage?limit=3").json()
    second = client.get("/api/v1/admin/usage?limit=3&offset=3").json()

    assert first["total"] == second["total"] == 7  # total 是**过滤后**的命中数，不随页变
    assert (first["limit"], first["offset"]) == (3, 0)
    assert (second["limit"], second["offset"]) == (3, 3)
    assert [row["email"] for row in first["users"]] == [
        "user0@example.com",
        "user1@example.com",
        "user2@example.com",
    ]
    assert [row["email"] for row in second["users"]] == [
        "user3@example.com",
        "user4@example.com",
        "user5@example.com",
    ]
    # 页与页**不重叠**（offset 真的落到了 SQL 上，不是取回全量再切片）
    assert not set(row["id"] for row in first["users"]) & set(
        row["id"] for row in second["users"]
    )


def test_usage_view_searches_by_email_case_insensitively(signed_in_admin: Any) -> None:
    seed(signed_in_admin, "alice@example.com")
    seed(signed_in_admin, "bob@example.com")
    seed(signed_in_admin, "Alice.Smith@example.com")

    body = client.get("/api/v1/admin/usage?q=ALICE").json()

    assert body["total"] == 2  # 不区分大小写
    assert [row["email"] for row in body["users"]] == [
        "alice@example.com",
        "Alice.Smith@example.com",
    ]


def test_usage_view_treats_wildcards_as_literal(signed_in_admin: Any) -> None:
    """`%` / `_` 是 LIKE 的通配符，但用户搜的是**字符**——不转义就会搜「a_b」命中「aXb」。"""
    seed(signed_in_admin, "a_b@example.com")
    seed(signed_in_admin, "aXb@example.com")

    body = client.get("/api/v1/admin/usage?q=a_b").json()

    assert [row["email"] for row in body["users"]] == ["a_b@example.com"]


def test_usage_view_with_no_match_is_empty_not_everything(signed_in_admin: Any) -> None:
    seed(signed_in_admin, "alice@example.com")

    body = client.get("/api/v1/admin/usage?q=nobody").json()

    assert body["total"] == 0
    assert body["users"] == []


def test_usage_view_rejects_out_of_range_params(signed_in_admin: Any) -> None:
    assert client.get("/api/v1/admin/usage?limit=0").status_code == 422
    assert client.get("/api/v1/admin/usage?limit=500").status_code == 422
    assert client.get("/api/v1/admin/usage?offset=-1").status_code == 422


def test_rejections_view_paginates(signed_in_admin: Any) -> None:
    seed(signed_in_admin, "alice@example.com", rejections=5)

    first = client.get("/api/v1/admin/rejections?limit=2").json()
    last = client.get("/api/v1/admin/rejections?limit=2&offset=4").json()

    assert first["total"] == last["total"] == 5
    assert len(first["rejections"]) == 2
    assert len(last["rejections"]) == 1  # 最后一页不满也是 1 条，不是补空
    assert not set(row["id"] for row in first["rejections"]) & set(
        row["id"] for row in last["rejections"]
    )


def test_usage_view_filters_by_role(signed_in_admin: Any) -> None:
    alice = seed(signed_in_admin, "alice@example.com")
    seed(signed_in_admin, "bob@example.com")
    signed_in_admin.users[alice["id"]]["role"] = "admin"

    body = client.get("/api/v1/admin/usage?role=admin").json()

    assert body["total"] == 1  # total 与行同一份过滤，不是「先数全部再筛」
    assert [row["email"] for row in body["users"]] == ["alice@example.com"]
    assert client.get("/api/v1/admin/usage?role=user").json()["total"] == 1
    assert client.get("/api/v1/admin/usage").json()["total"] == 2  # 留空 = 全部


def test_usage_view_filters_to_accounts_with_readings(signed_in_admin: Any) -> None:
    """一屏全 0 的那种账号（历史探针）不该占位置——三读数任一非零才算「有读数」。"""
    seed(signed_in_admin, "busy@example.com", calls=1)
    seed(signed_in_admin, "quiet@example.com")

    body = client.get("/api/v1/admin/usage?active_only=true").json()

    assert body["total"] == 1
    assert [row["email"] for row in body["users"]] == ["busy@example.com"]


def test_usage_view_combines_search_and_both_filters(signed_in_admin: Any) -> None:
    """三者叠加：搜索 ∩ 角色 ∩ 有读数——`total` 与行必须来自同一次过滤。"""
    busy = seed(signed_in_admin, "alice@example.com", calls=2)
    seed(signed_in_admin, "alice-silent@example.com")  # 同样匹配 alice，但没有读数
    signed_in_admin.users[busy["id"]]["role"] = "admin"

    body = client.get("/api/v1/admin/usage?q=alice&role=admin&active_only=true").json()

    assert body["total"] == 1
    assert [row["email"] for row in body["users"]] == ["alice@example.com"]


def test_usage_view_rejects_unknown_role(signed_in_admin: Any) -> None:
    """角色只收两个值——写错该 422，而不是静默当成「全部」（那会让筛选看着生效其实没生效）。"""
    assert client.get("/api/v1/admin/usage?role=root").status_code == 422


def test_usage_view_sorts_by_readings(signed_in_admin: Any) -> None:
    """按读数排序：分页与排序同侧（只排当前页等于没排）。"""
    seed(signed_in_admin, "quiet@example.com")
    seed(signed_in_admin, "busy@example.com", calls=5)
    seed(signed_in_admin, "mid@example.com", calls=2)

    desc = client.get("/api/v1/admin/usage?sort=agent_calls&order=desc").json()
    asc = client.get("/api/v1/admin/usage?sort=agent_calls&order=asc").json()

    assert [row["email"] for row in desc["users"]][:3] == [
        "busy@example.com",
        "mid@example.com",
        "quiet@example.com",
    ]
    assert [row["email"] for row in asc["users"]][:3] == [
        "quiet@example.com",
        "mid@example.com",
        "busy@example.com",
    ]


def test_usage_view_sort_keeps_pagination_stable(signed_in_admin: Any) -> None:
    """全 0 的账号之间**顺序必须确定**（按 id 兜底）——否则翻页会看到重复或漏行。"""
    for index in range(6):
        seed(signed_in_admin, f"user{index}@example.com")

    first = client.get("/api/v1/admin/usage?sort=agent_calls&limit=3").json()["users"]
    second = client.get(
        "/api/v1/admin/usage?sort=agent_calls&limit=3&offset=3"
    ).json()["users"]

    assert [row["email"] for row in first] == [f"user{i}@example.com" for i in range(3)]
    assert [row["email"] for row in second] == [f"user{i}@example.com" for i in range(3, 6)]


def test_usage_view_sorts_by_email(signed_in_admin: Any) -> None:
    seed(signed_in_admin, "carol@example.com")
    seed(signed_in_admin, "alice@example.com")
    seed(signed_in_admin, "bob@example.com")

    body = client.get("/api/v1/admin/usage?sort=email&order=desc").json()

    assert [row["email"] for row in body["users"]] == [
        "carol@example.com",
        "bob@example.com",
        "alice@example.com",
    ]


def test_usage_view_rejects_unknown_sort_key(signed_in_admin: Any) -> None:
    """白名单挡在 422——`ORDER BY` 拼的是标识符，绝不能把参数直接拼进去。"""
    assert client.get("/api/v1/admin/usage?sort=password_hash").status_code == 422
    assert client.get("/api/v1/admin/usage?order=sideways").status_code == 422


# ── 时间窗（E2c 追加：今日 / 近 7 天 / 近 30 天）────────────────


def _two_days_ago() -> str:
    """窗内的某一天（不是今天）——用它证明「近 7 天」真的把多天加起来了。"""
    from datetime import timedelta

    from app.core import quota

    return quota.period_key(quota.day_start() - timedelta(days=2))


def test_usage_view_sums_the_window(signed_in_admin: Any) -> None:
    user = seed(signed_in_admin, "alice@example.com", calls=3)  # 今天 3 次
    signed_in_admin.usage_counters[(user["id"], "agent_calls", _two_days_ago())] = 4

    today = client.get("/api/v1/admin/usage?days=1&q=alice").json()["users"][0]
    week = client.get("/api/v1/admin/usage?days=7&q=alice").json()["users"][0]

    assert today["agent_calls"] == 3  # 今日是窗口的 n=1 特例
    assert week["agent_calls"] == 7  # 近 7 天把两天加起来
    assert week["active_tasks"] == today["active_tasks"] == 0  # gauge **不随窗口变**


def test_usage_view_window_respects_its_start(signed_in_admin: Any) -> None:
    """窗口外的日子不算——否则「近 7 天」会把上个月的一起加进来。"""
    from datetime import timedelta

    from app.core import quota

    user = seed(signed_in_admin, "alice@example.com", calls=1)
    signed_in_admin.usage_counters[(user["id"], "agent_calls", _two_days_ago())] = 4
    signed_in_admin.usage_counters[
        (user["id"], "agent_calls", quota.period_key(quota.day_start() - timedelta(days=40)))
    ] = 100

    assert client.get("/api/v1/admin/usage?days=7&q=alice").json()["users"][0][
        "agent_calls"
    ] == 5
    assert client.get("/api/v1/admin/usage?days=30&q=alice").json()["users"][0][
        "agent_calls"
    ] == 5  # 40 天前那条仍在窗外
    assert client.get("/api/v1/admin/usage?days=7&q=alice").json()["users"][0][
        "rejected"
    ] == 0


def test_usage_view_window_applies_to_rejections_too(signed_in_admin: Any) -> None:
    """两个计数列**同一起点**（一个按 period_key、一个按 at，起点必须是同一刻）。"""
    seed(signed_in_admin, "alice@example.com", rejections=2)

    assert client.get("/api/v1/admin/usage?days=1&q=alice").json()["users"][0]["rejected"] == 2
    assert client.get("/api/v1/admin/usage?days=7&q=alice").json()["users"][0]["rejected"] == 2


def test_usage_view_window_only_accepts_the_three_buckets(signed_in_admin: Any) -> None:
    assert client.get("/api/v1/admin/usage?days=3").status_code == 422
    assert client.get("/api/v1/admin/usage?days=365").status_code == 422


def test_usage_window_is_a_pure_function() -> None:
    """跨月由日期运算兜住（窗口起点直接减天数，不做「同月」假设）。"""
    from datetime import datetime

    from app.core import quota

    moment = datetime(2026, 11, 3, 10, 0, tzinfo=quota.CN_TZ)
    assert quota.window(1, moment)[:2] == ("2026-11-03", "2026-11-03")
    assert quota.window(7, moment)[:2] == ("2026-10-28", "2026-11-03")
    assert quota.window(30, moment)[:2] == ("2026-10-05", "2026-11-03")
