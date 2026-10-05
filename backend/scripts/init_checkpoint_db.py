"""建 LangGraph checkpointer 表（幂等，可反复执行）。

用法：cd backend && uv run python scripts/init_checkpoint_db.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.core.checkpoint import setup_checkpoint_tables  # noqa: E402
from app.core.config import get_settings  # noqa: E402


async def main() -> int:
    settings = get_settings()
    print(f"目标库：{settings.postgres_summary}")
    try:
        await setup_checkpoint_tables(settings.postgres_dsn)
    except Exception as exc:  # noqa: BLE001 —— 转成可读提示
        print(f"建表失败：{type(exc).__name__}: {exc}")
        print("提示：确认容器已起 —— docker compose up -d --wait")
        return 1

    async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "select tablename from pg_tables"
            " where schemaname = 'public' and tablename like 'checkpoint%'"
            " order by tablename"
        )
        tables = [row[0] for row in await cursor.fetchall()]

    print(f"checkpoint 表 {len(tables)} 张：" + ", ".join(tables))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
