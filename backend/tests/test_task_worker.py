"""离线单测：任务 worker 与执行体（P3-E1a）。

不起常驻 worker，直接 `asyncio.run(run_once(db))` 驱动（理由见 SPEC §10 E1）。
本文件守四件事：

  * **取件与终态**：FIFO 串行、成功落 `backtest_runs` + `result.run_id`、失败落 `error` 且**不落 run**；
  * **冻结材料**：执行体只吃任务行——落进 `backtest_runs.request` 的是**用户可见的那一份**
    （执行字段不进回测记录表，源码只留 `code_sha256`）；
  * **取消与扫尾**：`queued` 被取消后不再执行；`running` 被扫尾标 `failed/interrupted` 之后，
    迟到的 `finish_task` 不得把它覆盖回 `done`；
  * **用户策略分支**：真沙箱子进程跑真模板（与 `test_backtest_user.py` 同一姿态）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.core.config import get_settings
from app.data import duckdb_client
from app.strategy import code_sha256
from app.strategy.templates import template_source
from app.tasks import worker
from tests.conftest import TEST_USER, drive_tasks, make_backtest_dir, trading_days
from tests.fakes import FakeDatabase

SYMBOL = "600519"
OTHER_SYMBOL = "600036"
START = date(2026, 7, 1)
BAR_COUNT = 60
LAST_DAY = trading_days(START, BAR_COUNT)[-1]


def sample_bars(offset: float = 0.0) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": day,
            "open": 100.0 + i + offset,
            "close": 100.5 + i + offset,
            "high": 101.0 + i + offset,
            "low": 99.0 + i + offset,
        }
        for i, day in enumerate(trading_days(START, BAR_COUNT))
    ]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_backtest_dir(tmp_path, sample_bars(), [], symbol=SYMBOL)
    make_backtest_dir(tmp_path, sample_bars(offset=5), [], symbol=OTHER_SYMBOL)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    # 用户策略分支的子进程读 Settings 而非 monkeypatch：环境变量是唯一穿过进程边界的注入
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def frozen_request(symbol: str = SYMBOL, **overrides: Any) -> dict[str, Any]:
    """任务行里 `request` 的形状（提交时冻结，见 SPEC §2.1）。"""
    request: dict[str, Any] = {
        "strategy": "ma_cross",
        "symbol": symbol,
        "start": START.isoformat(),
        "end": LAST_DAY.isoformat(),
        "adjust": "qfq",
        "pit_mode": "pit",
        "costs": {"fees": True, "slippage": True, "slippage_bps": 5.0},
        "params": {},
        "compare_pit": False,
    }
    request.update(overrides)
    return request


def queue(db: FakeDatabase, request: dict[str, Any], kind: str = "backtest") -> str:
    task_id = str(uuid.uuid4())
    asyncio.run(db.create_task(task_id, TEST_USER["id"], kind, request))
    return task_id


# ── 取件与终态 ──────────────────────────────────────────────────────────────


def test_no_queued_task_returns_false(signed_in: FakeDatabase) -> None:
    assert asyncio.run(worker.run_once(signed_in)) is False


def test_successful_task_saves_run_and_marks_done(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    task_id = queue(signed_in, frozen_request())

    assert drive_tasks(signed_in) == 1
    task = signed_in.tasks[task_id]
    assert task["status"] == "done"
    assert task["progress"] == {"done": 1, "total": 1}
    assert task["finished_at"] is not None

    run_id = task["result"]["run_id"]
    record = signed_in.runs[run_id]
    assert record["user_id"] == TEST_USER["id"]
    assert record["report"]["meta"]["symbol"] == SYMBOL
    # 落库的 request 是**用户可见的那一份**：执行字段一个都不许进回测记录表
    for field in ("source", "strategy_name", "uses_events", "compare_pit"):
        assert field not in record["request"]
    assert record["request"]["pit_mode"] == "pit"


def test_run_carries_data_digest(signed_in: FakeDatabase, data_dir: Path) -> None:
    """E7d：执行体把**窗口数据指纹**同时落进记录列与报告 meta——两处必须同一个值。

    指纹的鉴别力在 `test_snapshot_window.py` 里测（同数据同值、窗口外改动不影响）；
    这里钉的是**接线**：执行体算了一次、两处写了一样的。
    """
    task_id = queue(signed_in, frozen_request())

    assert drive_tasks(signed_in) == 1
    record = signed_in.runs[signed_in.tasks[task_id]["result"]["run_id"]]

    digest = record["data_digest"]
    assert isinstance(digest, str) and len(digest) == 64
    assert record["report"]["meta"]["data_digest"] == digest
    # 列表摘要也带得出（「我的回测」不看完整报告）
    summaries = asyncio.run(signed_in.list_backtest_runs(TEST_USER["id"], 10))
    assert summaries[0]["data_digest"] == digest


def test_tasks_run_in_fifo_order(signed_in: FakeDatabase, data_dir: Path) -> None:
    first = queue(signed_in, frozen_request(symbol=SYMBOL))
    second = queue(signed_in, frozen_request(symbol=OTHER_SYMBOL))

    assert drive_tasks(signed_in) == 2
    runs = [signed_in.runs[row_id] for row_id in signed_in._run_order]
    assert [row["request"]["symbol"] for row in runs] == [SYMBOL, OTHER_SYMBOL]
    assert [signed_in.tasks[tid]["status"] for tid in (first, second)] == ["done", "done"]


def test_failed_task_is_marked_failed_without_run(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """执行期出错 → 任务失败、**不落记录**（与同步时代「异常路径不留半条」同语义）。

    `kind` 走 `backtest.batch.error_of` 那一套词汇：区间内无 bar 在**提交期**由
    `resolve_window` 拦下（404，见端点用例），能走到执行体的同类错误是引擎自己抛的
    `BacktestError` —— 它必须落成 `failed` 而不是丢进日志。
    """
    task_id = queue(signed_in, frozen_request(symbol="000001"))

    assert drive_tasks(signed_in) == 1
    task = signed_in.tasks[task_id]
    assert task["status"] == "failed"
    assert task["result"] is None
    assert task["error"]["kind"] == "backtest"
    assert "无行情数据" in task["error"]["message"]
    assert signed_in.runs == {}


def test_unknown_kind_fails_the_task(signed_in: FakeDatabase) -> None:
    task_id = queue(signed_in, frozen_request(), kind="no_such_kind")

    assert drive_tasks(signed_in) == 1
    assert signed_in.tasks[task_id]["status"] == "failed"
    assert signed_in.tasks[task_id]["error"]["kind"] == "internal"


# ── 取消与扫尾 ──────────────────────────────────────────────────────────────


def test_cancelled_task_is_not_executed(signed_in: FakeDatabase, data_dir: Path) -> None:
    dropped = queue(signed_in, frozen_request())
    kept = queue(signed_in, frozen_request(symbol=OTHER_SYMBOL))
    asyncio.run(signed_in.cancel_task(TEST_USER["id"], dropped))

    assert drive_tasks(signed_in) == 1
    assert signed_in.tasks[dropped]["status"] == "cancelled"
    assert signed_in.tasks[dropped]["result"] is None
    assert signed_in.tasks[kept]["status"] == "done"
    assert len(signed_in.runs) == 1


def test_sweep_marks_interrupted_and_blocks_late_finish(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """扫尾之后，迟到的完成不得把 `failed` 覆盖回 `done`（真库的 `WHERE status='running'` 同义）。"""
    task_id = queue(signed_in, frozen_request())
    claimed = asyncio.run(signed_in.claim_next_task())
    assert claimed is not None  # 取件后卡在 running（模拟进程被杀）

    assert asyncio.run(signed_in.sweep_interrupted_tasks()) == 1
    task = signed_in.tasks[task_id]
    assert task["status"] == "failed"
    assert task["error"]["kind"] == "interrupted"

    asyncio.run(signed_in.finish_task(task_id, result={"run_id": "late"}))
    assert signed_in.tasks[task_id]["status"] == "failed"
    assert signed_in.tasks[task_id]["result"] is None


def test_start_sweeps_then_serves(signed_in: FakeDatabase, data_dir: Path) -> None:
    """`start()` = 扫尾 + 常驻循环；`stop()` 取消它。"""
    task_id = queue(signed_in, frozen_request())
    asyncio.run(signed_in.claim_next_task())  # 一条卡在 running 的旧行

    async def scenario() -> None:
        serving = await worker.start(signed_in)
        await worker.stop(serving)

    asyncio.run(scenario())
    assert signed_in.tasks[task_id]["status"] == "failed"  # 扫尾生效
    assert signed_in.tasks[task_id]["error"]["kind"] == "interrupted"


def test_start_does_not_sweep_when_lock_held(
    signed_in: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拿不到单实例锁的进程**不扫尾、不取件**。

    多进程部署时进程 B 启动，若照旧扫尾，会把进程 A 正在跑的 `running` 标成
    failed；A 的 `finish_task` 因 `WHERE status='running'` 静默不写，任务结果就丢了。
    """
    task_id = queue(signed_in, frozen_request())
    asyncio.run(signed_in.claim_next_task())  # 进程 A 正在跑

    async def no_lock(_db: Any) -> None:
        return None

    monkeypatch.setattr(worker, "_acquire_instance_lock", no_lock)

    async def scenario() -> worker.WorkerHandle | None:
        return await worker.start(signed_in)

    assert asyncio.run(scenario()) is None
    assert signed_in.tasks[task_id]["status"] == "running"  # 没有被 B 杀掉


# ── 用户策略分支 ────────────────────────────────────────────────────────────


def test_user_strategy_task_uses_frozen_source(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """跑的是**任务行里冻结的源码**：`code_sha256` 与冻结值一致，`strategy_id` 落库。"""
    source = template_source("ma_cross")
    request = frozen_request(
        strategy="user",
        strategy_id=str(uuid.uuid4()),
        strategy_name="我的双均线",
        source=source,
        uses_events=False,
        compare_pit=False,
    )
    task_id = queue(signed_in, request)

    assert drive_tasks(signed_in) == 1
    assert signed_in.tasks[task_id]["status"] == "done"

    record = signed_in.runs[signed_in.tasks[task_id]["result"]["run_id"]]
    assert record["code_sha256"] == code_sha256(source)
    assert record["strategy_id"] == request["strategy_id"]
    assert record["report"]["meta"]["strategy_kind"] == "user"
    assert record["report"]["meta"]["strategy_name"] == "我的双均线"
