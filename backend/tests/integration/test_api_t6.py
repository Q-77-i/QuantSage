"""T6 端点验收：打真实 `data/` 落盘数据。

先跑 `uv run python scripts/download_bars.py` 与 `scripts/download_events.py`。
默认不收集；用 `uv run pytest -m integration` 触发。

离线用例（`tests/test_api_t6.py`）用合成 Parquet 验契约与错误分支，这里验的是
**真实数据的形状**：真实 bars 有 27 列、真实 events 的来源三元组是否真的有值、
真实样本量是否触发 `meta.warnings`。合成夹具永远测不出这些。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.data import duckdb_client as dc
from app.main import app
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

client = TestClient(app)

DATA_DIR = get_settings().data_dir
SYMBOLS = ("600519", "300750", "600036")


@pytest.fixture
def member_client(real_stack: TestClient) -> Iterator[TestClient]:
    """已登录的客户端：`market` / `events` 是公开的，**回测自 M1c 起要登录**（跑完落库）。

    账号在 teardown 里清——`users` 级联带走这个账号的 `backtest_runs`，不留垃圾。
    """
    email = f"m1c-t6-{uuid.uuid4().hex[:8]}@example.com"
    signed = sign_up(email)
    try:
        yield signed
    finally:
        purge_rows([email], [])


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_bars_endpoint_matches_parquet(symbol: str) -> None:
    response = client.get(f"/api/v1/market/{symbol}/bars")
    assert response.status_code == 200
    body = response.json()

    rows = dc.bars(symbol, data_dir=DATA_DIR)
    assert body["count"] == len(rows)
    assert body["bars"][0]["time"] == rows[0]["trade_date"].isoformat()
    assert body["bars"][-1]["time"] == rows[-1]["trade_date"].isoformat()


def test_events_endpoint_keeps_source_annotations() -> None:
    """来源标注是 PRD §5 的硬性要求：真实数据上必须条条有值，不能是空列。"""
    body = client.get("/api/v1/events", params={"symbol": "300750"}).json()
    assert body["count"] > 0

    for event in body["events"]:
        assert event["source"], f"{event['event_id']} 缺 source"
        assert event["original_source"], f"{event['event_id']} 缺 original_source"
        assert event["content_hash"], f"{event['event_id']} 缺 content_hash"
        # available_at 是本项目的护城河字段，绝不能为空
        assert event["available_at"] is not None


def test_event_driven_backtest_quantifies_pit_gap(member_client: TestClient) -> None:
    """T6 回测页的核心展示项：两模式对比必须真的有数，不是空壳。"""
    response = member_client.post(
        "/api/v1/backtest",
        json={"strategy": "event_driven", "symbol": "300750", "pit_mode": "both"},
    )
    assert response.status_code == 200
    body = response.json()["report"]  # M1c 起外套信封 {run_id, report}

    comparison = body["pit_comparison"]
    assert comparison is not None
    delta = comparison["delta"]
    assert delta["final_equity_pct"] is not None  # 分母恒为正，不该为 None
    assert comparison["entry_dates"]["pit"] or comparison["entry_dates"]["non_pit"]
    # 事件窗口只有约 3 个月，必须触发样本量提示——前端要把它显示出来
    assert body["meta"]["warnings"]
    assert "交易明细" not in body["meta"]["warnings"][0]  # 提示的是样本量，不是别的


def test_ma_cross_backtest_covers_full_history(member_client: TestClient) -> None:
    response = member_client.post(
        "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": "600519"}
    )
    assert response.status_code == 200
    body = response.json()["report"]

    assert body["meta"]["bars"] == len(dc.bars("600519", data_dir=DATA_DIR))
    assert body["pit_comparison"] is None
    # 净值曲线与基准逐点对齐，前端两条线才能同轴画
    assert all(point["benchmark"] is not None for point in body["equity_curve"])
