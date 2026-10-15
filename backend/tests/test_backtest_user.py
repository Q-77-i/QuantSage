"""离线单测：`POST /api/v1/backtest` 的用户策略分支（M4c）。

这里守三件事（闸门顺序写在 SPEC §5 M4c）：

  * **闸门**：归属 404 → 静态检查 `error`=0 → 参数 → 入队。检查必须在跑之前——
    「运行前带行号给话术」正是这么成立的；
  * **沙箱**：真子进程跑真模板，报告与内置策略同构（只多两个标识键）；
  * **落库身份**：`strategy_id` + `code_sha256` 落进记录，重开时能取回来（前端据此判断
    「已非当次运行的代码」）。

**P3-E1a 的语义分界**（异步化后的必然结果，别把它当回归）：提交期能判的一律仍是同步 4xx——
归属 404、静态检查 422（带 findings）、参数 422、窗口 404/503；只有**跑起来才知道**的那些
（沙箱配额、用户代码运行期异常）落在任务的 `error` 里（`{kind, message}`，用户代码的问题
另带 `line`，与旧 422 响应体逐字同形）。所以下面分成「闸门」与「运行期」两组用例。

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
from tests.conftest import (
    NO_MEMORY_LIMIT,
    drive_tasks,
    make_backtest_dir,
    submit_and_run,
    trading_days,
    ts,
)
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


def user_body(strategy_id: str | None, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"strategy": "user", "symbol": SYMBOL, **overrides}
    if strategy_id is not None:
        body["strategy_id"] = strategy_id
    return body


def run_user(strategy_id: str | None, **overrides: Any):
    """**提交**一次用户策略回测（P3-E1a 起回 202）。验闸门状态码的用例用它。"""
    return client.post("/api/v1/backtest", json=user_body(strategy_id, **overrides))


def run_user_task(strategy_id: str | None, **overrides: Any) -> dict[str, Any]:
    """提交 + 跑队列，返回**任务详情**——运行期失败看 `error`，成功看 `result`。"""
    response = run_user(strategy_id, **overrides)
    assert response.status_code == 202, response.text
    drive_tasks(app.state.db)
    return client.get(f"/api/v1/tasks/{response.json()['task_id']}").json()


def run_user_report(strategy_id: str | None, **overrides: Any) -> dict[str, Any]:
    """提交 + 跑 + 取回 `{run_id, report}`（成功路径）。"""
    return submit_and_run(client, user_body(strategy_id, **overrides))


# ── 跑通 ────────────────────────────────────────────────────────────────────


def test_user_strategy_runs_and_reports_identity(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    created = make_strategy(template_source("ma_cross"), name="我的双均线")
    payload = run_user_report(created["id"])

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
    submit_and_run(client, {"strategy": "ma_cross", "symbol": SYMBOL})
    assert client.get("/api/v1/backtest/runs").json()[0]["strategy_name"] is None


def test_event_template_yields_pit_comparison(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """`USES_EVENTS = True` 的模板在 `pit_mode="both"` 下真的产出对比表。

    判据从「策略名 = event_driven」泛化成源码声明之后，这条是它唯一的行为证据。
    """
    created = make_strategy(template_source("event_driven"), name="事件驱动")
    report = run_user_report(
        created["id"], pit_mode="both", start="2026-02-01", end="2026-02-10"
    )["report"]
    assert report["pit_comparison"] is not None


def test_not_using_events_skips_pit_comparison(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    created = make_strategy(template_source("ma_cross"), name="不看事件")
    report = run_user_report(created["id"], pit_mode="both")["report"]
    assert report["pit_comparison"] is None  # 两模式必然同结果


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

    # 跨字段约束走**用户自己**的 validate_params（`fast < slow`）——它在沙箱里跑，
    # 所以是运行期错误：提交 202，失败落在任务的 `error`（E1a 的分界，见文件头）
    crossed = run_user_task(created["id"], params={"fast": 20, "slow": 5})
    assert crossed["status"] == "failed"
    assert crossed["error"]["kind"] == "rejected"
    assert "慢线" in crossed["error"]["message"]


def test_bool_param_survives_the_request_model(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """用户策略的 `bool` 参数要真的能跑（M4c-3 走查逮到的回归）。

    症状曾是 422「参数 xxx 需要 true / false（当前 0.0）」——病根不在校验器，而在
    `BacktestRequest.params: dict[str, float]`：pydantic 把 JSON 的 `false` 收成了 `0.0`，
    于是开关参数一路走到 `strategy.params.coerce` 才炸。**现象离病根隔了三层**，
    所以这条用例从端点打到落库，把整条链钉住。
    """
    created = make_strategy(
        'PARAMS = {"window": {"type": "int", "default": 5, "min": 1, "max": 60, "label": "窗口"},\n'
        '          "keep_flat": {"type": "bool", "default": True, "label": "只空仓观察"}}\n'
        "USES_EVENTS = False\n\n\n"
        "def on_bar(ctx, p):\n"
        "    return []\n",
        name="带开关",
    )

    payload = run_user_report(created["id"], params={"window": 5, "keep_flat": False})
    metrics = payload["report"]["metrics"]
    assert metrics["trade_count"] == 0  # 空仓观察 → 一笔都不成

    detail = client.get(f"/api/v1/backtest/runs/{payload['run_id']}").json()
    # 落库的是**布尔**（不是 0.0）：JSONB 里 `false` 与 `0` 是两种东西，前端据此回填开关
    assert detail["request"]["params"]["keep_flat"] is False


def test_runtime_error_lands_in_task_with_line(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """运行期异常：提交是 202（那时还看不到它），失败落在任务 `error`——**行号照给**。

    `line` 的形状与旧 422 响应体逐字一致（`{kind, message, line}`），前端不写第二套。
    """
    created = make_strategy("def on_bar(ctx):\n    return [1 / 0]\n", name="除零")
    task = run_user_task(created["id"])

    assert task["status"] == "failed"
    assert task["result"] is None
    assert task["error"]["kind"] == "rejected"
    assert task["error"]["line"] == 2
    assert signed_in.runs == {}  # 失败照样不落记录


# ── 故障矩阵（坏代码不拖垮服务）─────────────────────────────────────────────


#: 六条坏法 → 它们在**哪一关**被拦：静态检查（提交期 422，带 findings）还是运行期（任务 error）。
#: 这条分界就是 P3-E1a 的语义边界：能静态判的仍然同步，跑起来才知道的落任务。
BAD_CODES: dict[str, tuple[str, str, object]] = {
    # 名字 → （源码，拦截点，期望值）
    "死循环": ("def on_bar(ctx):\n    while True:\n        pass\n", "runtime", {"wall", "cpu"}),
    "大内存": (
        "def on_bar(ctx):\n    blob = []\n    while True:\n        blob.append(bytearray(1024 * 1024))\n",
        "runtime",
        {"memory"},
    ),
    "语法错": ("def on_bar(ctx)\n    return []\n", "submit", 422),
    "缺 on_bar": ("x = 1\n", "submit", 422),
    "数据绕行": ("import duckdb\n\n\ndef on_bar(ctx):\n    return []\n", "submit", 422),
    # R5 静态规则就能看出返回形态不对（`return 1`），所以它在提交期被拦，不用等跑
    "返回值形态": ("def on_bar(ctx):\n    return 1\n", "submit", 422),
}

#: 运行期那两条坏法**各给一套配额**，因为「谁先触发」正是它们要验的东西：
#: 「死循环」要在 wall/cpu 上被拦 ⇒ 内存阈值必须高到不抢先；「大内存」反过来靠内存拦。
#: **共用一套紧配额是错的**——内存压到 200MB 时，Linux 上子进程的 RSS 基线本身就逼近它，
#: 死循环会先死在内存在，报出来的 `kind` 不是 wall/cpu（CI 首跑就是这么红的，2026-10-15）。
RUNTIME_LIMITS: dict[str, SandboxLimits] = {
    "死循环": SandboxLimits(
        wall_seconds=1.5, cpu_seconds=2, memory_mb=NO_MEMORY_LIMIT, output_bytes=64 * 1024
    ),
    # 墙钟给得比「死循环」宽：这条要留出真的把内存吃上去的时间
    "大内存": SandboxLimits(
        wall_seconds=10.0, cpu_seconds=10, memory_mb=200, output_bytes=64 * 1024
    ),
}


def test_fault_matrix_keeps_service_alive(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """六条坏法各给明确错误，且**每条之后服务仍活着**（`/health` 200）。

    运行期那两条的配额见 `RUNTIME_LIMITS`——**各给一套**，不能共用。
    """
    seen: dict[str, object] = {}
    for name, (code, stage, expected) in BAD_CODES.items():
        created = make_strategy(code, name=f"坏法-{name}")
        if stage == "submit":  # 静态检查拦在提交期：202 都不给，findings 直接交出去
            response = run_user(created["id"])
            assert response.status_code == expected, (name, response.status_code, response.text)
            assert response.json()["findings"]
            seen[name] = response.status_code
        else:  # 运行期才知道的：提交 202，失败落任务
            limits["value"] = RUNTIME_LIMITS[name]
            task = run_user_task(created["id"])
            assert task["status"] == "failed", (name, task)
            assert task["error"]["kind"] in expected, (name, task["error"])
            seen[name] = task["error"]["kind"]
        assert client.get("/health").status_code == 200

    # 紧配额下超长输出（报告本身超过 64KB）也要被拦成运行期错误
    limits["value"] = SandboxLimits(
        wall_seconds=20, cpu_seconds=15, memory_mb=NO_MEMORY_LIMIT, output_bytes=200
    )
    created = make_strategy(template_source("ma_cross"), name="输出超限")
    assert run_user_task(created["id"])["error"]["kind"] == "output"

    # 服务照常：换回正常配额，好策略仍能跑通
    limits["value"] = real_default_limits()
    healthy = make_strategy(template_source("ma_cross"), name="恢复后")
    assert run_user_report(healthy["id"])["report"]["metrics"]["trade_count"] >= 1
    assert client.get("/health").status_code == 200
    assert set(seen) == set(BAD_CODES)


def test_sandbox_termination_carries_kind(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """配额类失败带 `kind`（请求合法，是这次运行没能完成）——现在落在任务的 `error` 里。"""
    limits["value"] = SandboxLimits(wall_seconds=1.5, cpu_seconds=600, memory_mb=NO_MEMORY_LIMIT)
    created = make_strategy("def on_bar(ctx):\n    while True:\n        pass\n", name="死循环")
    task = run_user_task(created["id"])
    assert task["status"] == "failed"
    assert task["error"]["kind"] in {"wall", "cpu"}


def test_all_templates_run(signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]) -> None:
    """5 个模板都经 HTTP + 队列 + 沙箱跑得通（M4b 的模板在真实链路上的一次全量回归）。"""
    for template in TEMPLATES:
        created = make_strategy(template.source, name=f"模板-{template.key}")
        report = run_user_report(created["id"])["report"]
        assert report["meta"]["strategy_name"] == f"模板-{template.key}"


def test_run_window_defaults_follow_uses_events(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """缺省窗口按 `USES_EVENTS` 走：声明 True 的从事件语料起点开跑。"""
    created = make_strategy(template_source("event_driven"), name="看事件")
    report = run_user_report(created["id"], start=None)["report"]
    assert report["meta"]["start"] == "2026-02-01"


def test_deleted_strategy_leaves_runs_intact(
    signed_in: FakeDatabase, data_dir: Path, limits: dict[str, SandboxLimits]
) -> None:
    """删策略**不级联删**回测记录：报告 JSONB 自足，`strategy_id` 悬空即可。"""
    created = make_strategy(template_source("ma_cross"))
    payload = run_user_report(created["id"])
    assert client.delete(f"/api/v1/strategies/{created['id']}").status_code == 200

    detail = client.get(f"/api/v1/backtest/runs/{payload['run_id']}").json()
    assert detail["strategy_id"] == created["id"]
    assert detail["report"]["metrics"]["trade_count"] >= 1
    assert run_user(created["id"]).status_code == 404  # 但不能再跑它
