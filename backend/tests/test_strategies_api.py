"""离线单测：策略 CRUD、静态检查与模板（M4c）。

落库与归属在离线侧只能验到「端点把什么交给业务库」——真写进 JSONB 再由 SQL 抽回来，
是 `tests/integration/test_strategies_m4c.py` 的事。这里守四件事：

  * **草稿可存**：语法错的源码也该 201，findings 随响应返回（闸门在运行前，不在保存时）；
  * **归属**：列表只回本人的、越权与不存在同为 404（不泄露存在性）；
  * **唯一约束**：撞名 409 靠 `FakeDatabase` 照抄 `UNIQUE(user_id, name)` 的语义，
    否则这条用例测的是替身自己的宽容；
  * **`{findings, meta}` 同源**：参数表单的 schema 与编辑器标注出自同一次响应。

跑真沙箱的那些在 `test_backtest_user.py`（要真子进程 + 合成 Parquet），这里一条不 spawn。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.core.db import StrategyNameTaken
from app.main import app
from tests.conftest import TEST_USER
from tests.fakes import FakeDatabase

client = TestClient(app)

GOOD_CODE = '''\
PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"}}
USES_EVENTS = False


def on_bar(ctx, p):
    return []
'''

SYNTAX_ERROR_CODE = "def on_bar(ctx)\n    return []\n"  # 冒号漏了
FUTURE_CODE = '''\
def on_bar(ctx):
    return [Signal(Side.BUY, reason="越界"), ctx.history[ctx.index + 1].close]
'''


def create(**overrides: Any) -> dict[str, Any]:
    body = {"name": "我的策略", "code": GOOD_CODE, **overrides}
    response = client.post("/api/v1/strategies", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ── 鉴权门 ──────────────────────────────────────────────────────────────────


def test_strategy_endpoints_require_login(jwt_secret: Any) -> None:
    uid = "00000000-0000-0000-0000-000000000000"
    assert client.get("/api/v1/strategies").status_code == 401
    assert client.post("/api/v1/strategies", json={"name": "x", "code": GOOD_CODE}).status_code == 401
    assert client.get(f"/api/v1/strategies/{uid}").status_code == 401
    assert client.put(f"/api/v1/strategies/{uid}", json={"name": "y"}).status_code == 401
    assert client.delete(f"/api/v1/strategies/{uid}").status_code == 401
    assert client.post("/api/v1/strategies/check", json={"code": GOOD_CODE}).status_code == 401
    assert client.get("/api/v1/strategies/templates").status_code == 401


# ── 建 ──────────────────────────────────────────────────────────────────────


def test_create_returns_row_with_hash_and_findings(signed_in: FakeDatabase) -> None:
    body = create(params={"fast": 7})
    assert body["name"] == "我的策略"
    assert body["code"] == GOOD_CODE
    assert body["params"] == {"fast": 7}
    assert len(body["code_sha256"]) == 64
    assert body["findings"] == []  # 干净代码零命中
    assert body["created_at"] and body["updated_at"]


def test_create_accepts_draft_with_syntax_error(signed_in: FakeDatabase) -> None:
    """**草稿可存**：语法错也 201，findings 给出 R0——闸门在回测提交前，不在保存时。"""
    body = create(code=SYNTAX_ERROR_CODE)
    rules = [item["rule"] for item in body["findings"]]
    assert "R0" in rules
    assert all(item["severity"] == "error" for item in body["findings"] if item["rule"] == "R0")


def test_create_reports_future_function_without_blocking(signed_in: FakeDatabase) -> None:
    body = create(code=FUTURE_CODE)
    assert {item["rule"] for item in body["findings"]} == {"R3"}  # 只报未来索引，不拦保存


def test_create_name_is_cleaned_and_bounded(signed_in: FakeDatabase) -> None:
    assert create(name="  带空白  ")["name"] == "带空白"

    for bad in ("   ", "x" * 61, "带\x07控制符"):
        response = client.post("/api/v1/strategies", json={"name": bad, "code": GOOD_CODE})
        assert response.status_code == 422, (bad, response.text)


def test_create_rejects_empty_or_oversized_code(signed_in: FakeDatabase) -> None:
    assert client.post("/api/v1/strategies", json={"name": "空", "code": "   "}).status_code == 422
    huge = "x = 1\n" * 20000  # ≈120KB
    assert client.post("/api/v1/strategies", json={"name": "大", "code": huge}).status_code == 422


def test_duplicate_name_conflicts(signed_in: FakeDatabase) -> None:
    create(name="同名")
    response = client.post("/api/v1/strategies", json={"name": "同名", "code": GOOD_CODE})
    assert response.status_code == 409
    assert "同名" in response.json()["detail"]


def seed_foreign(db: FakeDatabase, user_id: int, strategy_id: str, name: str) -> None:
    """直接往替身里摆一条**别人**的策略（端点不该看见它）。"""
    now = datetime.now(UTC)
    db.strategies[(user_id, strategy_id)] = {
        "id": strategy_id,
        "name": name,
        "code": GOOD_CODE,
        "params": {},
        "created_at": now,
        "updated_at": now,
    }


def test_same_name_is_fine_for_another_user(signed_in: FakeDatabase) -> None:
    """唯一约束是 `UNIQUE(user_id, name)`，不是全局唯一。"""
    seed_foreign(signed_in, 999, "00000000-0000-0000-0000-0000000000cc", "同名")
    assert create(name="同名")["name"] == "同名"


# ── 读 / 列 ─────────────────────────────────────────────────────────────────


def test_list_returns_summaries_without_code(signed_in: FakeDatabase) -> None:
    create(name="第一条")
    create(name="第二条")
    rows = client.get("/api/v1/strategies").json()
    assert [row["name"] for row in rows][:2] == ["第二条", "第一条"]  # updated_at 倒序
    assert set(rows[0]) == {"id", "name", "created_at", "updated_at"}  # 列表不带 code


def test_list_only_returns_own(signed_in: FakeDatabase) -> None:
    create(name="我的")
    seed_foreign(signed_in, 999, "00000000-0000-0000-0000-0000000000dd", "别人的")
    assert [row["name"] for row in client.get("/api/v1/strategies").json()] == ["我的"]


def test_get_returns_code_and_current_hash(signed_in: FakeDatabase) -> None:
    created = create()
    body = client.get(f"/api/v1/strategies/{created['id']}").json()
    assert body["code"] == GOOD_CODE
    assert body["code_sha256"] == created["code_sha256"]
    assert "findings" not in body  # 读不检查，findings 只随写与 /check 给


def test_get_other_users_strategy_is_404(signed_in: FakeDatabase) -> None:
    seed_foreign(signed_in, 999, "00000000-0000-0000-0000-0000000000aa", "别人的")
    target = "/api/v1/strategies/00000000-0000-0000-0000-0000000000aa"
    assert client.get(target).status_code == 404
    assert client.put(target, json={"name": "抢过来"}).status_code == 404
    assert client.delete(target).status_code == 404


def test_get_rejects_non_uuid(signed_in: FakeDatabase) -> None:
    assert client.get("/api/v1/strategies/not-a-uuid").status_code == 422
    assert client.delete("/api/v1/strategies/not-a-uuid").status_code == 422


# ── 改 / 删 ─────────────────────────────────────────────────────────────────


def test_update_is_partial(signed_in: FakeDatabase) -> None:
    created = create(params={"fast": 5})

    renamed = client.put(f"/api/v1/strategies/{created['id']}", json={"name": "改名了"}).json()
    assert renamed["name"] == "改名了"
    assert renamed["code"] == GOOD_CODE  # 没给的字段不动
    assert renamed["params"] == {"fast": 5}

    recoded = client.put(
        f"/api/v1/strategies/{created['id']}", json={"code": SYNTAX_ERROR_CODE, "params": {"fast": 9}}
    ).json()
    assert recoded["code"] == SYNTAX_ERROR_CODE
    assert recoded["params"] == {"fast": 9}
    assert recoded["code_sha256"] != created["code_sha256"]
    assert recoded["findings"][0]["rule"] == "R0"


def test_update_conflict_and_missing(signed_in: FakeDatabase) -> None:
    first = create(name="甲")
    create(name="乙")
    response = client.put(f"/api/v1/strategies/{first['id']}", json={"name": "乙"})
    assert response.status_code == 409

    missing = "00000000-0000-0000-0000-0000000000bb"
    assert client.put(f"/api/v1/strategies/{missing}", json={"name": "无"}).status_code == 404
    assert client.delete(f"/api/v1/strategies/{missing}").status_code == 404


def test_delete_then_gone(signed_in: FakeDatabase) -> None:
    created = create()
    assert client.delete(f"/api/v1/strategies/{created['id']}").json() == {
        "id": created["id"],
        "deleted": True,
    }
    assert client.get(f"/api/v1/strategies/{created['id']}").status_code == 404
    assert client.delete(f"/api/v1/strategies/{created['id']}").status_code == 404


# ── 检查 ────────────────────────────────────────────────────────────────────


def test_check_returns_findings_and_meta(signed_in: FakeDatabase) -> None:
    body = client.post("/api/v1/strategies/check", json={"code": FUTURE_CODE}).json()
    assert [item["rule"] for item in body["findings"]] == ["R3"]
    assert body["findings"][0]["severity"] == "error"
    assert body["findings"][0]["line"] == 2
    assert body["meta"] == {"params": {}, "uses_events": False}


def test_check_meta_carries_param_schema(signed_in: FakeDatabase) -> None:
    """参数表单就靠这一份 schema 渲染——形状写死在这里，前端按它生成输入框。"""
    body = client.post("/api/v1/strategies/check", json={"code": GOOD_CODE}).json()
    assert body["meta"]["params"] == {
        "fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"}
    }


def test_check_meta_is_null_when_params_unparsable(signed_in: FakeDatabase) -> None:
    """`PARAMS` 坏掉时 `meta` 为 null，问题由 R4 findings 承载——检查器从不抛异常。"""
    code = "PARAMS = make_params()\n\n\ndef on_bar(ctx):\n    return []\n"
    body = client.post("/api/v1/strategies/check", json={"code": code}).json()
    assert body["meta"] is None
    assert "R4" in {item["rule"] for item in body["findings"]}


def test_check_reports_syntax_error_as_finding(signed_in: FakeDatabase) -> None:
    body = client.post("/api/v1/strategies/check", json={"code": SYNTAX_ERROR_CODE}).json()
    assert body["meta"] is None
    assert [item["rule"] for item in body["findings"]] == ["R0"]


# ── 模板 ────────────────────────────────────────────────────────────────────


def test_templates_are_served_with_source(signed_in: FakeDatabase) -> None:
    rows = client.get("/api/v1/strategies/templates").json()
    assert len(rows) == 5
    assert sum(1 for row in rows if row["builtin"]) == 2
    assert all(row["source"].strip() for row in rows)
    assert {row["key"] for row in rows} >= {"ma_cross", "event_driven"}


# ── 替身语义（守住「离线矩阵不是假的」）────────────────────────────────────


def test_fake_database_mirrors_unique_constraint(signed_in: FakeDatabase) -> None:
    """替身必须自己也会抛 `StrategyNameTaken`——否则上面那条 409 用例是假的。"""
    import asyncio

    asyncio.run(signed_in.create_strategy("a", TEST_USER["id"], "重名", GOOD_CODE, {}))
    try:
        asyncio.run(signed_in.create_strategy("b", TEST_USER["id"], "重名", GOOD_CODE, {}))
    except StrategyNameTaken:
        return
    raise AssertionError("替身没有照抄 UNIQUE(user_id, name)")
