"""M6 用户策略走沙箱的模拟盘路径：**与内置策略逐笔等价** + 坏代码不拖垮服务。

两条夹具讲究（都是踩过才知道的）：

* 跨进程取数只能靠 **`DATA_DIR` 环境变量**——父进程 monkeypatch 查询层对子进程无效
  （子进程是另一个解释器，`get_settings()` 从环境重读）；
* **交易日必须是真交易日**：模拟盘的推进日来自冻结日历，用 `trading_days`（连续日历日）
  会让引擎与模拟盘看到不同的 bar 序列。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.data import calendar as cal
from app.main import app
from app.strategy import SandboxLimits
from app.strategy.sandbox import default_limits as real_default_limits
from app.strategy.templates import template_source
from tests.conftest import make_backtest_dir, write_bars_parquet
from tests.fakes import FakeDatabase

client = TestClient(app)

SYMBOL = "600519"
START = date(2026, 1, 5)
BARS = 40


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[date]:
    days = cal.sessions(START, START + timedelta(days=BARS * 2 + 20))[:BARS]
    rows = [
        {
            "trade_date": day,
            "open": close - 0.05,
            "close": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "volume": 1_000_000.0 + index * 1_000,
        }
        for index, day in enumerate(days)
        for close in [10.0 + math.sin(index / 2.7) * 0.8 + index * 0.02]
    ]
    make_backtest_dir(tmp_path, rows, events=[], symbol=SYMBOL)
    write_bars_parquet(tmp_path / "bars", SYMBOL, rows, adjust="raw")
    # 子进程读的是 Settings：环境变量优先于 .env，父进程与子进程因此看到同一个目录
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield days
    get_settings.cache_clear()


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> dict[str, SandboxLimits]:
    """紧配额，不让坏代码真的等满 20s（同 M4a 口径）。"""
    current = {"value": SandboxLimits(wall_seconds=20, cpu_seconds=10, memory_mb=512,
                                      output_bytes=1_000_000, stderr_bytes=10_000)}
    monkeypatch.setattr("app.strategy.sandbox.default_limits", lambda: current["value"])
    return current


def session_body(days: list[date], **over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "会话",
        "initial_cash": 200_000.0,
        "symbols": [SYMBOL],
        "strategy": "ma_cross",
        "params": {"fast": 5, "slow": 20},
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
    }
    payload.update(over)
    return payload


def make_strategy(code: str, name: str = "我的策略") -> dict[str, Any]:
    response = client.post("/api/v1/strategies", json={"name": name, "code": code})
    assert response.status_code == 201, response.text
    return response.json()


def run_to_end(account_id: str) -> dict[str, Any]:
    response = client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})
    assert response.status_code == 200, response.text
    return response.json()


def test_user_strategy_paper_matches_the_builtin(
    signed_in: FakeDatabase, data_dir: list[date], limits: dict[str, SandboxLimits]
) -> None:
    """模板源码（用户策略）跑模拟盘 == 内置 ma_cross **逐笔相等**。

    这条与 `test_paper_replay` 的 parity 是同一条判据的两半：那边验「模拟盘 == 回测」，
    这边验「用户策略走沙箱 == 内置策略」，合起来即「用户策略的模拟盘 == 内置策略的回测」。
    """
    days = data_dir
    builtin = client.post("/api/v1/paper/accounts", json=session_body(days))
    assert builtin.status_code == 201, builtin.text
    builtin_final = run_to_end(builtin.json()["account"]["id"])

    created = make_strategy(template_source("ma_cross"), name="我的双均线")
    user = client.post(
        "/api/v1/paper/accounts",
        json=session_body(
            days, name="用户策略会话", strategy="user", strategy_id=created["id"]
        ),
    )
    assert user.status_code == 201, user.text
    assert user.json()["account"]["config"]["strategy_name"] == "我的双均线"
    user_final = run_to_end(user.json()["account"]["id"])

    assert len(builtin_final["decisions"]) > 0, "夹具价格序列应当真的产生信号"
    assert [d["fill"] for d in user_final["decisions"]] == [
        d["fill"] for d in builtin_final["decisions"]
    ]
    assert user_final["equity_curve"] == builtin_final["equity_curve"]
    assert user_final["positions"] == builtin_final["positions"]


def test_user_strategy_keeps_running_after_saving(
    signed_in: FakeDatabase, data_dir: list[date], limits: dict[str, SandboxLimits]
) -> None:
    """逐日推进也一样走沙箱（每次 step 都是一次整段重放，跨进程）。"""
    days = data_dir
    created = make_strategy(template_source("ma_cross"), name="逐日")
    detail = client.post(
        "/api/v1/paper/accounts",
        json=session_body(days, strategy="user", strategy_id=created["id"]),
    ).json()
    account_id = detail["account"]["id"]

    for _ in range(3):
        response = client.post(f"/api/v1/paper/accounts/{account_id}/step")
        assert response.status_code == 200, response.text
    assert client.get(f"/api/v1/paper/accounts/{account_id}").json()["progress"]["days_done"] == 4


def test_deleted_strategy_conflicts_instead_of_silently_switching(
    signed_in: FakeDatabase, data_dir: list[date], limits: dict[str, SandboxLimits]
) -> None:
    """会话用的策略被删掉后继续推进 → **409 并说清楚**，不静默换一个策略接着跑。"""
    days = data_dir
    created = make_strategy(template_source("ma_cross"), name="会被删的")
    detail = client.post(
        "/api/v1/paper/accounts",
        json=session_body(days, strategy="user", strategy_id=created["id"]),
    ).json()
    assert client.delete(f"/api/v1/strategies/{created['id']}").status_code == 200

    response = client.post(f"/api/v1/paper/accounts/{detail['account']['id']}/step")
    assert response.status_code == 409
    assert "会被删的" in response.json()["detail"]


def test_broken_strategy_is_rejected_and_service_survives(
    signed_in: FakeDatabase, data_dir: list[date], limits: dict[str, SandboxLimits]
) -> None:
    """运行期抛异常的坏代码：422 带话术，**服务照常**（沙箱把它的代价关在可回收的进程里）。"""
    days = data_dir
    broken = make_strategy(
        'PARAMS = {}\nUSES_EVENTS = False\n\n\ndef on_bar(ctx):\n    raise RuntimeError("故意炸")\n',
        name="坏策略",
    )
    response = client.post(
        "/api/v1/paper/accounts",
        json=session_body(days, strategy="user", strategy_id=broken["id"], params={}),
    )
    assert response.status_code == 422
    assert "故意炸" in response.json()["detail"]

    assert client.get("/health").status_code == 200
    good = make_strategy(template_source("ma_cross"), name="好策略")
    assert (
        client.post(
            "/api/v1/paper/accounts",
            json=session_body(days, strategy="user", strategy_id=good["id"]),
        ).status_code
        == 201
    )


def test_static_check_blocks_before_spawn(
    signed_in: FakeDatabase, data_dir: list[date], limits: dict[str, SandboxLimits]
) -> None:
    """含未来函数的策略在**建会话时**就被拦下（闸门顺序同 `POST /backtest`）。"""
    days = data_dir
    leaky = make_strategy(
        "PARAMS = {}\nUSES_EVENTS = False\n\n\n"
        "def on_bar(ctx):\n    future = ctx.history[ctx.index + 1]\n    return []\n",
        name="前视策略",
    )
    response = client.post(
        "/api/v1/paper/accounts",
        json=session_body(days, strategy="user", strategy_id=leaky["id"], params={}),
    )
    assert response.status_code == 422
    findings = response.json()["findings"]
    assert any(item["rule"] == "R3" for item in findings)
