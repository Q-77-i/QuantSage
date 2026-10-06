"""集成测试公用件。

集成用例打的是**开发库**（`quantsage`），所以建出来的账号、会话必须自己收干净，
不留垃圾——这里放共用的清库出口。
"""

from __future__ import annotations

from app.core.config import get_settings

#: langgraph 自己的三张表，thread_id 是 TEXT
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def purge_rows(emails: list[str], thread_ids: list[str]) -> None:
    """删账号（`chat_threads` 随 users 级联）与残留 checkpoint。

    走同步 psycopg 直连：应用侧的异步池绑在 TestClient 的事件循环上，测试线程碰不得。
    """
    import psycopg

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        if emails:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", (emails,))
        if thread_ids:
            for table in CHECKPOINT_TABLES:
                conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (thread_ids,)
                )
