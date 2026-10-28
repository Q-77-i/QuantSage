"""S5：两个 API worker 同时启动时，`init_schema` 必须串行（advisory xact lock）。"""

from __future__ import annotations

import asyncio

from app.core.db import init_schema


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))

    def transaction(self) -> _Tx:
        return _Tx()


class _Tx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Ctx:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def connection(self) -> _Ctx:
        return _Ctx(self.conn)


def test_init_schema_takes_the_schema_lock_before_any_ddl() -> None:
    conn = _Conn()
    asyncio.run(init_schema(_Pool(conn)))  # type: ignore[arg-type]

    assert conn.calls, "init_schema 至少要执行建表语句"
    first_sql, first_params = conn.calls[0]
    assert "pg_advisory_xact_lock" in first_sql
    assert isinstance(first_params, tuple) and len(first_params) == 1
