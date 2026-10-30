"""M8b 集成（真实 Postgres + 真实 checkpointer）：离线替身证明不了的那几件事。

只有真库能验的：
  * **三个幂等 ALTER**（`kind` / `subject` / `account_id DROP NOT NULL`）连跑两次 lifespan 不报错；
  * `research_runs` 的读写往返与归属过滤（SQL 写在库里，内存替身证明不了）；
  * **HITL 的持久性**：图实例 A 停在 `interrupt()`，**换一个全新的图实例**（同一个真 checkpointer）
    用 `Command(resume=…)` 接着跑完——这正是「进程重启后还能批」的最小复现。

不调真 LLM：`build_context` 注入假模型（与离线用例同一个缝）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from app.agent.deep.budget import Budget
from app.agent.deep.graph import build_deep_graph
from app.agent.deep.roles.base import RoleContext
from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.main import app

# `real_stack` 不在此列：它是 conftest 的 **fixture**，pytest 自动发现；显式导入反而
# 会在测试函数把它写成参数时构成「重定义」（F811）——`sign_up` 是普通函数，必须导入。
from tests.integration.conftest import purge_rows, sign_up
from tests.test_deep_graph import DeepFake

AS_OF = "2026-09-30"

pytestmark = pytest.mark.integration


def _account() -> tuple[Any, str]:
    """注册一个账号（**必须在 lifespan 之后调**：库里没起来时注册端点直接 503）。"""
    email = f"m8-{uuid.uuid4().hex[:8]}@example.com"
    return sign_up(email), email


def test_schema_is_idempotent_and_runs_round_trip(real_stack: TestClient) -> None:
    """建表与三个 ALTER 连跑两次 lifespan；运行记录读写往返 + 归属过滤。"""
    account, email = _account()
    db = app.state.db
    user_id = _user_id(account)
    run_id = str(uuid.uuid4())

    asyncio.run(db.create_research_run(
        run_id=run_id, user_id=user_id, question="分析一下 600519",
        subject={"symbols": ["600519"], "as_of": f"{AS_OF}T15:00:00+08:00"},
    ))
    assert asyncio.run(db.update_research_run(run_id, status="done", events=[{"phase": "end", "node": "market"}],
                                              budget={"llm_calls": 3}, report_id=None))

    row = asyncio.run(db.get_research_run(user_id, run_id))
    assert row is not None and row["status"] == "done" and row["events"][0]["node"] == "market"
    assert row["budget"]["llm_calls"] == 3

    # 归属过滤：别人的 id 查不到（与端点 404 同口径）
    assert asyncio.run(db.get_research_run(user_id + 1, run_id)) is None
    # 真库读回的 id 是 `uuid.UUID`（M7 记过的坑）——端点层已 `str()` 归一，断言跟着归一
    assert [str(item["id"]) for item in asyncio.run(db.list_research_runs(user_id))] == [run_id]


def test_deep_report_lands_in_the_m7_container(real_stack: TestClient) -> None:
    """深研报落**同一张** research_reports：account_id 为空、kind 是 deep_research_report。"""
    account, email = _account()
    db = app.state.db
    user_id = _user_id(account)
    report_id = str(uuid.uuid4())
    report = {"kind": "deep_research_report", "blocks": [{"id": "overview", "kind": "fact"}]}

    asyncio.run(db.create_research_report(
        report_id=report_id, user_id=user_id, account_id=None,
        snapshot={"bars": {"digest": "d" * 64}}, snapshot_hash="h" * 64,
        report=report, report_hash="r" * 64,
        kind="deep_research_report",
        subject={"question": "分析一下 600519", "run_id": "run-1"},
    ))

    row = asyncio.run(db.get_research_report(user_id, report_id))
    assert row is not None and row["account_id"] is None
    assert row["report"]["kind"] == "deep_research_report"
    assert row["subject"]["run_id"] == "run-1"


def test_hitl_survives_a_brand_new_graph_instance(real_stack: TestClient) -> None:
    """图实例 A 停在 `interrupt()`，**新实例 B** 接着跑完——进程重启后仍能批的最小复现。"""
    run_id = f"m8-hitl-{uuid.uuid4().hex[:8]}"
    brief_payload = {
        "question": "分析一下 600519 值不值得买",
        "symbols": ["600519"],
        "as_of": f"{AS_OF}T15:00:00+08:00",
        "scope": "",
        "notes": [],
    }

    def context() -> RoleContext:
        from app.agent.deep.types import Brief

        return RoleContext(
            brief=Brief(
                question=brief_payload["question"],
                symbols=("600519",),
                as_of=datetime.fromisoformat(brief_payload["as_of"]),
            ),
            budget=Budget(),
            chat=DeepFake(synthesis_script=[{"conclusion": "建议买入并持有。", "citations": []}]),
            thinking=False,
            timeout=5.0,
        )

    async def phase_one() -> dict[str, Any]:
        async with open_checkpointer(get_settings().postgres_dsn) as saver:
            graph = build_deep_graph(ctx=context(), checkpointer=saver)
            config = {"configurable": {"thread_id": run_id}}
            await graph.ainvoke({"run_id": run_id, "brief": brief_payload}, config)
            # 真 checkpointer 上必须走 async 接口（同步 get_state 会抛）
            snapshot = await graph.aget_state(config)
            return {
                "next": list(snapshot.next),
                "interrupts": [i.value for t in snapshot.tasks for i in (t.interrupts or [])],
            }

    async def phase_two() -> dict[str, Any]:
        async with open_checkpointer(get_settings().postgres_dsn) as saver:  # 新连接、新图实例
            graph = build_deep_graph(ctx=context(), checkpointer=saver)
            config = {"configurable": {"thread_id": run_id}}
            return await graph.ainvoke(Command(resume={"choice": "continue"}), config)

    first = asyncio.run(phase_one())
    assert first["interrupts"] and first["interrupts"][0]["kind"] == "wording"

    second = asyncio.run(phase_two())
    assert second["gate"]["approval_choice"] == "continue"
    assert second["draft"]["conclusion"].startswith("建议买入")

    _cleanup_thread(run_id)


def _cleanup_thread(thread_id: str) -> None:
    """checkpointer 的表是 langgraph 自己的，按 thread_id 清（同 conftest 口径）。"""
    import psycopg

    from tests.integration.conftest import CHECKPOINT_TABLES

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        for table in CHECKPOINT_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))


def _user_id(client: Any) -> int:
    """从会话 cookie 反查 user_id：走 `/auth/me`（返回的是 `UserOut` 本体，没有外层信封）。"""
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    return int(response.json()["id"])


def test_sweep_interrupted_runs_on_real_database(real_stack: TestClient) -> None:
    """真库：启动扫尾只把 running 标 failed，awaiting_approval 原样保留。"""
    account, email = _account()
    db = app.state.db
    user_id = _user_id(account)
    running_id = str(uuid.uuid4())
    awaiting_id = str(uuid.uuid4())
    try:
        asyncio.run(
            db.create_research_run(
                run_id=running_id, user_id=user_id, question="running", subject={}
            )
        )
        asyncio.run(
            db.create_research_run(
                run_id=awaiting_id,
                user_id=user_id,
                question="awaiting",
                subject={},
                status="awaiting_approval",
            )
        )

        assert asyncio.run(db.sweep_interrupted_research_runs()) >= 1
        assert asyncio.run(db.get_research_run(user_id, running_id))["status"] == "failed"
        assert (
            asyncio.run(db.get_research_run(user_id, awaiting_id))["status"]
            == "awaiting_approval"
        )
    finally:
        purge_rows([email], [])
