"""M5b 端点单测：SSE 帧序列、请求级 422 矩阵、落库与重开、鉴权。

三条要点：

* **请求级错误必须在开流之前**——流一旦开始状态码就改不了了（与 chat 同一条约束）。
  故「轴数超限 / 某格参数非法 / 格数超限」全部断言 **HTTP 422 的 JSON 响应**，
  而不是「流里出现一个 error 帧」；
* **落库发生在 `done`**——流没走完就不该留记录（刷新即重跑）；
* 用户策略走**真实沙箱子进程**（与 M4c 同一条路径），静态检查有 error 的源码
  必须在 spawn **之前**被 422 拦下并带回 findings。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client
from app.main import app
from tests.conftest import make_backtest_dir, trading_days, write_bars_parquet

client = TestClient(app)

SYMBOL = "600519"
START = date(2026, 7, 1)
BAR_COUNT = 60

GOOD_USER_SOURCE = """PARAMS = {"n": {"type": "int", "default": 3, "min": 1, "max": 20}}
USES_EVENTS = False


def on_bar(ctx, p):
    n = p["n"]
    if ctx.index < n:
        return []
    window = [bar.close for bar in ctx.history[-n:]]
    if ctx.position.is_flat and window[-1] > window[0]:
        return [Signal(side=Side.BUY, reason="短窗上行")]
    if not ctx.position.is_flat and window[-1] < window[0]:
        return [Signal(side=Side.SELL, reason="短窗下行")]
    return []
