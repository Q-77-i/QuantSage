"""结算调度：跨用户账户分页（D#27）。

真库的 keyset SQL 由集成套件覆盖；离线这条钉住调度器依赖的**分页契约**：
从 None 开始、按 id 升序、页尾作为下一页游标，直到空页——不会像旧的 `LIMIT 500`
那样把尾部账户永久漏掉。
"""

from __future__ import annotations

import asyncio
from datetime import date

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


def test_list_all_paper_accounts_keyset_covers_every_row() -> None:
    db = FakeDatabase()
    expected = _seed(db, 5)

    async def walk() -> list[str]:
        seen: list[str] = []
        after: str | None = None
        while True:
            page = await db.list_all_paper_accounts(after_id=after, limit=2)
            if not page:
                return seen
            seen.extend(row["id"] for row in page)
            after = str(page[-1]["id"])
            if len(page) < 2:
                return seen

    assert asyncio.run(walk()) == sorted(expected)
