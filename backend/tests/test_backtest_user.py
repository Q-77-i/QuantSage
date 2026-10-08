"""离线单测：`POST /api/v1/backtest` 的用户策略分支（M4c）。

这里守三件事，都在 HTTP 层（闸门顺序写在 SPEC §5 M4c）：

  * **闸门**：归属 404 → 静态检查 `error`=0 → 参数 → 沙箱。检查必须在 spawn **之前**——
    「运行前带行号给话术」正是这么成立的；
  * **沙箱**：真子进程跑真模板，报告与内置策略同构（只多两个标识键）；
  * **落库身份**：`strategy_id` + `code_sha256` 落进记录，重开时能取回来（前端据此判断
    「已非当次运行的代码」）。

行情走合成 Parquet，**`DATA_DIR` 环境变量是唯一能穿过进程边界的注入方式**——父进程里
monkeypatch 查询层对子进程无效（子进程是另一个解释器）。
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.strategy import SandboxLimits
from app.strategy.sandbox import default_limits as real_default_limits
from app.strategy.templates import TEMPLATES, template_source
from tests.conftest import make_backtest_dir, trading_days, ts
from tests.fakes import FakeDatabase

client = TestClient(app)

SYMBOL = "600519"
START = date(2026, 1, 5)
BARS = 60


def _waves() -> list[dict[str, Any]]:
    """确定性的波动序列：够长到 MA5/MA20 都能算，且会真的产生金叉死叉。"""
    rows = []
    for index, day in enumerate(trading_days(START, BARS)):
        close = 10.0 + math.sin(index / 2.7) * 0.8 + index * 0.02
        rows.append(
            {
                "trade_date": day,
                "open": close - 0.05,
                "close": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "volume": 1_000_000.0 + index * 1000,
            }
        )
    return rows


def _events() -> list[dict[str, Any]]:
    """两条利多事件（评分高于缺省门槛），供事件驱动模板触发。"""
    return [
        {
            "event_id": f"evt-{index}",
            "event_time": ts(f"2026-02-{index + 1:02d} 10:00:00"),
            "available_at": ts(f"2026-02-{index + 1:02d} 10:00:00"),
            "title": "公司发布业绩预增公告",
            "direction_norm": "positive",
            "score": 80.0,
        }
        for index in range(2)
    ]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_backtest_dir(tmp_path, _waves(), _events(), symbol=SYMBOL)
    # 子进程读的是 Settings：环境变量优先于 .env，父进程与子进程因此看到同一个目录
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> dict[str, SandboxLimits]:
    """可切换的配额：故障矩阵用紧配额（不等 20s），跑通用正常配额。"""
    current = {"value": real_default_limits()}
    monkeypatch.setattr("app.strategy.sandbox.default_limits", lambda: current["value"])
    return current


def make_strategy(code: str, name: str = "我的策略") -> dict[str, Any]:
    response = client.post("/api/v1/strategies", json={"name": name, "code": code})
    assert response.status_code == 201, response.text
    return response.json()


def run_user(strategy_id: str | None, **overrides: Any):
    body: dict[str, Any] = {"strategy": "user", "symbol": SYMBOL, **overrides}
    if strategy_id is not None:
        body["strategy_id"] = strategy_id
    return client.post("/api/v1/backtest", json=body)


# ── 跑通 ────────────────────────────────────────────────────────────────────


def test_user_strategy_runs_and_reports_identity(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    created = make_strategy(template_source("ma_cross"), name="我的双均线")
    response = run_user(created["id"])
    assert response.status_code == 200, response.text

    payload = response.json()
    meta = payload["report"]["meta"]
    assert meta["strategy_kind"] == "user"
    assert meta["strategy_name"] == "我的双均线"
    assert payload["report"]["metrics"]["trade_count"] >= 1  # 合成序列真的成交了

    detail = client.get(f"/api/v1/backtest/runs/{payload['run_id']}").json()
    assert detail["strategy_id"] == created["id"]
    assert detail["code_sha256"] == created["code_sha256"]
    assert detail["request"]["strategy"] == "user"

    summary = client.get("/api/v1/backtest/runs").json()[0]
    assert summary["strategy"] == "user"
    assert summary["strategy_name"] == "我的双均线"  # 列表显示「用户策略 · 名称」靠它


def test_builtin_runs_keep_strategy_name_null(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    assert client.post(
        "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL}
    ).status_code == 200
    assert client.get("/api/v1/backtest/runs").json()[0]["strategy_name"] is None


def test_event_template_yields_pit_comparison(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """`USES_EVENTS = True` 的模板在 `pit_mode="both"` 下真的产出对比表。

    判据从「策略名 = event_driven」泛化成源码声明之后，这条是它唯一的行为证据。
    """
    created = make_strategy(template_source("event_driven"), name="事件驱动")
    response = run_user(created["id"], pit_mode="both", start="2026-02-01", end="2026-02-10")
    assert response.status_code == 200, response.text
    assert response.json()["report"]["pit_comparison"] is not None


def test_not_using_events_skips_pit_comparison(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    created = make_strategy(template_source("ma_cross"), name="不看事件")
    response = run_user(created["id"], pit_mode="both")
    assert response.status_code == 200, response.text
    assert response.json()["report"]["pit_comparison"] is None  # 两模式必然同结果


# ── 闸门 ────────────────────────────────────────────────────────────────────


def test_gate_rejects_future_function_with_findings(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """含未来索引的**库存源码**跑不动，且给出 findings（带行号）——不是「跑完才知道」。"""
    created = make_strategy(
        "def on_bar(ctx):\n    return [] if ctx.history[ctx.index + 1] else []\n",
        name="偷看未来",
    )
    response = run_user(created["id"])
    assert response.status_code == 422, response.text
    body = response.json()
    assert "静态检查" in body["detail"]
    assert any(item["rule"] == "R3" and item["line"] == 2 for item in body["findings"])


def test_gate_requires_strategy_id(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    assert run_user(None).status_code == 422  # 缺 strategy_id
    created = make_strategy(template_source("ma_cross"))
    # 与非 user 策略同给也拒绝（互斥）
    mixed = client.post(
        "/api/v1/backtest",
        json={"strategy": "ma_cross", "symbol": SYMBOL, "strategy_id": created["id"]},
    )
    assert mixed.status_code == 422


def test_gate_rejects_unknown_or_foreign_strategy(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    missing = "00000000-0000-0000-0000-0000000000ee"
    assert run_user(missing).status_code == 404
    assert run_user("not-a-uuid").status_code == 422


def test_gate_validates_params_against_source_schema(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    created = make_strategy(template_source("ma_cross"))

    unknown = run_user(created["id"], params={"nope": 1})
    assert unknown.status_code == 422
    assert "不接受参数" in unknown.json()["detail"]

    out_of_range = run_user(created["id"], params={"fast": 999, "slow": 20})
    assert out_of_range.status_code == 422
    assert "上限" in out_of_range.json()["detail"]

    # 跨字段约束走用户自己的 validate_params（`fast < slow`）
    crossed = run_user(created["id"], params={"fast": 20, "slow": 5})
    assert crossed.status_code == 422
    assert "慢线" in crossed.json()["detail"]


def test_runtime_error_maps_to_422_with_line(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    created = make_strategy(
        "def on_bar(ctx):\n    return [1 / 0]\n", name="除零"
    )
    response = run_user(created["id"])
    assert response.status_code == 422, response.text
    assert response.json()["line"] == 2


# ── 故障矩阵（坏代码不拖垮服务）─────────────────────────────────────────────


BAD_CODES: dict[str, tuple[str, int]] = {
    # 名字 → （源码，期望状态码）
    "死循环": ("def on_bar(ctx):\n    while True:\n        pass\n", 400),
    "大内存": (
        "def on_bar(ctx):\n    blob = []\n    while True:\n        blob.append(bytearray(1024 * 1024))\n",
        400,
    ),
    "语法错": ("def on_bar(ctx)\n    return []\n", 422),
    "缺 on_bar": ("x = 1\n", 422),
    "数据绕行": ("import duckdb\n\n\ndef on_bar(ctx):\n    return []\n", 422),
    "返回值形态": ("def on_bar(ctx):\n    return 1\n", 422),
}


def test_fault_matrix_keeps_service_alive(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """六条坏法各给明确错误码，且**每条之后服务仍活着**（`/health` 200）。

    配额压到「2s CPU / 1.5s 墙钟 / 200MB 内存 / 64KB 输出」，与 M4a 的矩阵同口径——
    否则一条死循环用例就要真等 20s。
    """
    limits["value"] = SandboxLimits(
        wall_seconds=1.5, cpu_seconds=2, memory_mb=200, output_bytes=64 * 1024
    )
    seen: dict[str, int] = {}
    for name, (code, expected) in BAD_CODES.items():
        created = make_strategy(code, name=f"坏法-{name}")
        response = run_user(created["id"])
        assert response.status_code == expected, (name, response.status_code, response.text)
        assert client.get("/health").status_code == 200
        seen[name] = response.status_code

    # 紧配额下超长输出（报告本身超过 64KB）也要被拦成 400
    limits["value"] = SandboxLimits(
        wall_seconds=20, cpu_seconds=15, memory_mb=512, output_bytes=200
    )
    created = make_strategy(template_source("ma_cross"), name="输出超限")
    assert run_user(created["id"]).status_code == 400

    # 服务照常：换回正常配额，好策略仍能跑通
    limits["value"] = real_default_limits()
    healthy = make_strategy(template_source("ma_cross"), name="恢复后")
    assert run_user(healthy["id"]).status_code == 200
    assert client.get("/health").status_code == 200
    assert set(seen) == set(BAD_CODES)


def test_sandbox_termination_carries_kind(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """配额类失败是 **400 + kind**（请求合法，是这次运行没能完成）。"""
    limits["value"] = SandboxLimits(wall_seconds=1.5, cpu_seconds=600, memory_mb=512)
    created = make_strategy("def on_bar(ctx):\n    while True:\n        pass\n", name="死循环")
    response = run_user(created["id"])
    assert response.status_code == 400
    assert response.json()["kind"] in {"wall", "cpu"}


def test_all_templates_run(signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]) -> None:
    """5 个模板都经 HTTP + 沙箱跑得通（M4b 的模板在真实链路上的一次全量回归）。"""
    for template in TEMPLATES:
        created = make_strategy(template.source, name=f"模板-{template.key}")
        response = run_user(created["id"])
        assert response.status_code == 200, (template.key, response.text)
        assert response.json()["report"]["meta"]["strategy_name"] == f"模板-{template.key}"


def test_run_window_defaults_follow_uses_events(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """缺省窗口按 `USES_EVENTS` 走：声明 True 的从事件语料起点开跑。"""
    created = make_strategy(template_source("event_driven"), name="看事件")
    response = run_user(created["id"], start=None)
    assert response.status_code == 200, response.text
    assert response.json()["report"]["meta"]["start"] == "2026-02-01"


def test_deleted_strategy_leaves_runs_intact(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """删策略**不级联删**回测记录：报告 JSONB 自足，`strategy_id` 悬空即可。"""
    created = make_strategy(template_source("ma_cross"))
    payload = run_user(created["id"]).json()
    assert client.delete(f"/api/v1/strategies/{created['id']}").status_code == 200

    detail = client.get(f"/api/v1/backtest/runs/{payload['run_id']}").json()
    assert detail["strategy_id"] == created["id"]
    assert detail["report"]["metrics"]["trade_count"] >= 1
    assert run_user(created["id"]).status_code == 404  # 但不能再跑它