"""

BAD_USER_SOURCE = "import os\n\nPARAMS = {}\n\n\ndef on_bar(ctx):\n    return []\n"


def wave_bars() -> list[dict[str, object]]:
    """三段行情：上行 → 回落 → 再上行。双均线能真的交叉出信号。"""
    rows: list[dict[str, object]] = []
    price = 100.0
    for index, day in enumerate(trading_days(START, BAR_COUNT)):
        drift = 0.8 if index < BAR_COUNT // 3 else (-0.6 if index < 2 * BAR_COUNT // 3 else 0.9)
        price = round(price + drift, 2)
        rows.append(
            {"trade_date": day, "open": price, "close": price, "high": price + 0.5, "low": price - 0.5}
        )
    return rows


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bars = wave_bars()
    make_backtest_dir(tmp_path, bars, symbol=SYMBOL)
    write_bars_parquet(tmp_path / "bars", SYMBOL, bars, adjust="raw")
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    return tmp_path


def frames(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 正文切成 `(事件名, 载荷)` 序列。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.startswith("event: "):
            continue
        head, _, data = block.partition("\ndata: ")
        out.append((head.removeprefix("event: "), json.loads(data)))
    return out


def stream(client_: TestClient, path: str, body: dict[str, Any]) -> tuple[int, list]:
    with client_.stream("POST", path, json=body) as response:
        if response.status_code != 200:
            return response.status_code, []
        return 200, frames("".join(response.iter_text()))


def grid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "strategy": "ma_cross",
        "symbol": SYMBOL,
        "params": {"slow": 20},
        "axes": [{"param": "fast", "values": [3, 5, 8]}],
        "pit_mode": "pit",
    }
    body.update(overrides)
    return body


# ── 鉴权 ────────────────────────────────────────────────────


def test_grid_requires_login() -> None:
    assert client.post("/api/v1/optimize/grid", json=grid_body()).status_code == 401


def test_runs_requires_login() -> None:
    assert client.get("/api/v1/optimize/runs").status_code == 401


# ── 请求级 422（全部必须在开流之前）──────────────────────────


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"axes": []}, "至少要有 1 条参数轴"),
        (
            {"axes": [{"param": "a", "values": [1, 2]}, {"param": "b", "values": [1, 2]},
                      {"param": "c", "values": [1, 2]}]},
            "最多 2 条",
        ),
        ({"axes": [{"param": "nope", "values": [1, 2]}]}, "不接受参数"),
        ({"axes": [{"param": "slow", "values": [10, 20]}]}, "同时出现在基座参数与参数轴"),
        ({"axes": [{"param": "fast", "values": [3, 3]}]}, "有重复"),
        ({"axes": [{"param": "fast", "values": [3]}]}, "至少要有 2 个取值"),
    ],
)
def test_grid_rejects_bad_axes(
    signed_in: Any, data_dir: Path, override: dict, fragment: str
) -> None:
    response = client.post("/api/v1/optimize/grid", json=grid_body(**override))
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


def test_grid_rejects_the_whole_order_when_one_cell_is_illegal(
    signed_in: Any, data_dir: Path
) -> None:
    """SPEC §6 M5b 写死的那条：`fast=[5,20] × slow=[10,30]` 里的 `fast=20/slow=10` 非法 ⇒ 整单 422。

    这是**故意的**：静默跳过会在热力图上留一个按不出原因的空洞。
    """
    response = client.post(
        "/api/v1/optimize/grid",
        json=grid_body(params={}, axes=[{"param": "fast", "values": [5, 20]},
                                       {"param": "slow", "values": [10, 30]}]),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "第 3 格" in detail and "fast=20" in detail


def test_grid_rejects_a_grid_over_the_cell_cap(signed_in: Any, data_dir: Path) -> None:
    response = client.post(
        "/api/v1/optimize/grid",
        json=grid_body(
            params={},
            axes=[{"param": "fast", "values": list(range(1, 12))},
                  {"param": "slow", "values": list(range(20, 31))}],
        ),
    )
    assert response.status_code == 422
    assert "超过单次上限" in response.json()["detail"]


def test_grid_rejects_pit_mode_both(signed_in: Any, data_dir: Path) -> None:
    """网格不收 `both`：那等于把每格工作量翻倍，而 PIT 对比在单格重跑时照样能看。"""
    response = client.post("/api/v1/optimize/grid", json=grid_body(pit_mode="both"))
    assert response.status_code == 422


def test_grid_rejects_user_without_strategy_id(signed_in: Any) -> None:
    response = client.post("/api/v1/optimize/grid", json=grid_body(strategy="user"))
    assert response.status_code == 422
    assert "strategy_id" in response.text


def test_grid_rejects_an_unknown_symbol_before_streaming(signed_in: Any, data_dir: Path) -> None:
    """网格只有一个标的：没数据就是 404（与 `POST /backtest` 同响应），不开一条只会吐失败格的流。"""
    response = client.post("/api/v1/optimize/grid", json=grid_body(symbol="999999"))
    assert response.status_code == 404


def test_batch_rejects_a_duplicated_symbol(signed_in: Any, data_dir: Path) -> None:
    response = client.post(
        "/api/v1/optimize/batch",
        json={"symbols": [SYMBOL, SYMBOL], "strategies": [{"strategy": "ma_cross"}]},
    )
    assert response.status_code == 422
    assert "重复" in response.text


def test_batch_rejects_too_many_cells(signed_in: Any, data_dir: Path) -> None:
    response = client.post(
        "/api/v1/optimize/batch",
        json={
            "symbols": [f"{i:06d}" for i in range(20)],
            "strategies": [{"strategy": "ma_cross"}] * 6,
        },
    )
    assert response.status_code == 422
    assert "超过单次上限" in response.text


def test_batch_rejects_bad_params(signed_in: Any, data_dir: Path) -> None:
    """批量里每条策略只校验一次（不是每个标的各来一遍）。"""
    response = client.post(
        "/api/v1/optimize/batch",
        json={
            "symbols": [SYMBOL],
            "strategies": [{"strategy": "ma_cross", "params": {"fast": 20, "slow": 5}}],
        },
    )
    assert response.status_code == 422
    assert "不合法" in response.json()["detail"]


# ── SSE 端到端 ──────────────────────────────────────────────


def test_grid_streams_start_cells_then_done(signed_in: Any, data_dir: Path) -> None:
    status, events = stream(client, "/api/v1/optimize/grid", grid_body())
    assert status == 200

    names = [name for name, _ in events]
    assert names[0] == "start"
    assert names[-1] == "done"
    assert names.count("cell") == 3

    start = events[0][1]
    assert start["kind"] == "grid" and start["total"] == 3
    assert start["window"]["start"] and start["window"]["end"]
    assert start["axes"] == [{"param": "fast", "values": [3, 5, 8]}]

    cells = [payload for name, payload in events if name == "cell"]
    assert sorted(cell["index"] for cell in cells) == [0, 1, 2]
    for cell in cells:
        assert cell["ok"] is True
        assert cell["window"]["bars"] > 0
        assert "sharpe" in cell["metrics"]
        assert set(cell["moments"]) == {"skew", "kurt", "n"}

    done = events[-1][1]
    assert len(done["run_id"]) == 36
    assert done["cells_ok"] == 3 and done["cells_total"] == 3
    assert done["overfit"]["n_trials"] == 3
    assert done["best_index"] is not None
    # 原因码与展示文案**同行**出到帧里（M5b-2 起）：前端直接显示，不自己拼一句。
    # 出数时两者都必须是 None，不留下半句解释
    assert done["overfit"]["reason"] is None and done["overfit"]["reason_text"] is None


def test_grid_saves_the_summary_when_the_stream_completes(
    signed_in: Any, data_dir: Path
) -> None:
    _, events = stream(client, "/api/v1/optimize/grid", grid_body())
    run_id = events[-1][1]["run_id"]

    row = signed_in.optimizations[run_id]
    assert row["user_id"] == 1
    assert row["request"]["kind"] == "grid"
    assert row["request"]["start"] and row["request"]["adjust"] == "qfq"
    assert len(row["summary"]["cells"]) == 3
    assert row["summary"]["overfit"]["note"]


def test_runs_list_omits_the_cell_matrix(signed_in: Any, data_dir: Path) -> None:
    """列表**不带每格矩阵**（同真库的 `summary - 'cells'`）——列表页不需要它，拖回来只是浪费。"""
    stream(client, "/api/v1/optimize/grid", grid_body())
    rows = client.get("/api/v1/optimize/runs").json()

    assert len(rows) == 1
    assert "cells" not in rows[0]["summary"]
    assert rows[0]["summary"]["cells_total"] == 3
    assert rows[0]["summary"]["kind"] == "grid"


def test_run_reopen_returns_the_full_summary(signed_in: Any, data_dir: Path) -> None:
    _, events = stream(client, "/api/v1/optimize/grid", grid_body())
    run_id = events[-1][1]["run_id"]

    body = client.get(f"/api/v1/optimize/runs/{run_id}").json()
    assert len(body["summary"]["cells"]) == 3
    assert body["request"]["symbol"] == SYMBOL


def test_another_users_run_is_404(signed_in: Any, data_dir: Path) -> None:
    _, events = stream(client, "/api/v1/optimize/grid", grid_body())
    run_id = events[-1][1]["run_id"]

    signed_in.optimizations[run_id]["user_id"] = 999  # 换主人
    assert client.get(f"/api/v1/optimize/runs/{run_id}").status_code == 404


def test_bad_run_id_is_422_not_500(signed_in: Any) -> None:
    """非 UUID 直接 422：`%s::uuid` 会在驱动层抛 DataError，那是拿 500 报客户端错误。"""
    assert client.get("/api/v1/optimize/runs/not-a-uuid").status_code == 422


def test_batch_streams_one_cell_per_symbol_and_strategy(signed_in: Any, data_dir: Path) -> None:
    _, events = stream(
        client,
        "/api/v1/optimize/batch",
        {
            "symbols": [SYMBOL],
            "strategies": [
                {"strategy": "ma_cross", "params": {"fast": 5, "slow": 20}},
                {"strategy": "ma_cross", "params": {"fast": 8, "slow": 20}},
            ],
        },
    )
    names = [name for name, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    assert names.count("cell") == 2
    assert events[0][1]["kind"] == "batch"
    assert events[0][1]["total"] == 2


# ── 用户策略（真实沙箱）─────────────────────────────────────


async def make_user_strategy(db: Any, source: str, name: str = "短窗动量") -> str:
    row = await db.create_strategy("11111111-1111-1111-1111-111111111111", 1, name, source, {})
    return row["id"]


def test_user_strategy_missing_is_404(signed_in: Any, data_dir: Path) -> None:
    response = client.post(
        "/api/v1/optimize/grid",
        json=grid_body(strategy="user", strategy_id="11111111-1111-1111-1111-111111111111"),
    )
    assert response.status_code == 404


def test_user_strategy_with_a_static_error_is_422_with_findings(
    signed_in: Any, data_dir: Path
) -> None:
    """闸门在 spawn **之前**：整份网格一次都不该开跑。"""
    strategy_id = asyncio_run(make_user_strategy(signed_in, BAD_USER_SOURCE))
    response = client.post(
        "/api/v1/optimize/grid",
        json=grid_body(
            strategy="user",
            strategy_id=strategy_id,
            params={},
            axes=[{"param": "n", "values": [2, 3]}],
        ),
    )
    assert response.status_code == 422
    assert any(item["severity"] == "error" for item in response.json()["findings"])


def test_user_strategy_grid_runs_and_carries_its_name(signed_in: Any, data_dir: Path) -> None:
    strategy_id = asyncio_run(make_user_strategy(signed_in, GOOD_USER_SOURCE))
    _, events = stream(
        client,
        "/api/v1/optimize/grid",
        grid_body(
            strategy="user",
            strategy_id=strategy_id,
            params={},
            axes=[{"param": "n", "values": [2, 3, 4]}],
        ),
    )
    start = events[0][1]
    assert start["strategy_name"] == "短窗动量"
    assert start["total"] == 3

    cells = [payload for name, payload in events if name == "cell"]
    assert all(cell["ok"] for cell in cells)
    # 缺省值由用户策略的 `PARAMS` schema 填满——热力图的参数标注要看得到它
    assert all("n" in cell["params"] for cell in cells)
    assert events[-1][1]["cells_ok"] == 3


def test_user_strategy_with_a_wrong_axis_param_is_422(signed_in: Any, data_dir: Path) -> None:
    """轴参数不在用户策略的 `PARAMS` 里：整单 422，并列出可用参数。"""
    strategy_id = asyncio_run(make_user_strategy(signed_in, GOOD_USER_SOURCE))
    response = client.post(
        "/api/v1/optimize/grid",
        json=grid_body(
            strategy="user", strategy_id=strategy_id, params={},
            axes=[{"param": "nope", "values": [1, 2]}],
        ),
    )
    assert response.status_code == 422
    assert "不接受参数" in response.json()["detail"]


def asyncio_run(coro: Any) -> Any:
    """离线用例里驱动一小段协程（`FakeDatabase` 的方法是 async，但没有真实 IO）。"""
    import asyncio

    return asyncio.run(coro)
