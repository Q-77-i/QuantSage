"""异步任务与用户配额（P3-E1a / E2）。"""

from __future__ import annotations

import asyncio
import random
import time
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

#: 队列 v2：`kind` → 排队优先级（SPEC §3.2）。单 worker 下只影响排队顺序：
#: 交互式单次回测（0.1–2s）先于长网格/批量，避免一件上百格的活儿把回测压在后面。
#: 同优先级之间仍是 `available_at, created_at, id` 的 FIFO。
KIND_PRIORITY = {"backtest": 10}

#: 重排后至少等这么久再取件（SPEC §3.3）——给源侧抖动/瞬时错误一点缓冲。
RETRY_BACKOFF_SECONDS = 5.0

#: 逐格进度的 JSONB 硬上限。当前网格上限 100（`backtest.batch.MAX_CELLS`）；1,000 是
#: 防御未来放宽上限时单行无限增长。
MAX_PROGRESS_CELLS = 1000

#: 取件/重排/释放时把进度归零并丢掉上一次的 `cells`（保留 `total`）。
_PROGRESS_RESET = "jsonb_set(progress - 'cells', '{done}', '0'::jsonb)"

#: 配额锁的等待预算（S8 实验 D）：抢不到锁就**先归还业务池连接**、退避再试。
#: 旧写法在 `pg_advisory_xact_lock` 上阻塞，20 个并发提交会把 8 槽业务池全部占住
#: （实测 30.7% 的请求排队、排队者平均 148ms）；等待不该占用连接。
#: 预算内一直抢不到（极端争用），才退回旧的阻塞等锁——语义不变，只是兜底。
QUOTA_LOCK_BUDGET_SECONDS = 2.0
QUOTA_LOCK_BACKOFF_START_SECONDS = 0.005
QUOTA_LOCK_BACKOFF_MAX_SECONDS = 0.05


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
        max_attempts: int = 2,
    ) -> dict[str, Any] | None:
        """建一条 `queued` 任务。`total` 是格数——单次回测恒 1（E1b 的网格是展开后的格数）。

        `max_attempts`（P4-S2）在**建任务时快照**：之后改配置不影响已在队列里的任务。

        **带幂等键时返回既有的那一行**（E4b / L7）：`ON CONFLICT DO NOTHING` 撞上就回查，
        调用方凭 `row["id"] != task_id` 判断这是不是一次重放。冲突判定交给唯一索引而不是
        「先查后插」——两次调用之间的窗口正是要防的那个窗口。

        **在飞任务配额也在这一条语句里判**（`active_limit` 非 None 时）：用
        `pg_try_advisory_xact_lock(user)` 串行化同一用户的并发提交，抢到后执行
        `INSERT … SELECT … WHERE count < limit`；**抢不到先归还连接**、退避再试
        （S8 实验 D：旧写法在锁上阻塞，20 个并发提交把 8 槽业务池占满）。
        count 与 insert 仍是两次语句，必须在同一事务 + 同一把锁下（READ COMMITTED
        下后一条语句能看到前一条已提交的行）。预算内一直抢不到才退回阻塞等锁。

        返回 `None` = 配额已满且不是幂等重放（调用方翻 429）；其它情况返回任务行。
        """
        progress = Jsonb({"done": 0, "total": total})
        priority = KIND_PRIORITY.get(kind, 0)
        params = (
            task_id,
            user_id,
            kind,
            Jsonb(request),
            progress,
            idempotency_key,
            priority,
            max_attempts,
        )
        if active_limit is None:
            async with self.pool.connection() as conn, conn.transaction():
                row = await self._insert_task(
                    conn, params, user_id=user_id, active_limit=None
                )
                return await self._row_or_replay(conn, row, user_id, idempotency_key)
        return await self._create_task_with_quota(
            params=params,
            user_id=user_id,
            idempotency_key=idempotency_key,
            active_limit=active_limit,
        )

    async def _insert_task(
        self,
        conn: Any,
        params: tuple[Any, ...],
        *,
        user_id: int,
        active_limit: int | None,
    ) -> dict[str, Any] | None:
        """插入任务行；带配额时同一条语句里判 `count(*) < limit`。

        **配额分支必须在持有 `active_tasks:{user_id}` 锁的事务里调用**：count 与 insert
        虽在同一条语句，但 READ COMMITTED 下并发事务仍会各自看到旧计数。
        """
        if active_limit is None:
            cursor = await conn.execute(
                "INSERT INTO tasks "
                "(id, user_id, kind, status, request, progress, idempotency_key, "
                " priority, max_attempts) "
                "VALUES (%s::uuid, %s, %s, 'queued', %s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL DO NOTHING "
                f"RETURNING {_TASK_COLUMNS}",
                params,
            )
        else:
            cursor = await conn.execute(
                "INSERT INTO tasks "
                "(id, user_id, kind, status, request, progress, idempotency_key, "
                " priority, max_attempts) "
                "SELECT %s::uuid, %s, %s, 'queued', %s, %s, %s, %s, %s "
                "WHERE (SELECT count(*) FROM tasks WHERE user_id = %s "
                "       AND status IN ('queued', 'running')) < %s "
                "ON CONFLICT (user_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL DO NOTHING "
                f"RETURNING {_TASK_COLUMNS}",
                (*params, user_id, active_limit),
            )
        return await cursor.fetchone()

    async def _row_or_replay(
        self,
        conn: Any,
        row: dict[str, Any] | None,
        user_id: int,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
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

    async def _create_task_with_quota(
        self,
        *,
        params: tuple[Any, ...],
        user_id: int,
        idempotency_key: str | None,
        active_limit: int,
    ) -> dict[str, Any] | None:
        """带配额的建任务：**try-lock 不阻塞等**，抢不到就归还连接再退避重试。

        `pg_try_advisory_xact_lock` 失败时事务立即结束、连接回到池里——等待期间不占
        业务池槽位（这正是旧写法把 8 槽池占满的原因）。抢到锁后 count+insert 仍在同一
        事务里，配额语义与旧写法逐字一致。
        """
        lock_key = f"active_tasks:{user_id}"
        deadline = time.monotonic() + QUOTA_LOCK_BUDGET_SECONDS
        delay = QUOTA_LOCK_BACKOFF_START_SECONDS
        while True:
            async with self.pool.connection() as conn, conn.transaction():
                cursor = await conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS locked",
                    (lock_key,),
                )
                held = await cursor.fetchone()
                if held and held.get("locked"):
                    row = await self._insert_task(
                        conn, params, user_id=user_id, active_limit=active_limit
                    )
                    return await self._row_or_replay(
                        conn, row, user_id, idempotency_key
                    )
            if time.monotonic() >= deadline:
                break
            # 抖动覆盖整个区间：20 个等待者同时醒来会形成惊群
            await asyncio.sleep(delay * (0.5 + random.random()))
            delay = min(delay * 1.5, QUOTA_LOCK_BACKOFF_MAX_SECONDS)

        # 兜底：预算内一直抢不到（极端争用）→ 退回旧的阻塞等锁。
        # 这条路径才是「占着连接等」的；正常情况下不会走到。
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
            )
            row = await self._insert_task(
                conn, params, user_id=user_id, active_limit=active_limit
            )
            return await self._row_or_replay(conn, row, user_id, idempotency_key)

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

    async def get_task_progress(
        self, user_id: int, task_id: str
    ) -> dict[str, Any] | None:
        """SSE 每 1s 轮询用的轻量视图（S5）。

        **不取 `request`**：里面冻着网格的全部格规格，1s 拉一次是纯浪费；终态回放
        也只需要 `result` / `error`。
        """
        return await self._one(
            "SELECT status, progress, result, error FROM tasks "
            "WHERE id = %s::uuid AND user_id = %s",
            (task_id, user_id),
        )

    async def list_tasks(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """倒序摘要。**不带整份 `request`**（用户策略源码在里面），只抽列表要用的两列——
        与 `list_backtest_runs` 同一个理由：列表不为每行拖几 KB 回来。

        S5 又去掉 `progress.cells`（网格最多 100 格，50 条列表就是几 MB）；列表只要
        `done/total`，逐格进度由详情/SSE 取。
        """
        return await self._all(
            "SELECT id, kind, status, progress - 'cells' AS progress, result, error, "
            "created_at, started_at, finished_at, "
            "request->>'symbol' AS symbol, request->>'strategy' AS strategy "
            "FROM tasks WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )

    async def claim_next_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        kinds: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """取排队最前的一件并**原子地**置为 `running`。没有可取的返 None。

        「选 + 占」用 `FOR UPDATE SKIP LOCKED`：旧写法（外层 `UPDATE ... WHERE id = (子查询)`）
        在两个 worker 并发时拿到的子查询结果是同一行，后到者阻塞后重检仍只比 `id`，
        于是**两个 worker 拿到同一件**（探针 30/30）。`SKIP LOCKED` 让后到者直接跳过。

        `lease_seconds=0` = 租约关闭（`TASK_LEASE_ENABLED=false` 的回滚形态）：
        不写 `lease_expires_at` / `last_heartbeat_at`，只保留 `SKIP LOCKED`。

        `kinds=None` 保持旧行为（任何 kind）；P4-S7 的 worker 池按 kind 传元组，
        让每个协程只取自己负责的 kind。**不是多进程**：仍由持锁进程内的多个协程调用。

        取件**多带** worker 判活用的列（`attempts` / `max_attempts` / `cancel_requested_at`
        / `user_id`），它们都不进对外 `_TASK_COLUMNS`。进度归零：重排后从 0 重新计。
        """
        kinds_arg = list(kinds) if kinds is not None else None
        return await self._one(
            "WITH picked AS ("
            "  SELECT id FROM tasks "
            "  WHERE status = 'queued' AND available_at <= now() "
            "    AND (%s::text[] IS NULL OR kind = ANY(%s)) "
            "  ORDER BY priority DESC, available_at, created_at, id "
            "  LIMIT 1 FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE tasks SET status = 'running', started_at = now(), "
            "  attempts = attempts + 1, worker_id = %s, "
            "  lease_expires_at = CASE WHEN %s > 0 "
            "    THEN now() + make_interval(secs => %s) ELSE NULL END, "
            "  last_heartbeat_at = CASE WHEN %s > 0 THEN now() ELSE NULL END, "
            f"  progress = {_PROGRESS_RESET}, "
            "  updated_at = now() "
            "WHERE id = (SELECT id FROM picked) "
            "RETURNING user_id, cancel_requested_at, attempts, max_attempts, "
            f"{_TASK_COLUMNS}",
            (kinds_arg, kinds_arg, worker_id, lease_seconds, lease_seconds, lease_seconds),
        )

    async def heartbeat_task(
        self, task_id: str, *, worker_id: str, lease_seconds: int
    ) -> bool:
        """续租。返回是否命中——**没命中也别重试**：租约已被 reaper 回收，
        旧 worker 的迟到心跳不能把行抢回来（SPEC §3.3）。"""
        row = await self._one(
            "UPDATE tasks SET last_heartbeat_at = now(), "
            "  lease_expires_at = now() + make_interval(secs => %s), updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running' AND worker_id = %s "
            "RETURNING id",
            (lease_seconds, task_id, worker_id),
        )
        return row is not None

    async def requeue_task(
        self, task_id: str, *, worker_id: str, backoff_seconds: float
    ) -> bool:
        """可重排错误：`running` → `queued`，清租约，返回是否重排。

        条件带 `worker_id`：任务已被 reaper 回收（并可能被新 worker 取走）时，
        旧 worker 不得把它按回 `queued`。`attempts < max_attempts` 是数据库层兜底——
        调用方已判过一次，这里防的是并发下 `attempts` 被别处推过上限。
        **不写 `error`**：还没到终态。
        """
        row = await self._one(
            "UPDATE tasks SET status = 'queued', "
            "  available_at = now() + make_interval(secs => %s), "
            "  worker_id = NULL, lease_expires_at = NULL, last_heartbeat_at = NULL, "
            f"  progress = {_PROGRESS_RESET}, "
            "  updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running' AND worker_id = %s "
            "  AND attempts < max_attempts "
            "RETURNING id",
            (backoff_seconds, task_id, worker_id),
        )
        return row is not None

    async def release_task(self, task_id: str, *, worker_id: str) -> str | None:
        """优雅停机：释放本 worker 持有的租约（S4 §5.4），不把任务算作一次失败。

        - 未请求取消 → `queued`、`attempts = greatest(attempts-1, 0)`、清租约、进度归零；
          `attempts-1` 是刻意的：进程正常退出不是执行失败，不该消耗重排次数。
        - 已请求取消 → `cancelled` + `finished_at`（取消是权威，不重排）。
        - `worker_id` 栅栏：行已被 reaper 回收并重新取件时返回 `None`，不误放别人的租约。

        与 `requeue_task` 的区别：那个给「执行期瞬时错误」用（保留 attempts、带退避）；
        这个只给停机收尾用（attempts 回退、立即排队）。
        """
        row = await self._one(
            "UPDATE tasks SET "
            "  status = CASE WHEN cancel_requested_at IS NOT NULL "
            "    THEN 'cancelled' ELSE 'queued' END, "
            "  attempts = CASE WHEN cancel_requested_at IS NULL "
            "    THEN greatest(attempts - 1, 0) ELSE attempts END, "
            "  available_at = CASE WHEN cancel_requested_at IS NULL "
            "    THEN now() ELSE available_at END, "
            "  finished_at = CASE WHEN cancel_requested_at IS NOT NULL "
            "    THEN now() ELSE finished_at END, "
            "  worker_id = NULL, lease_expires_at = NULL, last_heartbeat_at = NULL, "
            "  progress = CASE WHEN cancel_requested_at IS NULL "
            f"    THEN {_PROGRESS_RESET} ELSE progress END, "
            "  updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running' AND worker_id = %s "
            "RETURNING status",
            (task_id, worker_id),
        )
        return str(row["status"]) if row else None

    async def reap_running_tasks(
        self, *, backoff_seconds: float, expired_only: bool = True
    ) -> dict[str, int]:
        """回收 `running` 行（SPEC §3.3）。返回各去向的条数（`queued`/`failed`/`cancelled`）。

        两种模式：**周期 reaper**（`expired_only=True`，只看过期租约；worker 活着时的安全网）
        与**启动扫尾**（`expired_only=False`，已持 advisory lock，证明没有第二个 worker，
        全部 `running` 都是孤儿——kill -9 之后靠它在秒级重排）。

        去向：已请求取消 → `cancelled`；`attempts < max_attempts` → `queued`（不写 error，
        还没终态）；否则 `failed/lease_expired`。三个 CASE 在**同一条语句**里，没有中间态。
        """
        where = "status = 'running'"
        if expired_only:
            where += " AND lease_expires_at IS NOT NULL AND lease_expires_at < now()"
        rows = await self._all(
            "UPDATE tasks SET "
            "  status = CASE "
            "    WHEN cancel_requested_at IS NOT NULL THEN 'cancelled' "
            "    WHEN attempts < max_attempts THEN 'queued' "
            "    ELSE 'failed' END, "
            "  available_at = CASE "
            "    WHEN cancel_requested_at IS NULL AND attempts < max_attempts "
            "    THEN now() + make_interval(secs => %s) ELSE available_at END, "
            "  error = CASE "
            "    WHEN cancel_requested_at IS NULL AND attempts >= max_attempts "
            "    THEN jsonb_build_object('kind', 'lease_expired', "
            "      'message', '执行中断且已达最大尝试次数', 'attempts', attempts) "
            "    ELSE error END, "
            "  finished_at = CASE "
            "    WHEN cancel_requested_at IS NOT NULL OR attempts >= max_attempts "
            "    THEN now() ELSE finished_at END, "
            "  worker_id = NULL, lease_expires_at = NULL, last_heartbeat_at = NULL, "
            "  progress = CASE "
            "    WHEN cancel_requested_at IS NULL AND attempts < max_attempts "
            f"    THEN {_PROGRESS_RESET} "
            "    ELSE progress END, "
            "  updated_at = now() "
            f"WHERE {where} "
            "RETURNING status",
            (backoff_seconds,),
        )
        counts: dict[str, int] = {}
        for row in rows:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
        return counts

    async def update_task_progress(
        self, task_id: str, *, worker_id: str, done: int, total: int
    ) -> None:
        """进度。E1a 的单次回测只在收尾置 1/1（E1b 的网格逐格推）。

        `worker_id` 栅栏：任务被回收并重新取件后，旧 worker 的进度不能盖住新 worker 的。
        """
        await self._exec(
            "UPDATE tasks SET progress = progress || %s::jsonb, updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running' AND worker_id = %s",
            (Jsonb({"done": done, "total": total}), task_id, worker_id),
        )

    async def append_task_cell(
        self,
        task_id: str,
        *,
        worker_id: str,
        done: int,
        total: int,
        cell: dict[str, Any],
    ) -> None:
        """网格/批量逐格进度（S5）：`done/total` 与 `cells` 一条 UPDATE 写入。

        `cells` 是 JSONB 数组；`MAX_CELLS=100`，上限 1,000 是防御性硬顶（未来放宽
        网格上限时不会让单行无限长）。`worker_id` 栅栏与 `update_task_progress` 同源。
        """
        await self._exec(
            "UPDATE tasks SET progress = jsonb_build_object("
            "  'done', %s, 'total', %s, "
            "  'cells', CASE "
            f"    WHEN jsonb_array_length(COALESCE(progress->'cells', '[]'::jsonb)) < {MAX_PROGRESS_CELLS} "
            "    THEN COALESCE(progress->'cells', '[]'::jsonb) || %s::jsonb "
            "    ELSE COALESCE(progress->'cells', '[]'::jsonb) END"
            "), updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running' AND worker_id = %s",
            (done, total, Jsonb(cell), task_id, worker_id),
        )

    async def finish_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        result: dict[str, Any],
        progress_done: int | None = None,
        progress_total: int | None = None,
    ) -> None:
        """标完成。`status='running' AND worker_id=%s` 是栅栏：被回收标成 `failed` 的行、
        或被新 worker 重新取件的行，都不该被迟到的完成覆盖（SPEC §3.3）。

        P4-S9：给出 `progress_done` 时把「进度满格」并进同一次 UPDATE（`progress || {...}`
        保留 `cells`），成功路径因此少一次独立 UPDATE；不给则保持旧的纯终态写法。
        """
        if progress_done is None:
            await self._exec(
                "UPDATE tasks SET status = 'done', result = %s, finished_at = now(), "
                "updated_at = now() WHERE id = %s::uuid AND status = 'running' "
                "AND worker_id = %s",
                (Jsonb(result), task_id, worker_id),
            )
            return
        total = progress_done if progress_total is None else progress_total
        await self._exec(
            "UPDATE tasks SET status = 'done', result = %s, finished_at = now(), "
            "updated_at = now(), "
            "progress = progress || jsonb_build_object('done', %s::int, 'total', %s::int) "
            "WHERE id = %s::uuid AND status = 'running' AND worker_id = %s",
            (Jsonb(result), progress_done, total, task_id, worker_id),
        )

    async def fail_task(
        self, task_id: str, *, worker_id: str, error: dict[str, Any]
    ) -> None:
        """标失败。**不落 `backtest_runs`**——与今天异常路径不留半条记录的语义一致。
        栅栏同 `finish_task`。"""
        await self._exec(
            "UPDATE tasks SET status = 'failed', error = %s, finished_at = now(), "
            "updated_at = now() WHERE id = %s::uuid AND status = 'running' "
            "AND worker_id = %s",
            (Jsonb(error), task_id, worker_id),
        )

    async def finish_task_cancelled(self, task_id: str, *, worker_id: str) -> None:
        """协作式中止收尾：`running` → `cancelled`。

        与 `finish_task` 分开是为了让「中止」在库里有自己的终态（`result` 为空——
        **中止的结果不落库**，见 SPEC §2.8）。栅栏同 `finish_task`。
        """
        await self._exec(
            "UPDATE tasks SET status = 'cancelled', finished_at = now(), updated_at = now() "
            "WHERE id = %s::uuid AND status = 'running' AND worker_id = %s",
            (task_id, worker_id),
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

    async def request_task_cancel(self, user_id: int, task_id: str) -> dict[str, Any] | None:
        """给 running 任务写取消标志（收尾项 6）。返回当前行；不存在/非本人返回 None。

        与 `cancel_task` 分开：`queued` 的取消是状态条件更新，`running` 的协作式中止
        只写 `cancel_requested_at`，由 worker 在格边界收尾。条件里带 `status='running'`：
        已经结束的行不该被迟到的取消请求改回任何状态。
        """
        row = await self._one(
            "UPDATE tasks SET cancel_requested_at = now(), updated_at = now() "
            "WHERE id = %s::uuid AND user_id = %s AND status = 'running' "
            f"RETURNING {_TASK_COLUMNS}",
            (task_id, user_id),
        )
        if row is not None:
            return row
        return await self.get_task(user_id, task_id)

    async def task_cancel_requested(self, task_id: str) -> bool:
        """worker 轮询用：这一行是否已收到取消请求。查不到按未请求处理。"""
        row = await self._one(
            "SELECT (cancel_requested_at IS NOT NULL) AS requested "
            "FROM tasks WHERE id = %s::uuid",
            (task_id,),
        )
        return bool(row and row["requested"])

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
