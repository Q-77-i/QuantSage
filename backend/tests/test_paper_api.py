"""M6 模拟盘端点：创建期校验 / 闸门的 HTTP 语义 / 推进 / 一键跑到结束 / 归属。

离线用例的替身是 `FakeDatabase`（同 M1c/M4c 口径：**唯一约束与状态守卫必须照抄真库**，
否则「只生效一次」这类断言测的是替身自己的宽容）。行情走真实 Parquet（沿用 P1 口径）。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data import calendar as cal
from app.data import duckdb_client
from app.main import app
from tests.conftest import make_backtest_dir, ts, write_bars_parquet

client = TestClient(app)

START = date(2026, 8, 3)
SYMBOL = "600519"


def sessions(count: int) -> list[date]:
    days = cal.sessions(START, START + timedelta(days=count * 3 + 10))
    assert len(days) >= count
    return days[:count]


def build_dir(root: Path, days: list[date]) -> Path:
    """六根 bar + 一条事件。

    价格是**单调上行**的：ma_cross(1,2) 在第二个交易日给出金叉，event_driven 则在**第一天**
    就能出单（事件在首根 bar 的收盘就可见）——两种策略各有用途：

    * `event_driven`：建会话当天就有待审批决策，且**带来源三元组**（决策单的完整形状）；
    * `ma_cross`：第一根 bar 给不出信号（没有历史），正好验证「第一天没有决策是正常的」。
    """
    prices = [100.0, 100.0, 130.0, 132.0, 134.0, 136.0][: len(days)]
    rows = [
        {"trade_date": day, "open": price, "close": price, "high": price + 1, "low": price - 1}
        for day, price in zip(days, prices, strict=True)
    ]
    events = [
        {
            "event_id": "news:1",
            "title": "公司发布重大合同公告",
            "event_time": ts("2026-08-03 09:00:00"),
            "available_at": ts("2026-08-03 09:10:00"),
            "direction_norm": "bullish",
            "score": 88.0,
            "source": "xiaoshi-archive",
            "original_source": "华尔街见闻",
            "content_hash": "abc123",
        }
    ]
    root = make_backtest_dir(root, rows, events=events, symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, rows, adjust="raw")
    return root


def body(**over: object) -> dict:
    payload: dict = {
        "name": "模拟会话",
        "initial_cash": 200_000.0,
        "symbols": [SYMBOL],
        "strategy": "event_driven",
        "params": {"min_score": 50.0, "hold_days": 3},
        "start": START.isoformat(),
        "costs": {"fees": True, "slippage": True, "slippage_bps": 5.0},
    }
    payload.update(over)
    return payload


@pytest.fixture
def paper_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed_in: object) -> list[date]:
    days = sessions(6)
    root = build_dir(tmp_path, days)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: root)
    return days


def create(**over: object):
    return client.post("/api/v1/paper/accounts", json=body(**over))


# ── 未登录与归属 ────────────────────────────────────────────


def test_requires_sign_in() -> None:
    """没有 `signed_in` 夹具（未登录姿态）时一律 401。"""
    assert client.get("/api/v1/paper/accounts").status_code == 401
    assert client.post("/api/v1/paper/accounts", json=body()).status_code == 401
    assert client.post(f"/api/v1/paper/accounts/{uuid.uuid4()}/step").status_code == 401
    assert client.post(f"/api/v1/paper/decisions/{uuid.uuid4()}/approve").status_code == 401


def test_unknown_account_is_404(paper_env: list[date]) -> None:
    assert client.get(f"/api/v1/paper/accounts/{uuid.uuid4()}").status_code == 404
    assert client.post(f"/api/v1/paper/accounts/{uuid.uuid4()}/step").status_code == 404


def test_non_uuid_id_is_422(paper_env: list[date]) -> None:
    assert client.get("/api/v1/paper/accounts/not-a-uuid").status_code == 422


# ── 创建 ────────────────────────────────────────────────────


def test_create_then_detail_shape(paper_env: list[date]) -> None:
    days = paper_env
    response = create()
    assert response.status_code == 201, response.text
    detail = response.json()

    assert detail["account"]["name"] == "模拟会话"
    assert detail["account"]["status"] == "active"
    assert detail["account"]["as_of"] == days[0].isoformat()
    assert detail["progress"]["days_total"] == len(days)
    assert detail["progress"]["days_done"] == 1
    assert detail["valuation"]["equity"] == pytest.approx(200_000.0)
    assert detail["positions"] == []
    # 建会话就跑了第一天：**决策单要当场看得见**（否则用户不知道该批什么）
    assert len(detail["decisions"]) == 1
    decision = detail["decisions"][0]
    assert decision["side"] == "buy"
    assert decision["status"] == "pending"
    assert decision["status_label"] == "待审批"
    assert decision["est_qty"] > 0
    assert decision["reason"].startswith("event_driven:news:1")
    # **来源三元组**：买入决策能回链到驱动它的那条事件（卖出没有来源，如实留空）
    assert decision["sources"]["source"] == "xiaoshi-archive"
    assert decision["sources"]["original_source"] == "华尔街见闻"
    assert decision["sources"]["content_hash"] == "abc123"
    assert decision["sources"]["available_at"].startswith("2026-08-03 09:10")
    assert detail["account"]["rules"][SYMBOL]["limit_check"] == "on"


def test_first_day_has_no_decision_for_a_strategy_that_needs_history(
    paper_env: list[date],
) -> None:
    """第一天没有决策是**结构性**的：均线要 `slow+1` 根 bar 才算得出来。如实空着，不编一张单。"""
    detail = create(strategy="ma_cross", params={"fast": 1, "slow": 2}).json()
    assert detail["decisions"] == []
    assert detail["pending"] == []

    account_id = detail["account"]["id"]
    assert client.post(f"/api/v1/paper/accounts/{account_id}/step").json()["this_step"][
        "generated"
    ] == []  # 第二个交易日：能算但还没金叉
    third = client.post(f"/api/v1/paper/accounts/{account_id}/step").json()
    (decision,) = third["this_step"]["generated"]  # 第三个交易日才出金叉
    assert decision["reason"].startswith("ma_cross:golden")


def test_end_must_not_exceed_local_data(paper_env: list[date]) -> None:
    days = paper_env
    response = create(end=(days[-1] + timedelta(days=30)).isoformat())
    assert response.status_code == 422
    assert "晚于本地行情" in response.json()["detail"]


def test_too_short_window_is_422(paper_env: list[date]) -> None:
    response = create(start=paper_env[1].isoformat(), end=paper_env[1].isoformat())
    assert response.status_code == 422


def test_quota_must_afford_one_lot(paper_env: list[date]) -> None:
    """配额买不起一手 ⇒ 422 并指出是哪只（否则那只标的一辈子不出手，用户看不出为什么）。"""
    response = create(initial_cash=10_000.0)
    assert response.status_code == 422
    assert SYMBOL in response.json()["detail"]
    assert "买不起一手" in response.json()["detail"]


def test_unknown_symbol_is_422(paper_env: list[date]) -> None:
    response = create(symbols=[SYMBOL, "300750"])
    assert response.status_code == 422
    assert "300750" in response.json()["detail"]


def test_unknown_strategy_is_422(paper_env: list[date]) -> None:
    assert create(strategy="nope").status_code == 422


def test_user_strategy_requires_saved_id(paper_env: list[date]) -> None:
    assert create(strategy="user").status_code == 422


def test_too_many_symbols_is_422(paper_env: list[date]) -> None:
    assert create(symbols=[f"{i:06d}" for i in range(21)]).status_code == 422


def test_bad_symbol_shape_is_422(paper_env: list[date]) -> None:
    assert create(symbols=["60051"]).status_code == 422
    assert create(symbols=["60051A"]).status_code == 422


# ── 闸门 ────────────────────────────────────────────────────


def _first_decision(detail: dict) -> dict:
    assert detail["pending"], "这一步应当有待审批决策"
    return detail["pending"][0]


def test_approve_then_fill_on_next_session(paper_env: list[date]) -> None:
    days = paper_env
    detail = create().json()
    decision = _first_decision(detail)

    approved = client.post(f"/api/v1/paper/decisions/{decision['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    stepped = client.post(f"/api/v1/paper/accounts/{detail['account']['id']}/step")
    assert stepped.status_code == 200, stepped.text
    after = stepped.json()
    assert after["account"]["as_of"] == days[1].isoformat()
    assert after["this_step"]["trade_date"] == days[1].isoformat()

    (filled,) = after["this_step"]["filled"]
    assert filled["id"] == decision["id"]
    assert filled["status"] == "filled"
    fill = filled["fill"]
    # 成交价 = 次日开盘 × (1 + 滑点)，费用与手工口径一致
    assert fill["ref_price"] == pytest.approx(100.0)
    assert fill["price"] == pytest.approx(100.0 * (1 + 5 / 10_000))
    assert fill["commission"] >= 5.0  # 最低佣金 5 元
    assert fill["stamp_tax"] == 0.0  # 买入不收印花税
    assert after["positions"][0]["shares"] == fill["qty"]


def test_approve_twice_is_409(paper_env: list[date]) -> None:
    detail = create().json()
    decision = _first_decision(detail)
    assert client.post(f"/api/v1/paper/decisions/{decision['id']}/approve").status_code == 200
    again = client.post(f"/api/v1/paper/decisions/{decision['id']}/approve")
    assert again.status_code == 409
    assert "只有待审批" in again.json()["detail"]


def test_unapproved_decision_expires_and_never_fills(paper_env: list[date]) -> None:
    """**未审批不成交**（PRD 硬要求）：直接推进过去，它变成过期，且账上一分钱没动。"""
    detail = create().json()
    decision = _first_decision(detail)

    stepped = client.post(f"/api/v1/paper/accounts/{detail['account']['id']}/step").json()
    assert [d["id"] for d in stepped["this_step"]["expired"]] == [decision["id"]]
    assert stepped["this_step"]["filled"] == []
    assert stepped["account"]["cash"] == pytest.approx(200_000.0)
    assert stepped["positions"] == []

    late = client.post(f"/api/v1/paper/decisions/{decision['id']}/approve")
    assert late.status_code == 409  # 过期之后不能再批


def test_reject_is_409_after_reject(paper_env: list[date]) -> None:
    detail = create().json()
    decision = _first_decision(detail)
    assert client.post(f"/api/v1/paper/decisions/{decision['id']}/reject").status_code == 200
    assert client.post(f"/api/v1/paper/decisions/{decision['id']}/reject").status_code == 409


def test_unknown_decision_is_404(paper_env: list[date]) -> None:
    assert client.post(f"/api/v1/paper/decisions/{uuid.uuid4()}/approve").status_code == 404


# ── 跑到结束 ────────────────────────────────────────────────


def test_run_to_end_finishes_and_then_conflicts(paper_env: list[date]) -> None:
    days = paper_env
    detail = create().json()
    account_id = detail["account"]["id"]

    finished = client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})
    assert finished.status_code == 200, finished.text
    final = finished.json()
    assert final["account"]["status"] == "finished"
    assert final["account"]["as_of"] == days[-1].isoformat()
    assert len(final["equity_curve"]) == len(days)
    assert final["pending"] == []

    assert (
        client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"}).status_code
        == 409
    )
    assert client.post(f"/api/v1/paper/accounts/{account_id}/step").status_code == 409


def test_run_with_none_rejects_everything(paper_env: list[date]) -> None:
    detail = create().json()
    final = client.post(
        f"/api/v1/paper/accounts/{detail['account']['id']}/run", json={"approve": "none"}
    ).json()
    assert all(d["fill"] is None for d in final["decisions"])
    assert final["positions"] == []
    assert final["account"]["cash"] == pytest.approx(200_000.0)


def test_run_bad_approve_value_is_422(paper_env: list[date]) -> None:
    detail = create().json()
    response = client.post(
        f"/api/v1/paper/accounts/{detail['account']['id']}/run", json={"approve": "maybe"}
    )
    assert response.status_code == 422


# ── 列表 ────────────────────────────────────────────────────


def test_list_shows_own_accounts_only(paper_env: list[date], signed_in: object) -> None:
    create(name="甲")
    assert [row["name"] for row in client.get("/api/v1/paper/accounts").json()] == ["甲"]

    signed_in.paper_accounts[next(iter(signed_in.paper_accounts))]["user_id"] = 999  # 换个主人
    assert client.get("/api/v1/paper/accounts").json() == []
