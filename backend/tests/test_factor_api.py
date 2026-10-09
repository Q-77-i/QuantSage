"""M5c 因子报告端点：缺省窗口、参数校验、两个因子源、费用开关、降级。

端点**公开**（同 `market/*`，不触用户数据），同步返回（实测端到端 <0.2s，不走 SSE）。
请求级的判死全部发生在取数之前：结构性错误 422、显式越界 400——**不静默**。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client as dc
from app.main import app
from tests.conftest import ts, write_bars_parquet, write_events_parquet

client = TestClient(app)

#: 30 个连续自然日（测试不需要真交易日历：面板的「交易日」就是行情里出现过的日子）
BARS_DAYS = [date(2026, 7, 1) + timedelta(days=i) for i in range(30)]
#: 报告窗口 = 最后 5 天；前 25 天是价格因子的 lookback 原料
WINDOW = BARS_DAYS[-8:]
SYMBOLS = [f"6005{i:02d}" for i in range(25)]


def _price(j: int, i: int) -> float:
    """价格同时决定动量与次日收益：两者的截面排序都随 j 单调升（便于断言 IC 的符号）。"""
    return (10 + j) * (1 + i * 0.001 * (j + 1))


def make_factor_dir(tmp_path: Path) -> Path:
    for j, symbol in enumerate(SYMBOLS):
        write_bars_parquet(
            tmp_path / "bars",
            symbol,
            [
                {"trade_date": day, "open": _price(j, i), "close": _price(j, i)}
                for i, day in enumerate(BARS_DAYS)
            ],
        )
    events = [
        {
            "event_id": f"news:{j}",
            "symbols": [symbol],
            "direction_norm": "bullish",
            "factor_value": 0.1 + j / 100,
            "event_time": ts(f"{day.isoformat()} 10:00:00"),
        }
        for j, symbol in enumerate(SYMBOLS[:10])
        for day in WINDOW[:2]
    ]
    write_events_parquet(tmp_path / "events", "panel", events)
    return tmp_path


@pytest.fixture
def factor_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = make_factor_dir(tmp_path)
    monkeypatch.setattr(dc, "resolve_data_dir", lambda _=None: root)
    return root


def report(**params: object) -> dict:
    response = client.get("/api/v1/factor/report", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ── 形状与缺省 ──────────────────────────────────────────────


def test_event_report_has_the_contracted_shape(factor_dir: Path) -> None:
    body = report()

    assert set(body) == {"params", "window", "universe", "ic", "groups", "long_short", "notes"}
    assert len(body["groups"]) == 5
    assert body["long_short"]["tradable"] is False
    assert body["notes"]
    # 缺省窗口 = **语料起点 ∩ 行情末端**（都由数据决定，不写死「今天」）
    assert body["window"]["start"] == WINDOW[0].isoformat()
    assert body["window"]["end"] == BARS_DAYS[-1].isoformat()
    assert body["window"]["bars_end"] == BARS_DAYS[-1].isoformat()
    assert body["window"]["corpus"]["rows"] == 20
    assert body["params"]["source"] == "event"
    assert body["params"]["factor"] == "factor_value"
    assert body["params"]["quantiles"] == 5


def test_event_pool_is_counted_and_days_are_reported(factor_dir: Path) -> None:
    body = report(start=WINDOW[0].isoformat(), end=WINDOW[-1].isoformat())

    # 事件铺在前两天，各 10 只；10 < MIN_POOL(20) ⇒ 两天都被门槛跳过，如实计数
    assert body["window"]["skipped_thin_pool"] == 2
    assert body["ic"]["days"] == 0
    assert body["ic"]["mean"] is None
    assert body["universe"]["pool_avg"] == 0


def test_price_source_ranks_the_whole_market_and_direction_flips_the_sign(
    factor_dir: Path,
) -> None:
    window = {"source": "price", "start": WINDOW[0].isoformat(), "end": WINDOW[-1].isoformat()}

    momentum = report(**window, direction="momentum")
    reversal = report(**window, direction="reversal")

    assert momentum["params"]["factor"] == "price_momentum_20"
    assert momentum["window"]["signal_days"] == len(WINDOW) - 2  # 末两天没有 t+2
    assert momentum["ic"]["mean"] == pytest.approx(-reversal["ic"]["mean"])
    assert momentum["ic"]["days"] == reversal["ic"]["days"] > 0


def test_costs_off_returns_null_net(factor_dir: Path) -> None:
    body = report(source="price", costs="false")

    assert body["groups"][0]["net"] is None
    assert body["long_short"]["net"] is None
    assert body["groups"][0]["gross"] is not None


def test_turnover_is_reported_per_group(factor_dir: Path) -> None:
    body = report(source="price")
    assert 0.0 <= body["groups"][0]["turnover_avg"] <= 1.0
    assert body["groups"][0]["turnover_avg"] > 0  # 日频再平衡必然有换手


# ── 参数校验（不静默） ──────────────────────────────────────


def test_start_after_end_is_422(factor_dir: Path) -> None:
    response = client.get(
        "/api/v1/factor/report",
        params={"start": WINDOW[-1].isoformat(), "end": WINDOW[0].isoformat()},
    )
    assert response.status_code == 422
    assert "起始" in response.json()["detail"]


def test_window_longer_than_the_cap_is_422(factor_dir: Path) -> None:
    response = client.get(
        "/api/v1/factor/report",
        params={"start": "2020-01-01", "end": WINDOW[-1].isoformat()},
    )
    assert response.status_code == 422
    assert "窗口" in response.json()["detail"]


def test_end_beyond_the_bars_is_400(factor_dir: Path) -> None:
    response = client.get(
        "/api/v1/factor/report",
        params={"end": (BARS_DAYS[-1] + timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 400
    assert "行情" in response.json()["detail"]


def test_event_start_before_the_corpus_is_400(factor_dir: Path) -> None:
    response = client.get(
        "/api/v1/factor/report",
        params={"start": (BARS_DAYS[0] - timedelta(days=10)).isoformat()},
    )
    assert response.status_code == 400
    assert "语料" in response.json()["detail"]


def test_unknown_source_is_422(factor_dir: Path) -> None:
    assert client.get("/api/v1/factor/report", params={"source": "astrology"}).status_code == 422


def test_data_not_ready_is_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dc, "resolve_data_dir", lambda _=None: tmp_path / "empty")
    assert client.get("/api/v1/factor/report").status_code == 503


def test_events_before_an_explicit_window_do_not_pile_into_the_first_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**回归**：窗口之前可得的事件不得进窗口（这正是「含补窗的归属日历」存在的理由）。

    `bucket_day` 对早于全部日子的事件按引擎语义兜底到首日；若归属日历只给窗口内的日子，
    窗口第一天就会把历史事件整堆吸进池子。默认窗口起点 = 语料起点，把这个 bug 藏住了——
    故用例刻意用**显式窗口**：事件全铺在窗口之前的一天，窗口内应当**一天信号都没有**。
    """
    root = tmp_path
    for j, symbol in enumerate(SYMBOLS):
        write_bars_parquet(
            root / "bars",
            symbol,
            [
                {"trade_date": day, "open": _price(j, i), "close": _price(j, i)}
                for i, day in enumerate(BARS_DAYS)
            ],
        )
    write_events_parquet(
        root / "events",
        "panel",
        [
            {
                "event_id": f"news:{j}",
                "symbols": [symbol],
                "direction_norm": "bullish",
                "factor_value": 0.5,
                "event_time": ts(f"{BARS_DAYS[5].isoformat()} 10:00:00"),
            }
            for j, symbol in enumerate(SYMBOLS)  # 全 25 只：够 MIN_POOL，池子真能成形
        ],
    )
    monkeypatch.setattr(dc, "resolve_data_dir", lambda _=None: root)

    body = report(start=BARS_DAYS[10].isoformat(), end=BARS_DAYS[-1].isoformat())

    assert body["window"]["signal_days"] == 0
    assert body["universe"]["pool_avg"] == 0
    assert body["universe"]["symbols_seen"] == 0


def test_unknown_direction_is_422(factor_dir: Path) -> None:
    response = client.get(
        "/api/v1/factor/report", params={"source": "price", "direction": "sideways"}
    )
    assert response.status_code == 422


def test_unknown_adjust_is_422(factor_dir: Path) -> None:
    assert client.get("/api/v1/factor/report", params={"adjust": "hfq"}).status_code == 422
