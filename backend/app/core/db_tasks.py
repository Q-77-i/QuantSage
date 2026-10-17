"""异步任务与用户配额（P3-E1a / E2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb


def like_pattern(text: str) -> str:
    """把用户输入变成 `ILIKE` 模式（admin 视图的邮箱搜索）。

    转义 `\\` / `%` / `_`——不转的话搜「100%」会把 `%` 当通配符，搜 `a_b` 会命中 `aXb`。
    **空串给 `%%`（匹配全部）**而不是另写一条无过滤的 SQL：`email` 是 NOT NULL，
    一条都不会漏，而两条 SQL 迟早有一处忘了同步。
    """
    escaped = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


#: admin 用量视图的**行定义**（含三读数）与**过滤条件**——列表与计数**共用同一份**。
#: 分两处写过一次就会漂：`total` 与 `rows` 用不同的 WHERE，前端的「共 N 个账号」
#: 与实际能翻到的行数对不上，而且**不会报错**。
#: 两个计数列是**区间量**（E2c 追加：今日 / 近 7 天 / 近 30 天），不是「今日」专用——
#: 故列名叫 `agent_calls` / `rejected` 而非 `*_today`（名字得跟得住口径）。
#: `active_tasks` 是 **gauge**（此刻在飞几个），**不随时间窗变**。
#: 占位符顺序：`_USAGE_SELECT` 四个（period_key 起 / 止 / since / 邮箱模式），
#: `_USAGE_FILTER` 三个（role ×2 / 只看有读数）。
_USAGE_SELECT = (
    "SELECT u.id, u.email, u.role, "
    "  (SELECT count(*) FROM tasks t WHERE t.user_id = u.id "
    "     AND t.status IN ('queued', 'running')) AS active_tasks, "
    "  COALESCE(c.used, 0) AS agent_calls, "
    "  COALESCE(r.rejected, 0) AS rejected "
    "FROM users u "
    "LEFT JOIN (SELECT user_id, sum(used) AS used FROM usage_counters "
    "  WHERE metric = 'agent_calls' AND period_key BETWEEN %s AND %s "
    "  GROUP BY user_id) c ON c.user_id = u.id "
    "LEFT JOIN (SELECT user_id, count(*) AS rejected FROM quota_rejections "
    "  WHERE at >= %s GROUP BY user_id) r ON r.user_id = u.id "
    "WHERE u.email ILIKE %s"
)
#: 别名只能在**外层**用（SQL 的 WHERE 看不见 SELECT 的别名），故过滤包在派生表外面
_USAGE_FILTER = (
    "(%s = '' OR t.role = %s) "
    "AND (NOT %s OR t.active_tasks > 0 OR t.agent_calls > 0 OR t.rejected > 0)"
)

#: 可排序列的白名单：**请求里的键 → 派生表里的列**。白名单而不是把参数拼进 SQL——
#: `ORDER BY` 拼的是**标识符**，占位符在那儿会被当成常量（`ORDER BY %s` 排的是字面量），
#: 所以只能查表；表里没有的键由端点用 `Query(pattern=...)` 挡在 422。
_USAGE_SORTS = {
    "id": "t.id",
    "email": "t.email",
    "active_tasks": "t.active_tasks",
    "agent_calls": "t.agent_calls",
    "rejected": "t.rejected",
}

#: 任务行的对外列。`user_id` 留在行里**不出接口**（与既有约定一致：归属只作过滤条件）。
_TASK_COLUMNS = (
    "id, kind, status, request, progress, result, error, created_at, started_at, finished_at"
)


class TasksMixin:
    # ── 异步任务（P3-E1a）────────────────────────────────────

    async def create_task(
        self,
        task_id: str,
        user_id: int,
        kind: str,
        request: dict[str, Any],
        *,
        total: int = 1,
        idempotency_key: str | None = None,
        active_limit: int | None = None,
    ) -> dict[str, Any] | None:
        """建一条 `queued` 任务。`total` 是格数——单次回测恒 1（E1b 的网格是展开后的格数）。

        **带幂等键时返回既有的那一行**（E4b / L7）：`ON CONFLICT DO NOTHING` 撞上就回查，
        调用方凭 `row["id"] != task_id` 判断这是不是一次重放。冲突判定交给唯一索引而不是
        「先查后插」——两次调用之间的窗口正是要防的那个窗口。

        **在飞任务配额也在这一条语句里判**（`active_limit` 非 None 时）：先取
        `pg_advisory_xact_lock(user)` 再 `INSERT … SELECT … WHERE count < limit`。
        先 `count` 再 `insert` 是两次语句，并发提交会一起越过上限；计数与插入必须在
        同一事务里，且用 advisory lock 把同一用户的并发提交串起来（READ COMMITTED 下
        后一条语句能看到前一条已提交的行）。

        返回 `None` = 配额已满且不是幂等重放（调用方翻 429）；其它情况返回任务行。
        """
        progress = Jsonb({"done": 0, "total": total})
        async with self.pool.connection() as conn, conn.transaction():
            if active_limit is not None:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"active_tasks:{user_id}",),
                )
                cursor = await conn.execute(
                    "INSERT INTO tasks "
                    "(id, user_id, kind, status, request, progress, idempotency_key) "
                    "SELECT %s::uuid, %s, %s, 'queued', %s, %s, %s "
                    "WHERE (SELECT count(*) FROM tasks WHERE user_id = %s "
                    "       AND status IN ('queued', 'running')) < %s "
                    "ON CONFLICT (user_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL DO NOTHING "
                    f"RETURNING {_TASK_COLUMNS}",
                    (
                        task_id,
                        user_id,
                        kind,
                        Jsonb(request),
                        progress,
                        idempotency_key,
                        user_id,
                        active_limit,
                    ),
                )
            else:
                cursor = await conn.execute(
                    "INSERT INTO tasks "
                    "(id, user_id, kind, status, request, progress, idempotency_key) "
                    "VALUES (%s::uuid, %s, %s, 'queued', %s, %s, %s) "
                    "ON CONFLICT (user_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL DO NOTHING "
                    f"RETURNING {_TASK_COLUMNS}",
                    (task_id, user_id, kind, Jsonb(request), progress, idempotency_key),
                )
            row = await cursor.fetchone()
            if row is not None:
                return row
            if idempotency_key is None:
                return None
            # 幂等重放优先于配额：同一个键已经代表过一件事，不该被额度挡回去
            cursor = await conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE user_id = %s AND idempotency_key = %s",
                (user_id, idempotency_key),
            )
            return await cursor.fetchone()

    async def task_by_idempotency_key(self, user_id: int, key: str) -> dict[str, Any] | None:
        """按幂等键回查（E4b / L7）。闸门**之前**的短路用它，重复提交不再走一遍闸门。"""
        return await self._one(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE user_id = %s AND idempotency_key = %s",
            (user_id, key),
        )

    async def get_task(self, user_id: int, task_id: str) -> dict[str, Any] | None:
        """越权与不存在同为 None（调用方一律翻 404，与既有矩阵同姿态）。"""
        return await self._one(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = %s::uuid AND user_id = %s",
            (task_id, user_id),
        )

    async def list_tasks(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """倒序摘要。**不带整份 `request`**（用户策略源码在里面），只抽列表要用的两列——
        与 `list_backtest_runs` 同一个理由：列表不为每行拖几 KB 回来。"""
        return await self._all(
            "SELECT id, kind, status, progress, result, error, created_at, started_at, "
            "finished_at, request->>'symbol' AS symbol, request->>'strategy' AS strategy "
            "FROM tasks WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )

    async def claim_next_task(self) -> dict[str, Any] | None:
        """取排队最久的一件并**原子地**置为 `running`。没有可取的返 None。

        「选 + 占」写在**一条语句**里（子查询与更新同属一条语句），所以两个 worker 同时
        取也只会有一个拿到行——本仓是单 worker，这条性质是白拿的保险，不是为了多 worker。
        """
        return await self._one(
            "UPDATE tasks SET status = 'running', started_at = now(), updated_at = now() "
            "WHERE id = (SELECT id FROM tasks WHERE status = 'queued' "
            "            ORDER BY created_at, id LIMIT 1) "
            # 取件**多带 `user_id`**：执行体要拿它落 `backtest_runs` 的归属。
            # 对外列不含它是给端点的（归属只作过滤条件，不出接口）——两者的用途不同
            f"RETURNING user_id, {_TASK_COLUMNS}",
            (),
        )

    async def update_task_progress(self, task_id: str, *, done: int, total: int) -> None:
        """进度。E1a 的单次回测只在收尾置 1/1（E1b 的网格逐格推）。"""
        await self._exec(
            "UPDATE tasks SET progress = %s, updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running'",
            (Jsonb({"done": done, "total": total}), task_id),
        )

    async def finish_task(self, task_id: str, *, result: dict[str, Any]) -> None:
        """标完成。`status = 'running'` 是条件：被扫尾标成 `failed` 的行不该被迟到的完成覆盖。"""
        await self._exec(
            "UPDATE tasks SET status = 'done', result = %s, finished_at = now(), "
            "updated_at = now() WHERE id = %s::uuid AND status = 'running'",
            (Jsonb(result), task_id),
        )

    async def fail_task(self, task_id: str, *, error: dict[str, Any]) -> None:
        """标失败。**不落 `backtest_runs`**——与今天异常路径不留半条记录的语义一致。"""
        await self._exec(
            "UPDATE tasks SET status = 'failed', error = %s, finished_at = now(), "
            "updated_at = now() WHERE id = %s::uuid AND status = 'running'",
            (Jsonb(error), task_id),
        )

    async def finish_task_cancelled(self, task_id: str) -> None:
        """协作式中止收尾：`running` → `cancelled`。

        与 `finish_task` 分开是为了让「中止」在库里有自己的终态（`result` 为空——
        **中止的结果不落库**，见 SPEC §2.8）。条件同样是 `status='running'`。
        """
        await self._exec(
            "UPDATE tasks SET status = 'cancelled', finished_at = now(), updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running'",
            (task_id,),
        )

    async def cancel_task(self, user_id: int, task_id: str) -> dict[str, Any] | None:
        """取消：**只对 `queued` 生效**（条件更新），返回当前行。不存在/非本人返 None。

        调用方据返回的 `status` 判 409：已在跑的单次回测中止不了（子线程 + 沙箱子进程），
        如实拒绝而不是假装能停。与 worker 之间无需加锁：`cancel` 只改 `queued` 行、
        `claim_next_task` 只选 `queued` 行，两条语句各自原子，谁先命中谁生效。
        """
        cancelled = await self._one(
            "UPDATE tasks SET status = 'cancelled', finished_at = now(), updated_at = now() "
            "WHERE id = %s::uuid AND user_id = %s AND status = 'queued' "
            f"RETURNING {_TASK_COLUMNS}",
            (task_id, user_id),
        )
        if cancelled is not None:
            return cancelled
        return await self.get_task(user_id, task_id)

    async def sweep_interrupted_tasks(self) -> int:
        """启动扫尾：上一条进程留下的 `running` 行标为 `failed / interrupted`，返回条数。

        **不自动重跑**（SPEC §2.4）：静默重跑比明说失败更让人困惑；排队中的那些由 worker
        自然接续（表即队列），所以这里只处理 `running`。
        """
        rows = await self._all(
            "UPDATE tasks SET status = 'failed', error = %s, finished_at = now(), "
            "updated_at = now() WHERE status = 'running' RETURNING id",
            # `kind` 与执行失败的 `error_of` 同一套词汇（前端只写一份分档逻辑）
            (Jsonb({"kind": "interrupted", "message": "服务重启中断"}),),
        )
        return len(rows)

    # ── 用户配额（E2，SPEC §3.1）─────────────────────────────

    async def task_status_counts(self) -> dict[str, int]:
        """任务表按状态计数（E7e 队列深度指标，scrape 时现查）。

        与 `count_active_tasks` 同一条理由：任务表是唯一真值，不另立计数器——计数器得在
        五条终结路径上各减一次，漏一处就是永久偏差。
        """
        rows = await self._all(
            "SELECT status, count(*) AS n FROM tasks GROUP BY status", ()
        )
        return {str(row["status"]): int(row["n"]) for row in rows}

    async def count_active_tasks(self, user_id: int) -> int:
        """在飞任务数（gauge）：`queued` + `running` 都算——**排队即占槽位**。

        直查而不另立计数器，是因为计数器得在**终结**时减一，而终结有五条路径（成功 / 失败 /
        取消 / 启动扫尾 / 进程被杀）——漏减一处就是永久占额；任务表才是那个唯一真值。
        """
        row = await self._one(
            "SELECT count(*) AS n FROM tasks WHERE user_id = %s "
            "AND status IN ('queued', 'running')",
            (user_id,),
        )
        return int(row["n"]) if row else 0

    async def bump_usage_counter(
        self, user_id: int, metric: str, period_key: str, limit: int
    ) -> int | None:
        """**原子地**检查并递增（counter）：返回本次序号；已达上限时返回 `None` 且**不计数**。

        「检查 + 递增」必须是一条语句：拆成两句的话，两个并发请求会同时读到 `used = limit - 1`
        然后各写一次，实际放行 `limit + 1` 个。`ON CONFLICT DO UPDATE … WHERE` 里那个 `WHERE`
        挡住的正是这一次递增——0 行回来即超限（**拒绝不计数**，也就不会「越拒越多」）。

        ⚠ 新行走的是 `INSERT` 分支、**不看 `limit`**（SQL 语义如此）——调用方保证 `limit > 0`
        （`quota.enforce` 在 `limit <= 0` 时整条跳过）。
        """
        row = await self._one(
            "INSERT INTO usage_counters (user_id, metric, period_key, used) "
            "VALUES (%s, %s, %s, 1) "
            "ON CONFLICT (user_id, metric, period_key) DO UPDATE "
            "SET used = usage_counters.used + 1, updated_at = now() "
            "WHERE usage_counters.used < %s "
            "RETURNING used",
            (user_id, metric, period_key, limit),
        )
        return None if row is None else int(row["used"])

    async def admin_usage(
        self,
        period_from: str,
        period_to: str,
        since: datetime,
        *,
        limit: int,
        offset: int,
        q: str = "",
        role: str = "",
        active_only: bool = False,
        sort: str = "id",
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        """admin 配额视图的**一页**（E2b；E2c 加分页、搜索、筛选、排序与时间窗）。

        三列各有来源，别指望一条 join 解决：在飞任务数**直查任务表**（与 gauge 同源，
        否则视图与判定会各说各话），调用数按 `period_key` 区间**求和**，被拒按 `at >= since` 数事件。
        `since` 是时间窗**起点时刻**（+08 的 00:00）——两个计数列的时间轴必须同起点。

        ⚠ 搜索用 `ILIKE '%q%'`，**用不上 `email` 的唯一索引**（B-tree 只吃前缀）——
        本地千级规模顺序扫描无妨，真上量再引 `pg_trgm`（SPEC §3.5 记为边界）。
        """
        # 排序必须**带上 id 兜底**：同值行在两次查询里顺序不定，翻页会看到重复或漏行
        # （`sort="id"` 本身就是那个兜底，不必再缀一次）
        column = _USAGE_SORTS[sort]
        tiebreak = "" if sort == "id" else ", t.id"
        direction = "DESC" if order == "desc" else "ASC"
        return await self._all(
            f"SELECT * FROM ({_USAGE_SELECT}) t WHERE {_USAGE_FILTER} "
            f"ORDER BY {column} {direction}{tiebreak} LIMIT %s OFFSET %s",
            (
                period_from,
                period_to,
                since,
                like_pattern(q),
                role,
                role,
                active_only,
                limit,
                offset,
            ),
        )

    async def admin_usage_total(
        self,
        period_from: str,
        period_to: str,
        since: datetime,
        *,
        q: str = "",
        role: str = "",
        active_only: bool = False,
    ) -> int:
        """过滤后的命中数——分页控件靠它算总页数。

        与上一页**共用 `_USAGE_SELECT` / `_USAGE_FILTER`**：两处各写一份 WHERE 时，
        「共 N 个账号」与实际能翻到的行数会悄悄对不上（且不报错）。
        计数也要 `period_key` / `since`——三读数是派生表里算出来的，计数得先把它建出来。
        """
        row = await self._one(
            f"SELECT count(*) AS n FROM ({_USAGE_SELECT}) t WHERE {_USAGE_FILTER}",
            (period_from, period_to, since, like_pattern(q), role, role, active_only),
        )
        return int(row["n"]) if row else 0

    async def quota_rejections(self, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        """最近的拒绝事件（E2b，新的在前；E2c 加分页）。邮箱随行——读它的只有 admin 面。"""
        return await self._all(
            "SELECT r.id, r.user_id, u.email, r.metric, r.used, r.limit_value, r.at "
            "FROM quota_rejections r JOIN users u ON u.id = r.user_id "
            "ORDER BY r.at DESC, r.id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )

    async def quota_rejections_total(self) -> int:
        row = await self._one("SELECT count(*) AS n FROM quota_rejections", ())
        return int(row["n"]) if row else 0

    async def record_quota_rejection(
        self, user_id: int, metric: str, used: int, limit: int
    ) -> None:
        """记一次拒绝（E2）。**只记不判**：超限与否完全由上面那条语句决定，这一行不参与判定。

        它写在抛 429 **之前**（`quota.enforce`）——写挂了这次拒绝会变成 500，如实记在这里：
        库都不通了，500 也是实情，不做吞异常那层纸。
        """
        await self._exec(
            "INSERT INTO quota_rejections (user_id, metric, used, limit_value) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, metric, used, limit),
        )
