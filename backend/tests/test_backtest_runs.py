"""离线单测：回测落库与「我的回测」（M1c）。

落库这件事在离线侧只能验到「端点把什么交给业务库」——真写进 JSONB 再由 SQL 抽回来，
是 `tests/integration/test_watchlist_m1c.py` 的事（`Jsonb` 适配、`->` 抽子集的类型）。

这里守三件事：
  * 信封形状与「落库在成功之后」（异常路径不留半条记录）；
  * 落库的 `request` 是**解析后的 config**，且**必须 JSON 可序列化**——`resolve_window`
    返回的是 `date`，漏掉 `mode="json"` 会在真库写 JSONB 时炸，这里用 `json.dumps` 提前拦；
  * 归属：列表只回本人的，按 id 取回越权一律 404。
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
from tests.conftest import make_backtest_dir, trading_days
from tests.fakes import FakeDatabase

client = TestClient(app)

SYMBOL = "600519"
START = date(2026, 7, 1)
BAR_COUNT = 60


def sample_bars() -> list[dict[str, Any]]:
    return [
        {"trade_date": day, "open": 100.0 + i, "close": 100.5 + i, "high": 101.0 + i, "low": 99.0 + i}
        for i, day in enumerate(trading_days(START, BAR_COUNT))
    ]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_backtest_dir(tmp_path, sample_bars(), [], symbol=SYMBOL)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    return tmp_path


def run_backtest(**overrides: Any) -> dict[str, Any]:
    response = client.post(
        "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL, **overrides}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── 鉴权门 ──────────────────────────────────────────────────────────────────


def test_backtest_endpoints_require_login(jwt_secret: Any) -> None:
    assert client.post("/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL}).status_code == 401
    assert client.get("/api/v1/backtest/runs").status_code == 401
    assert (
        client.get("/api/v1/backtest/runs/00000000-0000-0000-0000-000000000000").status_code
        == 401
    )


# ── 落库 ────────────────────────────────────────────────────────────────────


def test_run_is_persisted_and_listed(signed_in: FakeDatabase, data_dir: Path) -> None:
    body = run_backtest()
    run_id = body["run_id"]

    runs = client.get("/api/v1/backtest/runs").json()
    assert [row["id"] for row in runs] == [run_id]

    summary = runs[0]
    assert summary["symbol"] == SYMBOL
    assert summary["strategy"] == "ma_cross"
    assert summary["start"] == START.isoformat()
    assert summary["pit_mode"] == "pit"
    # metrics 整块给（逐键抽会把数字变成字符串，前端按比例格式化就会算错）
    assert summary["metrics"] == body["report"]["metrics"]
    assert isinstance(summary["metrics"]["total_return"], float)


def test_stored_request_is_resolved_and_json_safe(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """存的是解析后的 config：区间已由 `resolve_window` 填好，且**不带 date 对象**。"""
    run_id = run_backtest(pit_mode="both")["run_id"]
    stored = client.get(f"/api/v1/backtest/runs/{run_id}").json()["request"]

    json.dumps(stored)  # date 没转成字符串的话，这里就炸了（真库写 JSONB 时同样炸）
    assert stored["start"] == START.isoformat()
    assert stored["end"] == trading_days(START, BAR_COUNT)[-1].isoformat()
    assert stored["adjust"] == "qfq"  # 不在请求体里，落库时补的
    assert stored["pit_mode"] == "both"  # 存请求值，不存归一后的 Mode.PIT
    assert stored["costs"] == {"fees": True, "slippage": True, "slippage_bps": 5.0}


def test_get_run_returns_full_report(signed_in: FakeDatabase, data_dir: Path) -> None:
    """重开靠它：报告要能原样取回，否则「我的回测」只能看指标、画不出图。"""
    created = run_backtest(pit_mode="both")
    detail = client.get(f"/api/v1/backtest/runs/{created['run_id']}").json()

    assert detail["report"] == created["report"]
    assert detail["created_at"]


def test_failed_run_is_not_recorded(signed_in: FakeDatabase, data_dir: Path) -> None:
    assert client.post(
        "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": "000001"}
    ).status_code == 404

    assert client.get("/api/v1/backtest/runs").json() == []


def test_runs_are_listed_newest_first(signed_in: FakeDatabase, data_dir: Path) -> None:
    first = run_backtest()["run_id"]
    second = run_backtest(pit_mode="non_pit")["run_id"]

    assert [row["id"] for row in client.get("/api/v1/backtest/runs").json()] == [second, first]


def test_runs_limit_is_validated(signed_in: FakeDatabase) -> None:
    assert client.get("/api/v1/backtest/runs", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/backtest/runs", params={"limit": 101}).status_code == 422


# ── 归属 ────────────────────────────────────────────────────────────────────


def test_runs_are_scoped_to_the_owner(signed_in: FakeDatabase, data_dir: Path) -> None:
    run_id = run_backtest()["run_id"]
    # 把这条记录改成别人的（等价于「B 也有自己的记录」），当前用户是 1
    signed_in.runs[run_id]["user_id"] = 2

    assert client.get("/api/v1/backtest/runs").json() == []
    assert client.get(f"/api/v1/backtest/runs/{run_id}").status_code == 404


def test_unknown_run_id_is_404(signed_in: FakeDatabase) -> None:
    assert (
        client.get("/api/v1/backtest/runs/00000000-0000-0000-0000-000000000000").status_code
        == 404
    )


def test_malformed_run_id_is_422(signed_in: FakeDatabase) -> None:
    """非 UUID 要在端点里挡住：`%s::uuid` 会在驱动层抛 DataError，那是拿 500 报客户端错误。"""
    assert client.get("/api/v1/backtest/runs/not-a-uuid").status_code == 422
