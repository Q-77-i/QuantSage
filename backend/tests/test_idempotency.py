"""E4b / L7：幂等键。

四条判据（SPEC §5.5）：

  * **同 key 同 body** → 只建**一行**任务，回同一个 `task_id`，`status` 取行里的真值；
  * **同 key 异 body** → **409**（宁可吵一声，不静默跑错的那份）；
  * **不同用户同 key** → 各跑各的（键的作用域是「某人」，不是全局）；
  * **没给 key** → 照旧每次新建（幂等是可选的）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.idempotency import HEADER_ALIAS, KEY_MAX
from app.data import duckdb_client
from app.main import app
from tests.conftest import FakeDatabase, make_backtest_dir, trading_days

client = TestClient(app)

MA = {
    "symbol": "600519",
    "strategy": "ma_cross",
    "start": "2026-07-01",
    "end": "2026-09-30",
}


@pytest.fixture(autouse=True)
def synthetic_market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把数据目录指到**合成的**行情上。

    本文件验的是**幂等键**（SPEC §5.5），不是数据——但提交要过 `resolve_window` 那道闸，
    而它读的是真实 `data/`。不铺这一层的话，在没有 `data/` 的环境（CI runner）里
    每个提交都回 503，**整文件 11 条一起红**。合成行只需覆盖 `MA` 的区间。
    """
    days = [day for day in trading_days(date(2026, 7, 1), 100) if day <= date(2026, 9, 30)]
    bars = [
        {
            "trade_date": day,
            "open": 10.0 + index * 0.1,
            "close": 10.0 + index * 0.1,
            "high": 10.5 + index * 0.1,
            "low": 9.5 + index * 0.1,
            "change_pct": 1.0,
        }
        for index, day in enumerate(days)
    ]
    root = make_backtest_dir(tmp_path, bars, symbol="600519")
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: root)
USER_STRATEGY_BODY = {**MA, "strategy": "user", "strategy_id": "00000000-0000-0000-0000-0000000000aa"}


def submit(body: dict[str, Any], key: str | None = None) -> Any:
    headers = {HEADER_ALIAS: key} if key is not None else {}
    return client.post("/api/v1/backtest", json=body, headers=headers)


def test_same_key_same_body_replays_one_task(signed_in: FakeDatabase) -> None:
    first = submit(MA, key="act-1")
    second = submit(MA, key="act-1")

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(signed_in.tasks) == 1  # 只建了一行


def test_replay_reports_the_real_status(signed_in: FakeDatabase) -> None:
    """重放一个已经跑完的任务却回 `queued` 就是在撒谎——status 一律取行里的真值。"""
    body = submit(MA, key="act-2").json()
    signed_in.tasks[body["task_id"]]["status"] = "done"

    assert submit(MA, key="act-2").json()["status"] == "done"


def test_same_key_different_body_is_409(signed_in: FakeDatabase) -> None:
    submit(MA, key="act-3")

    response = submit({**MA, "symbol": "300750"}, key="act-3")

    assert response.status_code == 409
    assert "Idempotency-Key" in response.json()["detail"]


def test_different_keys_create_different_tasks(signed_in: FakeDatabase) -> None:
    assert submit(MA, key="a").json()["task_id"] != submit(MA, key="b").json()["task_id"]
    assert len(signed_in.tasks) == 2


def test_without_key_every_submit_creates_a_task(signed_in: FakeDatabase) -> None:
    """不给键的调用方（脚本、curl）要的就是「每次都跑」。"""
    submit(MA)
    submit(MA)

    assert len(signed_in.tasks) == 2


def test_key_is_scoped_per_user(signed_in: FakeDatabase) -> None:
    from app.core.auth import require_user
    from tests.conftest import TEST_USER

    submit(MA, key="act-4")
    app.dependency_overrides[require_user] = lambda: {**TEST_USER, "id": 2}
    response = submit(MA, key="act-4")

    assert response.status_code == 202
    assert len(signed_in.tasks) == 2  # 另一个用户有自己的那一行


def test_overlong_key_is_422(signed_in: FakeDatabase) -> None:
    assert submit(MA, key="x" * (KEY_MAX + 1)).status_code == 422


def test_blank_key_means_no_key(signed_in: FakeDatabase) -> None:
    submit(MA, key="   ")
    submit(MA, key="   ")

    assert len(signed_in.tasks) == 2


def test_replay_short_circuits_before_the_gates(signed_in: FakeDatabase) -> None:
    """短路在闸门**之前**：第二次提交连窗口解析都不该走（那一步要真读一次行情）。

    判据：把行情目录换成「没有这个标的」的状态——闸门若照跑会 404，短路则照回同一个任务。
    """
    first = submit(MA, key="act-5").json()

    from app.data import duckdb_client

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("重复提交不该再走一遍闸门")

    original = duckdb_client.bars
    duckdb_client.bars = boom
    try:
        assert submit(MA, key="act-5").json()["task_id"] == first["task_id"]
    finally:
        duckdb_client.bars = original


def test_user_strategy_branch_is_also_idempotent(signed_in: FakeDatabase) -> None:
    """两条分支（内置 / 用户策略）都要接上——用户策略那条还多一道静态检查。"""
    from app.strategy.templates import template_source

    strategy_id = USER_STRATEGY_BODY["strategy_id"]
    signed_in.strategies[(1, strategy_id)] = {  # 假库的键是 (user_id, strategy_id)
        "id": strategy_id,
        "user_id": 1,
        "name": "我的双均线",
        "code": template_source("ma_cross"),
        "created_at": None,
        "updated_at": None,
    }

    first = submit(USER_STRATEGY_BODY, key="act-6")
    second = submit(USER_STRATEGY_BODY, key="act-6")

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(signed_in.tasks) == 1


def test_grid_submit_is_idempotent(signed_in: FakeDatabase) -> None:
    body = {"symbol": "600519", "strategy": "ma_cross", "axes": [{"param": "fast", "values": [5, 8]}]}
    headers = {HEADER_ALIAS: "act-7"}

    first = client.post("/api/v1/optimize/grid", json=body, headers=headers)
    second = client.post("/api/v1/optimize/grid", json=body, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(signed_in.tasks) == 1


def test_grid_same_key_different_body_is_409(signed_in: FakeDatabase) -> None:
    headers = {HEADER_ALIAS: "act-8"}
    client.post(
        "/api/v1/optimize/grid",
        json={"symbol": "600519", "strategy": "ma_cross", "axes": [{"param": "fast", "values": [5, 8]}]},
        headers=headers,
    )

    response = client.post(
        "/api/v1/optimize/grid",
        json={"symbol": "600519", "strategy": "ma_cross", "axes": [{"param": "fast", "values": [5, 9]}]},
        headers=headers,
    )

    assert response.status_code == 409
