"""M6 模拟盘端点：创建期校验 / 闸门的 HTTP 语义 / 推进 / 一键跑到结束 / 归属。

离线用例的替身是 `FakeDatabase`（同 M1c/M4c 口径：**唯一约束与状态守卫必须照抄真库**，
否则「只生效一次」这类断言测的是替身自己的宽容）。行情走真实 Parquet（沿用 P1 口径）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import paper as paper_api
from app.backtest.types import Side
from app.data import calendar as cal
from app.data import duckdb_client
from app.main import app
from app.paper import PaperError
from app.paper.store import decision_to_row
from app.paper.types import Decision, DecisionStatus, decision_id
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


# ── E5 风控 ─────────────────────────────────────────────────


RISK = {
    "max_gross_exposure": 100.0,
    "max_position": 30.0,
    "stop_loss": 10.0,
    "max_drawdown": 20.0,
}


def test_create_with_risk_exposes_state_and_audit(paper_env: list[date]) -> None:
    detail = create(risk=RISK).json()
    assert detail["account"]["config"]["risk"]["max_position"] == 30.0
    assert detail["risk_state"]["equity"] == pytest.approx(200_000.0)
    assert detail["risk_state"]["blocked_by_drawdown"] is False
    assert len(detail["decisions"]) == 1
    decision = detail["decisions"][0]
    assert decision["origin"] == "strategy"
    assert decision["est_qty"] == 500  # 30% × 20 万 / 100.05 ≈ 500 股
    caps = [event for event in detail["risk_events"] if event["verdict"] == "cap"]
    assert len(caps) == 1
    assert caps[0]["gate"] == "concentration"
    assert caps[0]["metrics"]["requested_qty"] == 1900
    assert caps[0]["metrics"]["final_qty"] == 500


def test_risk_off_detail_has_empty_state_and_events(paper_env: list[date]) -> None:
    detail = create().json()
    assert detail["account"]["config"]["risk"] is None
    assert detail["risk_state"] == {}
    assert detail["risk_events"] == []
    assert detail["decisions"][0]["origin"] == "strategy"


def test_invalid_risk_is_422(paper_env: list[date]) -> None:
    assert create(risk={**RISK, "max_position": 0}).status_code == 422
    missing = {key: value for key, value in RISK.items() if key != "stop_loss"}
    assert create(risk=missing).status_code == 422
    assert create(risk={**RISK, "max_drawdown": 101}).status_code == 422


def test_risk_budget_must_afford_one_lot(paper_env: list[date]) -> None:
    response = create(risk={**RISK, "max_position": 4.0})
    assert response.status_code == 422
    assert "风控上限下" in response.json()["detail"]
    assert "买不起一手" in response.json()["detail"]


def test_step_reports_risk_events_and_exits(paper_env: list[date]) -> None:
    detail = create(risk=RISK).json()
    account_id = detail["account"]["id"]
    stepped = client.post(f"/api/v1/paper/accounts/{account_id}/step").json()
    assert isinstance(stepped["this_step"]["risk_events"], list)
    assert isinstance(stepped["this_step"]["risk_exits"], list)
    assert isinstance(stepped["risk_state"], dict)
    assert stepped["risk_state"]["as_of"] == stepped["account"]["as_of"]


def test_run_with_risk_keeps_audit(paper_env: list[date]) -> None:
    detail = create(risk=RISK).json()
    final = client.post(
        f"/api/v1/paper/accounts/{detail['account']['id']}/run",
        json={"approve": "all"},
    ).json()
    assert final["account"]["status"] == "finished"
    assert final["risk_state"]["equity"] > 0
    assert any(event["verdict"] == "cap" for event in final["risk_events"])


def test_run_refuses_while_an_agent_proposal_is_pending(paper_env: list[date]) -> None:
    """收尾项 5a 拍板：Agent 提案逐条审批，批量路径直接拒绝（不是静默跳过）。"""
    detail = create(strategy="ma_cross", params={}).json()
    account_id = detail["account"]["id"]
    db = app.state.db
    as_of = db.paper_accounts[account_id]["as_of"]
    decision = Decision(
        id=decision_id(account_id, as_of, SYMBOL, Side.BUY),
        account_id=account_id,
        symbol=SYMBOL,
        trade_date=as_of,
        side=Side.BUY,
        est_qty=100,
        est_price=100.0,
        reason="Agent 测试提案",
        origin="agent",
    )
    assert asyncio.run(
        db.add_paper_decision(
            account_id=account_id,
            user_id=int(db.paper_accounts[account_id]["user_id"]),
            expected_as_of=as_of,
            decision=decision_to_row(decision),
        )
    )

    response = client.post(
        f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"}
    )

    assert response.status_code == 409
    assert "Agent 提案" in response.text


# ── P0 审计回归：全量重放 + 失败零副作用 ─────────────────────


def test_step_passes_all_decisions_not_the_display_limit(
    paper_env: list[date], signed_in: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重放必须拿到**全量**日志；`paper_decisions(500)` 的展示截断不得混进来。

    修复前 `step` 复用 `DECISION_LIMIT=500`，520 条日志只传 500 条给 replay，最早那条
    `filled` 会被策略输出重新生成并覆盖。这里拦截 `_run_replay` 的入参，只验“传了几条”。
    """
    detail = create().json()
    account_id = detail["account"]["id"]
    initial = len(detail["pending"])  # 建会话当天可能已有待审批决策
    rows = signed_in.paper_decision_rows  # type: ignore[attr-defined]
    for index in range(520):
        decision = Decision(
            id=str(uuid.uuid4()),
            account_id=account_id,
            symbol=SYMBOL,
            trade_date=paper_env[0],
            side=Side.BUY if index % 2 == 0 else Side.SELL,
            est_qty=100,
            est_price=100.0,
            reason=f"P0 日志 {index}",
            status=DecisionStatus.PENDING,
        )
        rows[decision.id] = decision_to_row(decision)

    captured: list[int] = []

    async def fake_run_replay(**kwargs: object) -> None:
        captured.append(len(kwargs["decisions"]))  # type: ignore[arg-type]
        raise PaperError("stop after capture")

    monkeypatch.setattr(paper_api, "_run_replay", fake_run_replay)

    response = client.post(f"/api/v1/paper/accounts/{account_id}/step")

    assert response.status_code == 400
    assert captured == [initial + 520]


def test_run_failure_leaves_pending_decisions_untouched(
    paper_env: list[date], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` 的批量裁决必须与推进同事务：重放失败时一条决策都不许改。

    修复前 `set_paper_decisions_bulk` 先独立提交，随后 replay 抛错——用户看到失败，
    但决策已经变成 approved。
    """
    detail = create().json()
    account_id = detail["account"]["id"]
    pending_ids = [row["id"] for row in detail["pending"]]
    assert pending_ids

    async def boom(**kwargs: object) -> None:
        raise PaperError("replay failed")

    monkeypatch.setattr(paper_api, "_run_replay", boom)

    response = client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})

    assert response.status_code == 400
    after = client.get(f"/api/v1/paper/accounts/{account_id}").json()
    assert [row["id"] for row in after["pending"]] == pending_ids
    assert all(row["status"] == "pending" for row in after["pending"])
