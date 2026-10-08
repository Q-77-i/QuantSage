"""离线单测：T6 的三个新端点（backtest / market bars / events）。

不联网、不连库：行情与事件都用合成 Parquet（与 T2/T4/T5 同一套夹具），
`resolve_data_dir` 指向 tmp_path。

错误映射在这里逐条验（503 / 404 / 422 / 400）：它是前端唯一的错误来源，
映射错了前端就只能显示「未知错误」；而这类分支在手工点页面时几乎不会被走到。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data import duckdb_client
from app.main import app
from tests.conftest import make_backtest_dir, trading_days, ts, write_bars_parquet
from tests.fakes import FakeDatabase

client = TestClient(app)

SYMBOL = "600519"
START = date(2026, 7, 1)
BAR_COUNT = 60
#: 事件从第 21 根 bar 起：让「start 缺省取事件窗口」与「取行情起点」产生可证的差异
EVENT_START = START + timedelta(days=20)


def sample_bars() -> list[dict[str, object]]:
    return [
        {
            "trade_date": day,
            "open": 100.0 + index,
            "close": 100.5 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
        }
        for index, day in enumerate(trading_days(START, BAR_COUNT))
    ]


SAMPLE_EVENTS: list[dict[str, object]] = [
    {
        "event_id": "evt-1",
        "title": "中标大额订单",
        "event_time": ts("2026-07-21 09:30:00"),
        "score": 80.0,
        "direction_norm": "bullish",
        "source": "xiaoshi",
        "original_source": "上交所公告",
        "content_hash": "a1b2c3d4e5f60718",
    },
    {
        "event_id": "evt-2",
        "title": "控股股东减持",
        "event_time": ts("2026-08-05 16:00:00"),
        "score": 20.0,
        "direction_norm": "bearish",
        "source": "xiaoshi",
        "original_source": "证券时报",
        "content_hash": "ffeeddccbbaa0099",
    },
]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bars = sample_bars()
    make_backtest_dir(tmp_path, bars, SAMPLE_EVENTS, symbol=SYMBOL)
    write_bars_parquet(tmp_path / "bars", SYMBOL, bars, adjust="raw")
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    return tmp_path


# ── GET /api/v1/market/{symbol}/bars ────────────────────────────────────────


def test_bars_returns_chart_shaped_rows(data_dir: Path) -> None:
    response = client.get(f"/api/v1/market/{SYMBOL}/bars")
    assert response.status_code == 200
    body = response.json()

    assert body["symbol"] == SYMBOL
    assert body["adjust"] == "qfq"
    assert body["count"] == BAR_COUNT
    first = body["bars"][0]
    # 图表只吃这几列：多一列都是把内部字段漏给前端
    assert set(first) == {"time", "open", "high", "low", "close", "volume", "is_suspended"}
    assert first["time"] == START.isoformat()  # Lightweight Charts 的 business day 口径


def test_bars_window_is_inclusive(data_dir: Path) -> None:
    response = client.get(
        f"/api/v1/market/{SYMBOL}/bars",
        params={"start": "2026-07-10", "end": "2026-07-12"},
    )
    assert response.status_code == 200
    assert [bar["time"] for bar in response.json()["bars"]] == [
        "2026-07-10",
        "2026-07-11",
        "2026-07-12",
    ]


def test_bars_adjust_switches_series(data_dir: Path) -> None:
    response = client.get(f"/api/v1/market/{SYMBOL}/bars", params={"adjust": "raw"})
    assert response.status_code == 200
    assert response.json()["adjust"] == "raw"


def test_bars_unknown_symbol_is_404(data_dir: Path) -> None:
    response = client.get("/api/v1/market/000001/bars")
    assert response.status_code == 404
    assert "000001" in response.json()["detail"]


@pytest.mark.parametrize("symbol", ["60051", "6005199", "abcdef"])
def test_bars_malformed_symbol_is_422(data_dir: Path, symbol: str) -> None:
    assert client.get(f"/api/v1/market/{symbol}/bars").status_code == 422


def test_bars_data_not_ready_is_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据未落盘是「依赖没就绪」，不是请求写错——必须与 404 分开。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: empty)
    assert client.get(f"/api/v1/market/{SYMBOL}/bars").status_code == 503


# ── GET /api/v1/events ──────────────────────────────────────────────────────


def test_events_expose_source_trio_and_score(data_dir: Path) -> None:
    """PRD §5 硬性要求：来源标注必须可见，故三元组必须在响应里。"""
    response = client.get("/api/v1/events", params={"symbol": SYMBOL})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(SAMPLE_EVENTS)

    first = body["events"][0]
    assert first["source"] == "xiaoshi"
    assert first["original_source"] == "上交所公告"
    assert first["content_hash"] == "a1b2c3d4e5f60718"
    # factor_scores 是双重编码的 JSON 文本，出接口前必须解析成数值
    assert first["score"] == pytest.approx(80.0)
    # PIT 语义最直观的展示位：两个时间戳并列
    assert first["event_time"].startswith("2026-07-21")
    assert first["available_at"] is not None


def test_events_window_filters_on_event_time(data_dir: Path) -> None:
    response = client.get(
        "/api/v1/events", params={"symbol": SYMBOL, "start": "2026-08-01"}
    )
    assert [event["event_id"] for event in response.json()["events"]] == ["evt-2"]


def test_events_empty_window_is_not_404(data_dir: Path) -> None:
    """「这个窗口内没有事件」是有意义的事实，不是错误。"""
    response = client.get(
        "/api/v1/events", params={"symbol": SYMBOL, "end": "2026-07-10"}
    )
    assert response.status_code == 200
    assert response.json() == {"symbol": SYMBOL, "count": 0, "events": []}


# ── POST /api/v1/backtest ───────────────────────────────────────────────────
#
# M1c 起本端点纳入鉴权（跑完要落库，归属是必要信息），响应改信封 `{run_id, report}`。
# 用例一律挂 `signed_in`：这里验的是回测本身，不是鉴权——「未登录 401」与
# 「越权 404 / 落库」在 test_backtest_runs.py 与 tests/integration/ 里单独验。
#
# 需要留意的一条新契约：**鉴权先于 body 校验**，未登录 + 非法请求体返回 401 而非 422
# （唯一例外是 JSON 本身解析失败）。下面几条 422/404/503 之所以还能拿到原状态码，
# 正是因为挂了登录夹具。


def post_backtest(**overrides) -> object:
    payload = {"strategy": "ma_cross", "symbol": SYMBOL, **overrides}
    return client.post("/api/v1/backtest", json=payload)


def test_backtest_returns_envelope_around_spec_section5_shape(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """信封是新增的一层，报告结构本身没动（P1 SPEC §6）。"""
    response = post_backtest()
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"run_id", "report"}

    report = body["report"]
    assert set(report) == {"meta", "metrics", "equity_curve", "trades", "open_position", "pit_comparison"}
    assert report["meta"]["bars"] == BAR_COUNT
    assert len(report["equity_curve"]) == BAR_COUNT


def test_backtest_default_window_uses_event_start_for_event_driven(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """缺省区间与 CLI 同口径：事件策略从事件窗口起跑，不从行情起点空转。"""
    body = post_backtest(strategy="event_driven").json()["report"]
    assert body["meta"]["start"] == EVENT_START.isoformat()


def test_backtest_default_window_uses_bar_start_for_ma_cross(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    body = post_backtest(strategy="ma_cross").json()["report"]
    assert body["meta"]["start"] == START.isoformat()


def test_backtest_both_modes_yields_comparison_for_event_driven(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    body = post_backtest(strategy="event_driven", pit_mode="both").json()["report"]
    comparison = body["pit_comparison"]
    assert comparison is not None
    assert set(comparison) == {"pit_metrics", "non_pit_metrics", "delta", "entry_dates"}


def test_backtest_both_modes_stays_null_for_ma_cross(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """ma_cross 不消费事件语料，两模式必然同结果——不做无意义的二次回测。"""
    body = post_backtest(strategy="ma_cross", pit_mode="both").json()["report"]
    assert body["pit_comparison"] is None


def test_backtest_pit_mode_selects_cutoff_field(signed_in: FakeDatabase, data_dir: Path) -> None:
    assert post_backtest(pit_mode="pit").json()["report"]["meta"]["cutoff_field"] == "available_at"
    assert (
        post_backtest(pit_mode="non_pit").json()["report"]["meta"]["cutoff_field"]
        == "event_time"
    )


def test_backtest_cost_switches_reach_the_engine(signed_in: FakeDatabase, data_dir: Path) -> None:
    with_costs = post_backtest(costs={"fees": True, "slippage": True}).json()["report"]
    without = post_backtest(costs={"fees": False, "slippage": False}).json()["report"]
    assert with_costs["meta"]["costs"] != without["meta"]["costs"]


def test_backtest_unknown_strategy_is_422(signed_in: FakeDatabase, data_dir: Path) -> None:
    response = post_backtest(strategy="buy_and_hold")
    assert response.status_code == 422


def test_backtest_unknown_param_is_422(signed_in: FakeDatabase, data_dir: Path) -> None:
    """拼错的键必须报错：`from_params` 会静默忽略它，用户会以为参数生效了。"""
    response = post_backtest(strategy="event_driven", params={"minscore": 50})
    assert response.status_code == 422
    assert "minscore" in response.text


@pytest.mark.parametrize(
    ("strategy", "params", "keyword"),
    [
        ("ma_cross", {"fast": 20, "slow": 5}, "必须小于"),
        ("ma_cross", {"fast": 0}, "至少为 1"),
        ("ma_cross", {"slow": 1}, "至少为 2"),
        ("ma_cross", {"fast": 5.5}, "需要整数"),
        ("event_driven", {"min_score": 101}, "0~100"),
        ("event_driven", {"hold_days": 0}, "至少为 1"),
    ],
)
def test_backtest_param_values_are_422(
    signed_in: FakeDatabase, data_dir: Path, strategy: str, params: dict[str, float], keyword: str
) -> None:
    """M4 补齐的另一半：值域也挡（原先只挡未知键）——绕过前端直接打 API 同样拦得住。"""
    response = post_backtest(strategy=strategy, params=params)
    assert response.status_code == 422
    assert keyword in response.text


def test_backtest_frontend_boundary_values_still_pass(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """反面守卫：T6c 前端放行的边界值，后端必须同样放行（不能悄悄加限制）。"""
    assert post_backtest(strategy="ma_cross", params={"fast": 1, "slow": 2}).status_code == 200
    assert post_backtest(strategy="event_driven", params={"hold_days": 1}).status_code == 200


def test_backtest_reversed_window_is_422(signed_in: FakeDatabase, data_dir: Path) -> None:
    response = post_backtest(start="2026-08-01", end="2026-07-01")
    assert response.status_code == 422


def test_backtest_unknown_symbol_is_404(signed_in: FakeDatabase, data_dir: Path) -> None:
    response = post_backtest(symbol="000001")
    assert response.status_code == 404


def test_backtest_data_not_ready_is_503(
    signed_in: FakeDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: empty)
    assert post_backtest().status_code == 503


def test_backtest_failure_leaves_no_run_record(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """落库在 `build_report` 成功之后：异常路径不该留下半条记录。"""
    assert post_backtest(symbol="000001").status_code == 404
    assert signed_in.runs == {}


# ── CORS ────────────────────────────────────────────────────────────────────


def test_cors_allows_frontend_origin_and_exposes_thread_header() -> None:
    """`X-Thread-Id` 是断连时回传会话号的兜底通道，跨源下不暴露浏览器读不到。"""
    response = client.get("/health", headers={"Origin": "http://127.0.0.1:3001"})
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3001"
    # Expose-Headers 只挂在真实响应上，预检响应里没有（实测确认）
    assert "x-thread-id" in response.headers["access-control-expose-headers"].lower()

    preflight = client.options(
        "/api/v1/backtest",
        headers={
            "Origin": "http://127.0.0.1:3001",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:3001"
    assert "POST" in preflight.headers["access-control-allow-methods"]

    # 会话删除（T6b）走 DELETE，预检不放行的话浏览器直接拦掉，且报错很难懂
    delete_preflight = client.options(
        "/api/v1/chat/threads/00000000-0000-0000-0000-000000000000",
        headers={
            "Origin": "http://127.0.0.1:3001",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert "DELETE" in delete_preflight.headers["access-control-allow-methods"]

    # 自选股改分组 / 重命名分组走 PATCH（M1c），同一个坑：漏了它浏览器直接拦掉
    patch_preflight = client.options(
        "/api/v1/watchlist/600519",
        headers={
            "Origin": "http://127.0.0.1:3001",
            "Access-Control-Request-Method": "PATCH",
        },
    )
    assert "PATCH" in patch_preflight.headers["access-control-allow-methods"]

    # 策略更新走 PUT（M4c）——同一个坑第二次：这次连浏览器实测都拦下了才发现，
    # 后端用例（TestClient 不走 CORS）与 HTTP 矩阵（只发 POST）都看不见
    put_preflight = client.options(
        "/api/v1/strategies/00000000-0000-0000-0000-000000000000",
        headers={
            "Origin": "http://127.0.0.1:3001",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert "PUT" in put_preflight.headers["access-control-allow-methods"]


# ── M1：数据时点（页头「数据截至 X」的数据源）────────────────


def test_freshness_returns_latest_dates(data_dir: Path) -> None:
    """必须来自真实 Parquet（离线用例也写真实文件，不 mock 查询层）。

    事件夹具没给 `available_at`，落盘时按 `event_time` 兜底，所以两者末点同源。
    """
    response = client.get("/api/v1/market/freshness")

    assert response.status_code == 200
    body = response.json()
    assert body["latest_trade_date"] == str(trading_days(START, BAR_COUNT)[-1])
    assert body["latest_event_available_at"].startswith("2026-08-05")


def test_freshness_returns_503_without_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据没落盘时 503，前端据此不显示标签——不能编一个日期出来。"""
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path / "nope")

    assert client.get("/api/v1/market/freshness").status_code == 503
