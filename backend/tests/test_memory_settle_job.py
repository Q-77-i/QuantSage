"""P2 T8：结算调度器的作业体 `settle_all_accounts`。

调度器本身只验触发时刻；这里验作业体：keyset 翻页覆盖 200+ 账户、单账户失败隔离、
计数与依赖未就绪时的短路。用内存替身与假 `settle_account`，不调模型。
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.memory import scheduler
from tests.fakes import FakeDatabase


def _seed(db: FakeDatabase, count: int) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        account_id = f"00000000-0000-4000-8000-{index:012d}"
        db.paper_accounts[account_id] = {
            "user_id": 1,
            "name": f"账户 {index}",
            "status": "active",
            "as_of": date(2026, 9, 30),
            "config": {},
        }
        ids.append(account_id)
    return ids


def test_settle_all_accounts_pages_and_isolates_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeDatabase()
    ids = _seed(db, 205)
    seen: list[str] = []

    async def fake_memory_for(_: Any) -> object:
        return object()

    async def fake_settle(
        _db: Any, _memory: Any, *, user_id: int, account_id: str, trace: Any
    ) -> tuple[None, Any]:
        assert user_id == 1
        if account_id == ids[100]:
            raise RuntimeError("单账户失败")
        seen.append(account_id)
        return None, SimpleNamespace(saved=1, reused=1)

    monkeypatch.setattr("app.memory.decision_store.memory_for", fake_memory_for)
    monkeypatch.setattr("app.memory.service.settle_account", fake_settle)
    monkeypatch.setattr(scheduler, "settle_trace", lambda *args, **kwargs: None)

    totals = asyncio.run(scheduler.settle_all_accounts(SimpleNamespace(db=db)))

    assert totals == {"accounts": 204, "saved": 204, "reused": 204, "failed": 1}
    assert len(seen) == 204
    assert ids[0] in seen and ids[-1] in seen  # keyset 翻页覆盖到第一页与最后一页


def test_settle_all_accounts_skips_when_db_or_memory_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.decision_store import MemoryUnavailable

    async def no_memory(_: Any) -> object:
        raise MemoryUnavailable("memory down")

    assert asyncio.run(scheduler.settle_all_accounts(SimpleNamespace(db=None))) == {
        "accounts": 0,
        "saved": 0,
        "reused": 0,
        "failed": 0,
    }

    monkeypatch.setattr("app.memory.decision_store.memory_for", no_memory)
    assert asyncio.run(scheduler.settle_all_accounts(SimpleNamespace(db=FakeDatabase()))) == {
        "accounts": 0,
        "saved": 0,
        "reused": 0,
        "failed": 0,
    }
