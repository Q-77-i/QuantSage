"""绩效研报容器与深路径运行记录（M7 / M8）。"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb


class ResearchMixin:
    # ── 绩效研报（M7）───────────────────────────────────────

    async def create_research_report(
        self,
        *,
        report_id: str,
        user_id: int,
        account_id: str | None,
        snapshot: dict[str, Any],
        snapshot_hash: str,
        report: dict[str, Any],
        report_hash: str,
        kind: str = "paper_account_report",
        subject: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """落一份冻结报告。`account_id=None` = 深研报（M8，没有账户主体）。

        账户报告走部分唯一索引 `(account_id, snapshot_hash)`：并发撞上时 `RETURNING`
        0 行，返回 `None`，由调用方回查既有行（`reused=true`）。深研报没有账户主体，
        保持普通插入。
        """
        values = (report_id, user_id, account_id, Jsonb(snapshot), snapshot_hash,
                  Jsonb(report), report_hash, kind,
                  Jsonb(subject) if subject is not None else None)
        if account_id is None:
            row = await self._one(
                "INSERT INTO research_reports "
                "(id, user_id, account_id, snapshot, snapshot_hash, report, report_hash, kind, subject) "
                "VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, created_at",
                values,
            )
            assert row is not None  # 无唯一冲突，RETURNING 必有一行
            return row
        return await self._one(
            "INSERT INTO research_reports "
            "(id, user_id, account_id, snapshot, snapshot_hash, report, report_hash, kind, subject) "
            "VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (account_id, snapshot_hash) WHERE account_id IS NOT NULL "
            "DO NOTHING RETURNING id, created_at",
            values,
        )

    # ── M8 深路径运行记录 ──

    async def create_research_run(
        self,
        *,
        run_id: str,
        user_id: int,
        question: str,
        subject: dict[str, Any],
        status: str = "running",
        events: list[dict[str, Any]] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self._one(
            "INSERT INTO research_runs (id, user_id, question, subject, status, events, budget) "
            "VALUES (%s::uuid, %s, %s, %s, %s, %s, %s) RETURNING id, created_at",
            (run_id, user_id, question, Jsonb(subject), status,
             Jsonb(events or []), Jsonb(budget or {})),
        )
        assert row is not None
        return row

    async def update_research_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        events: list[dict[str, Any]] | None = None,
        budget: dict[str, Any] | None = None,
        report_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        """按需更新（只写传了的列）。返回是否命中一行。"""
        sets: list[str] = ["updated_at = now()"]
        params: list[Any] = []
        if status is not None:
            sets.append("status = %s")
            params.append(status)
        if events is not None:
            sets.append("events = %s")
            params.append(Jsonb(events))
        if budget is not None:
            sets.append("budget = %s")
            params.append(Jsonb(budget))
        if report_id is not None:
            sets.append("report_id = %s::uuid")
            params.append(report_id)
        if error is not None:
            sets.append("error = %s")
            params.append(error)
        row = await self._one(
            f"UPDATE research_runs SET {', '.join(sets)} WHERE id = %s::uuid RETURNING id",
            (*params, run_id),
        )
        return row is not None

    async def sweep_interrupted_research_runs(self) -> int:
        """启动扫尾：本实例遗留的 `running` 研究运行标 `failed/interrupted`。

        只动 `running`；`awaiting_approval` 是合法暂停（等人工 resume），不能扫成失败。
        由后台主实例在启动时调用一次（与 task worker 扫尾同一把锁，避免多 worker 互踩）。
        """
        rows = await self._all(
            "UPDATE research_runs SET status = 'failed', error = %s, updated_at = now() "
            "WHERE status = 'running' RETURNING id",
            ("服务重启中断（interrupted）",),
        )
        return len(rows)

    async def get_research_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        return await self._one(
            "SELECT id, question, subject, status, events, budget, report_id, error, "
            "created_at, updated_at FROM research_runs WHERE user_id = %s AND id = %s::uuid",
            (user_id, run_id),
        )

    async def claim_research_resume(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        """原子地把 `awaiting_approval` 抢成 `running`；返回 None = 已被别人抢走。

        两个并发的 resume 会双跑同一张图的同一 checkpoint（事件交错、双倍 LLM 成本）。
        条件更新只放行一个——与 paper 的乐观推进同一手法，不靠进程内锁。
        """
        return await self._one(
            "UPDATE research_runs SET status = 'running', updated_at = now() "
            "WHERE id = %s::uuid AND user_id = %s AND status = 'awaiting_approval' "
            "RETURNING id, question, subject, status, events, budget, report_id, error, "
            "created_at, updated_at",
            (run_id, user_id),
        )

    async def list_research_runs(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """摘要列表（不带事件）——事件动辄上百条，列表页不需要。"""
        return await self._all(
            "SELECT id, question, subject, status, report_id, error, created_at, updated_at "
            "FROM research_runs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )

    async def find_research_report(
        self, user_id: int, account_id: str, snapshot_hash: str
    ) -> dict[str, Any] | None:
        """幂等复用：同一账户同一快照已有的那份（分享链接因此永远同一份）。"""
        return await self._one(
            "SELECT id, created_at, snapshot_hash, report_hash, report, share_token "
            "FROM research_reports "
            "WHERE user_id = %s AND account_id = %s::uuid AND snapshot_hash = %s "
            "ORDER BY created_at LIMIT 1",
            (user_id, account_id, snapshot_hash),
        )

    async def get_research_report(self, user_id: int, report_id: str) -> dict[str, Any] | None:
        return await self._one(
            # M8：深研报与账户报告同表，`kind` / `subject` 必须带出来（区分两类报告）
            "SELECT id, created_at, account_id, snapshot, snapshot_hash, report, report_hash, "
            "share_token, shared_at, kind, subject FROM research_reports "
            "WHERE user_id = %s AND id = %s::uuid",
            (user_id, report_id),
        )

    async def list_research_reports(self, user_id: int, account_id: str) -> list[dict[str, Any]]:
        """摘要列表（不带正文）——`/paper` 页的「已出研报」入口状态用它。"""
        return await self._all(
            "SELECT id, created_at, snapshot_hash, report_hash, share_token "
            "FROM research_reports WHERE user_id = %s AND account_id = %s::uuid "
            "ORDER BY created_at DESC",
            (user_id, account_id),
        )

    async def set_report_share(
        self, report_id: str, user_id: int, *, token: str | None
    ) -> dict[str, Any] | None:
        """生成（传 token）或撤销（传 None）分享。命中 0 行 = 不存在或非本人 → None。

        两个 `%s` 都要**显式转型**：`CASE WHEN %s IS NULL` 里的参数没有类型上下文，
        Postgres 会以 `IndeterminateDatatype` 拒收（真库实测，内存替身照不出来）。
        """
        return await self._one(
            "UPDATE research_reports SET share_token = %s::text, "
            "shared_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END "
            "WHERE user_id = %s AND id = %s::uuid "
            "RETURNING id, share_token, shared_at",
            (token, token, user_id, report_id),
        )

    async def get_report_by_id(self, report_id: str) -> dict[str, Any] | None:
        """按 id 取（**不带 user_id 过滤**）：调用方是**凭令牌**进来的打印页取数口——
        与 `get_report_by_token` 同一条理由（令牌本身就是凭据），端点层负责只吐正文。"""
        return await self._one(
            "SELECT id, account_id, snapshot, snapshot_hash, report, report_hash, "
            "shared_at, created_at FROM research_reports WHERE id = %s::uuid",
            (report_id,),
        )

    async def get_report_by_token(self, token: str) -> dict[str, Any] | None:
        """公开只读：**凭 token 取冻结产物正文**。本方法不做归属过滤（token 本身就是凭据），
        端点层负责只取正文、不带任何用户身份字段（`user_id` 留在行里不出接口）。"""
        return await self._one(
            "SELECT id, account_id, snapshot, snapshot_hash, report, report_hash, "
            "shared_at, created_at FROM research_reports WHERE share_token = %s",
            (token,),
        )
