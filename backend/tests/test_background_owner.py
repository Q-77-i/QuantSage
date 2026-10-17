"""后台主实例生命周期（E2）：只有持锁进程启动 worker 与两个调度器。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import main as main_module
from app.main import _Background, _start_background, _stop_background


class _Scheduler:
    """只记 shutdown 次数的调度器替身。"""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self, wait: bool = False) -> None:
        self.shutdown_calls += 1


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


def _patch_worker(monkeypatch: pytest.MonkeyPatch, handle: Any) -> list[Any]:
    calls: list[Any] = []

    async def start(db: Any) -> Any:
        calls.append(db)
        return handle

    async def stop(worker: Any) -> None:
        calls.append(("stop", worker))

    monkeypatch.setattr(main_module.task_worker, "start", start)
    monkeypatch.setattr(main_module.task_worker, "stop", stop)
    return calls


def _patch_schedulers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    etl: Any,
    settle: Any,
    etl_raises: bool = False,
) -> dict[str, int]:
    calls = {"etl": 0, "settle": 0}

    def start_etl() -> Any:
        calls["etl"] += 1
        if etl_raises:
            raise RuntimeError("ETL 调度器炸了")
        return etl

    def start_settle(_app: Any) -> Any:
        calls["settle"] += 1
        return settle

    monkeypatch.setattr(main_module, "start_scheduler", start_etl)
    monkeypatch.setattr(main_module, "start_settlement_scheduler", start_settle)
    return calls


def test_no_db_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app()
    worker_calls = _patch_worker(monkeypatch, handle=object())
    scheduler_calls = _patch_schedulers(monkeypatch, etl=_Scheduler(), settle=_Scheduler())

    background = asyncio.run(_start_background(app, None))

    assert background.owner is None
    assert worker_calls == []
    assert scheduler_calls == {"etl": 0, "settle": 0}
    assert getattr(app.state, "task_worker", None) is None


def test_standby_does_not_start_schedulers(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁被别的进程持有：只服务 HTTP，不跑 worker/调度器。"""
    app = _app()
    worker_calls = _patch_worker(monkeypatch, handle=None)
    scheduler_calls = _patch_schedulers(monkeypatch, etl=_Scheduler(), settle=_Scheduler())

    background = asyncio.run(_start_background(app, object()))

    assert background.owner is False
    assert background.worker is None
    assert len(worker_calls) == 1
    assert scheduler_calls == {"etl": 0, "settle": 0}


def test_owner_starts_worker_and_both_schedulers(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app()
    handle = object()
    _patch_worker(monkeypatch, handle=handle)
    etl, settle = _Scheduler(), _Scheduler()
    scheduler_calls = _patch_schedulers(monkeypatch, etl=etl, settle=settle)

    background = asyncio.run(_start_background(app, object()))

    assert background.owner is True
    assert background.worker is handle
    assert app.state.task_worker is handle
    assert app.state.scheduler is etl
    assert app.state.settle_scheduler is settle
    assert scheduler_calls == {"etl": 1, "settle": 1}


def test_etl_failure_keeps_owner_and_starts_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个调度器起不来不影响另一个，也不改变主实例身份。"""
    app = _app()
    handle = object()
    _patch_worker(monkeypatch, handle=handle)
    settle = _Scheduler()
    scheduler_calls = _patch_schedulers(
        monkeypatch, etl=None, settle=settle, etl_raises=True
    )

    background = asyncio.run(_start_background(app, object()))

    assert background.owner is True
    assert background.scheduler is None
    assert background.settle_scheduler is settle
    assert scheduler_calls == {"etl": 1, "settle": 1}


def test_worker_start_failure_starts_no_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 起不来时保留手动 ETL 的逃生口（owner=None），但不启动调度器。"""
    app = _app()

    async def boom(_db: Any) -> Any:
        raise RuntimeError("库抖了一下")

    monkeypatch.setattr(main_module.task_worker, "start", boom)
    scheduler_calls = _patch_schedulers(monkeypatch, etl=_Scheduler(), settle=_Scheduler())

    background = asyncio.run(_start_background(app, object()))

    assert background.owner is None
    assert scheduler_calls == {"etl": 0, "settle": 0}


def test_stop_background_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_calls = _patch_worker(monkeypatch, handle=None)
    etl, settle = _Scheduler(), _Scheduler()
    background = _Background(
        owner=True, worker=object(), scheduler=etl, settle_scheduler=settle
    )

    asyncio.run(_stop_background(background))
    asyncio.run(_stop_background(background))

    assert len(worker_calls) == 1
    assert worker_calls[0][0] == "stop"
    assert etl.shutdown_calls == 1
    assert settle.shutdown_calls == 1
    assert background.worker is None
    assert background.scheduler is None
    assert background.settle_scheduler is None
