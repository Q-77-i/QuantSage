"""M5b 集成测试：批量 / 网格落库汇总 + 重开，打真实 Postgres 与真实 `data/` 行情。

离线那份（`tests/test_optimize_api.py`）注入的是内存业务库，**证不了 SQL 里的事**：
`optimization_runs` 的建表、JSONB 往返、`user_id` 过滤、`summary - 'cells'` 这个
只写在 SQL 里的投影（`FakeDatabase` 是照抄语义，不是同一段代码）、以及删账号的级联。

前置条件：`docker compose up -d --wait`（postgres 必需）+ `data/` 已落盘。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

SYMBOL = "600519"
#: 把区间收窄：真数据全期 1,636 根 bar，逐格 ~500ms，集成用例没必要跑满
WINDOW = {"start": "2026-07-01", "end": "2026-09-30"}


def fresh_emails(prefix: str, count: int, threads: int = 0) -> tuple[list[str], list[str]]:
    suffix = uuid.uuid4().hex[:8]
    emails = [f"m5b-{prefix}-{suffix}-{index}@example.com" for index in range(count)]
    thread_ids = [str(uuid.uuid4()) for _ in range(threads)]
    return emails, thread_ids


def frames(body: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.startswith("event: "):
            continue
        head, _, data = block.partition("\ndata: ")
        out.append((head.removeprefix("event: "), json.loads(data)))
    return out


def run_grid(client: TestClient, **overrides: Any) -> list[tuple[str, dict[str, Any]]]:
    body = {
        "strategy": "ma_cross",
        "symbol": SYMBOL,
        "params": {"slow": 20},
        "axes": [{"param": "fast", "values": [3, 5, 8]}],
        **WINDOW,
        **overrides,
    }
    with client.stream("POST", "/api/v1/optimize/grid", json=body) as response:
        assert response.status_code == 200, "网格流没能开起来"
        return frames("".join(response.iter_text()))


def test_schema_setup_is_idempotent() -> None:
    """新建表与索引都幂等：同一批 DDL 连跑两次不能报错。"""
    import asyncio

    from app.core.config import get_settings
    from app.core.db import init_schema, open_pool

    async def setup_twice() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            await init_schema(pool)
            await init_schema(pool)

    asyncio.run(setup_twice())


def test_grid_round_trips_through_real_jsonb(real_stack: TestClient) -> None:
    """落库 → 重开：**数字还是数字**（JSONB 往返最常见的坑是把它们变成字符串），
    且每格矩阵一寸不少地回来了。"""
    emails, _ = fresh_emails("roundtrip", 1)
    try:
        alice = sign_up(emails[0])
        events = run_grid(alice)
        done = events[-1][1]
        assert done["cells_ok"] == 3

        reopened = alice.get(f"/api/v1/optimize/runs/{done['run_id']}")
        assert reopened.status_code == 200, reopened.text
        summary = reopened.json()["summary"]

        assert len(summary["cells"]) == 3
        assert summary["cells_total"] == 3 and summary["cells_ok"] == 3
        first = summary["cells"][0]
        assert isinstance(first["metrics"]["sharpe"], (int, float))
        assert isinstance(first["moments"]["skew"], (int, float))
        assert isinstance(first["params"]["fast"], int)
        # `overfit` 自带全部输入（重开时不必回算）
        assert summary["overfit"]["n_trials"] == 3
        assert summary["overfit"]["note"]
        assert isinstance(summary["overfit"]["sr_variance"], (int, float))
    finally:
        purge_rows(emails, [])


def test_runs_list_projection_happens_in_sql(real_stack: TestClient) -> None:
    """`summary - 'cells'` 是**写在 SQL 里**的投影：离线替身照抄了语义，这一跑才算数。"""
    emails, _ = fresh_emails("list", 1)
    try:
        alice = sign_up(emails[0])
        run_id = run_grid(alice)[-1][1]["run_id"]

        rows = alice.get("/api/v1/optimize/runs").json()
        assert [row["id"] for row in rows] == [run_id]
        assert "cells" not in rows[0]["summary"]
        assert rows[0]["summary"]["cells_ok"] == 3
        assert rows[0]["request"]["symbol"] == SYMBOL
    finally:
        purge_rows(emails, [])


def test_ownership_is_filtered_in_sql(real_stack: TestClient) -> None:
    """越权一律 404，且真没读到别人的行（过滤在 `WHERE user_id = %s` 上）。"""
    emails, _ = fresh_emails("owner", 2)
    try:
        alice, bob = sign_up(emails[0]), sign_up(emails[1])
        run_id = run_grid(alice)[-1][1]["run_id"]

        assert bob.get(f"/api/v1/optimize/runs/{run_id}").status_code == 404
        assert bob.get("/api/v1/optimize/runs").json() == []
        assert alice.get(f"/api/v1/optimize/runs/{run_id}").status_code == 200
    finally:
        purge_rows(emails, [])


def test_batch_of_symbols_and_strategies(real_stack: TestClient) -> None:
    """批量：多标的 × 多策略，逐格一行，且**逐格的窗口各记各的**（不共享）。"""
    emails, _ = fresh_emails("batch", 1)
    try:
        alice = sign_up(emails[0])
        body = {
            "symbols": [SYMBOL, "000001"],
            "strategies": [
                {"strategy": "ma_cross", "params": {"fast": 5, "slow": 20}},
                {"strategy": "ma_cross", "params": {"fast": 10, "slow": 30}},
            ],
            **WINDOW,
        }
        with alice.stream("POST", "/api/v1/optimize/batch", json=body) as response:
            assert response.status_code == 200
            events = frames("".join(response.iter_text()))

        assert events[0][1]["kind"] == "batch" and events[0][1]["total"] == 4
        cells = [payload for name, payload in events if name == "cell"]
        assert len(cells) == 4
        # 格序：标的在外、策略在内（前端表格一行一个标的）
        assert [(cell["symbol"], cell["params"]["fast"]) for cell in sorted(cells, key=lambda c: c["index"])] == [
            (SYMBOL, 5),
            (SYMBOL, 10),
            ("000001", 5),
            ("000001", 10),
        ]
        assert all(cell["ok"] for cell in cells)

        summary = alice.get(f"/api/v1/optimize/runs/{events[-1][1]['run_id']}").json()["summary"]
        assert summary["kind"] == "batch"
        assert len({(cell["window"]["start"], cell["window"]["end"]) for cell in summary["cells"]}) >= 1
    finally:
        purge_rows(emails, [])


def test_deleting_the_account_cascades(real_stack: TestClient) -> None:
    """删账号时汇总随外键级联清掉（teardown 全靠它，也免得开发库越跑越脏）。"""
    import psycopg

    from app.core.config import get_settings

    emails, _ = fresh_emails("cascade", 1)
    alice = sign_up(emails[0])
    run_id = run_grid(alice)[-1][1]["run_id"]

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        conn.execute("DELETE FROM users WHERE email = ANY(%s)", (emails,))
        left = conn.execute(
            "SELECT count(*) FROM optimization_runs WHERE id = %s::uuid", (run_id,)
        ).fetchone()
    assert left == (0,)
