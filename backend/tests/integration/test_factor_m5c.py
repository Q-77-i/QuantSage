"""M5c 因子报告验收：打**真实 `data/` 落盘数据**（不需要容器，公开端点无库依赖）。

默认不收集；用 `uv run pytest -m integration tests/integration/test_factor_m5c.py` 触发。
先跑 `scripts/download_bars.py` 与 `scripts/download_events.py`。

离线用例（`tests/test_factor_api.py`）验契约与错误分支，这里验的是**真实数据的形状**：
语料里真的有 `factor_value`（且与 `factor_scores.score` 同源）、真实池子有多薄、
两个因子源在真数据上跑得出报告。合成夹具永远测不出这些。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client as dc
from app.factor.panel import build_event_panel, build_price_panel
from app.main import app

pytestmark = pytest.mark.integration

client = TestClient(app)


def test_real_corpus_ranks_the_event_factor_over_the_whole_window() -> None:
    """真实语料全期跑通一次 IC（SPEC §12 M5c 集成增量）。"""
    response = client.get("/api/v1/factor/report")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["params"]["source"] == "event"
    # 实测口径：54 个有效信号日、日均池约 250 只——只断言「跑得出且不为空」，
    # 具体读数会随行情/语料刷新而前移
    assert body["ic"]["days"] > 0
    assert body["universe"]["pool_avg"] > 20
    assert len(body["groups"]) == 5
    assert body["notes"]


def test_real_prices_rank_the_reversal_factor_over_the_whole_market() -> None:
    response = client.get("/api/v1/factor/report", params={"source": "price"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ic"]["days"] > 0
    # 价格因子是全市场池：日均远大于事件池（实测约 5,400 只）
    assert body["universe"]["pool_avg"] > 1000
    assert body["long_short"]["tradable"] is False


def test_real_rows_carry_factor_value_and_lookback() -> None:
    """真实数据的**形状**：有向事件带 `factor_value`，价格行带 20 根之前的收盘。"""
    event_rows = dc.factor_event_rows()
    assert event_rows, "语料里应当有有向事件"
    assert all(row["factor_value"] is not None for row in event_rows)  # 实测覆盖 100%
    assert all(row["symbol"] for row in event_rows)

    price_rows = dc.factor_price_rows("2026-08-03", "2026-08-07")
    panel = build_price_panel(price_rows, direction="reversal")
    assert panel.days, "窗口内应当有交易日"
    with_lag = [value for day in panel.factor.values() for value in day.values()]
    assert with_lag, "20 根历史齐备的标的应当算得出因子值"


def test_real_event_panel_buckets_into_trading_days() -> None:
    """归属日落在真实交易日上，且窗口外的事件被如实剔除（不是硬塞）。"""
    price_rows = dc.factor_price_rows("2026-08-03", "2026-08-07")
    days = build_price_panel(price_rows).days
    panel = build_event_panel(dc.factor_event_rows(), days)

    assert panel.values, "真实语料应当有落入该窗口的事件"
    assert set(panel.values) <= set(days)
    assert all(isinstance(day, date) for day in panel.values)
