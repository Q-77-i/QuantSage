"""S5：多 worker 启动时，后台主实例锁要重试——uvicorn 重启的新进程不能一次抢不到就永远不跑后台。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.tasks import worker


class _Cursor:
    def __init__(self, value: bool) -> None:
        self.value = value

    async def fetchone(self) -> tuple[bool]:
        return (self.value,)


class _Conn:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.closed = False

    async def execute(self, sql: str, params: object = None) -> _Cursor:
        return _Cursor(self.value)

    async def close(self) -> None:
        self.closed = True


def test_single_worker_takes_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn(False)
    connects: list[object] = []

    async def fake_connect(*args: object, **kwargs: object) -> _Conn:
        connects.append(args)
        return conn

    import psycopg

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fake_connect)
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(api_workers=1))
    db = SimpleNamespace(pool=SimpleNamespace(conninfo="postgresql://x"))

    assert asyncio.run(worker._acquire_instance_lock(db)) is None
    assert len(connects) == 1
    assert conn.closed is True


def test_multi_worker_retries_until_lock_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks = iter([False, False, False, True])
    conns: list[_Conn] = []

    async def fake_connect(*args: object, **kwargs: object) -> _Conn:
        conn = _Conn(next(locks))
        conns.append(conn)
        return conn

    import psycopg

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fake_connect)
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(api_workers=2))

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    db = SimpleNamespace(pool=SimpleNamespace(conninfo="postgresql://x"))

    held = asyncio.run(worker._acquire_instance_lock(db))

    assert held is conns[-1]
    assert len(conns) == 4  # 初始 + 3 次重试
    assert all(conn.closed for conn in conns[:3])
    assert conns[3].closed is False
