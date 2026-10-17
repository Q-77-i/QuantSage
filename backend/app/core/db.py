"""业务库：自建表的连接池、幂等建表与数据访问。

与 checkpointer 的池**分开**：`checkpoints` 系列表由 `langgraph-checkpoint-postgres`
自己的迁移管理（`saver.setup()`），本模块只管我们自己的表，两边互不牵扯。

三条约定：
  * 建表走幂等 DDL（`CREATE TABLE IF NOT EXISTS`），项目不引 Alembic——表结构变更一律写在这里，
    启动时执行；改列要另起迁移语句，不能只改这里的 DDL 就当生效（`IF NOT EXISTS` 不会改已存在的表）；
  * 连接 `autocommit=True` + `dict_row`（与 checkpointer 池同口径），单语句操作不需要显式事务；
  * 所有读写都带 `user_id`（见 M1 归属校验），命中 0 行由调用方翻成 404。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

#: 自选股的回落分组：删组时组内标的回落到它，它自己不可重命名 / 删除。
#: 建表默认值与它同源（下面的 DDL 由它拼出），避免两处各写一份字面量而漂移。
DEFAULT_GROUP = "默认分组"

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

# 幂等建表：按依赖顺序，users 在前（其余表挂它的外键）
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            BIGSERIAL PRIMARY KEY,
        email         TEXT        NOT NULL UNIQUE,
        password_hash TEXT        NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        token_version INT         NOT NULL DEFAULT 0
    )
    """,
    # E8a：老库补列（`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，同 M4c 的约定）。
    # 语义：token 里的 `ver` 与它不等即视为已撤销——登出/改密把它加一。
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INT NOT NULL DEFAULT 0",
    # 角色（E2b）：默认普通用户，提权走 `scripts/grant_admin.py`。**不进 JWT**——
    # `require_user` 本来就查整行，角色变更立即生效（理由写在 `core/auth.py::require_admin`）。
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'",
    """
    CREATE TABLE IF NOT EXISTS chat_threads (
        thread_id      UUID PRIMARY KEY,
        user_id        BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS chat_threads_user_active_idx "
    "ON chat_threads (user_id, last_active_at DESC)",
    f"""
    CREATE TABLE IF NOT EXISTS watchlist (
        id          BIGSERIAL PRIMARY KEY,
        user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        symbol      TEXT        NOT NULL,
        group_name  TEXT        NOT NULL DEFAULT '{DEFAULT_GROUP}',
        added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        added_price NUMERIC(18, 4),
        UNIQUE (user_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id         UUID PRIMARY KEY,
        user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        request    JSONB       NOT NULL,
        report     JSONB       NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS backtest_runs_user_created_idx "
    "ON backtest_runs (user_id, created_at DESC)",
    # M4c：用户策略。`params` 存最近一次保存的参数值（工作台回填用）
    """
    CREATE TABLE IF NOT EXISTS strategies (
        id         UUID PRIMARY KEY,
        user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name       TEXT        NOT NULL,
        code       TEXT        NOT NULL,
        params     JSONB       NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS strategies_user_updated_idx "
    "ON strategies (user_id, updated_at DESC)",
    # M4c：`backtest_runs` 增列走**另起的迁移语句**——`CREATE TABLE IF NOT EXISTS` 对已存在的表
    # 什么都不做，只改上面的 DDL 是不会生效的（见模块 docstring 第二条约定）。
    # `strategy_id` **不设外键**：策略删了记录仍在（报告 JSONB 自足，见 SPEC §5 M4c）。
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS strategy_id UUID",
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS code_sha256 TEXT",
    # E7d：实验可复现的数据维度身份——**窗口内分片**的指纹（`app/report/snapshot.py::window_digest`）。
    # 旧记录为 NULL（不做回填：那要重算数据、不可信），前端按缺失显示。
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS data_digest TEXT",
    # M5b：批量 / 网格的**汇总**（`request` + `summary`，**不落完整报告**）。
    # `summary.cells` 只有每格的 `params → metrics` 与收益矩，没有净值曲线与逐笔——
    # 一百格 × 上千点净值曲线会把这张表撑成 MB 级 JSONB，而要看某一格的完整报告，
    # 从热力图点进去重跑一次即可（单格 70–350ms）。
    """
    CREATE TABLE IF NOT EXISTS optimization_runs (
        id         UUID PRIMARY KEY,
        user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        request    JSONB NOT NULL,
        summary    JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS optimization_runs_user_created_idx "
    "ON optimization_runs (user_id, created_at DESC)",
    # ── M6 模拟盘：四张表（账户 / 持仓 / 决策 / 净值）────────────────
    # 金额一律 NUMERIC(18,4)：算钱不用二进制浮点（`round(2.675, 2)` 给 2.67 那个教训）。
    # `config` 存创建时的全部参数（池子/策略/参数/区间/费用），落库即契约——读回时
    # 由 `paper.store.config_from_payload` 还原，重放的全部输入都从这一列来。
    """
    CREATE TABLE IF NOT EXISTS paper_accounts (
        id           UUID PRIMARY KEY,
        user_id      BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name         TEXT        NOT NULL,
        config       JSONB       NOT NULL,
        status       TEXT        NOT NULL DEFAULT 'active',
        cash         NUMERIC(18, 4) NOT NULL,
        realized_pnl NUMERIC(18, 4) NOT NULL DEFAULT 0,
        as_of        DATE        NOT NULL,
        rules        JSONB       NOT NULL DEFAULT '{}'::jsonb,
        risk_state   JSONB       NOT NULL DEFAULT '{}'::jsonb,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS paper_accounts_user_created_idx "
    "ON paper_accounts (user_id, created_at DESC)",
    # E5：风控快照是派生量，跟着每次推进重写；旧库补列走另起的迁移语句。
    "ALTER TABLE paper_accounts ADD COLUMN IF NOT EXISTS risk_state "
    "JSONB NOT NULL DEFAULT '{}'::jsonb",
    """
    CREATE TABLE IF NOT EXISTS paper_positions (
        account_id   UUID NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
        symbol       TEXT NOT NULL,
        shares       INTEGER NOT NULL,
        entry_price  NUMERIC(18, 4) NOT NULL,
        entry_fees   NUMERIC(18, 4) NOT NULL DEFAULT 0,
        entry_date   DATE,
        entry_reason TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (account_id, symbol)
    )
    """,
    # 决策表**同时是订单表与日志表**（决策与成交 1:1，不拆两张）——M7 的到期结算反思只读它。
    # `UNIQUE (account_id, trade_date, symbol, side)` 是 id 之外的又一道保险：id 本就是
    # 这三者加账户的函数（`paper.types.decision_id`），撞键说明有人手工塞了行。
    """
    CREATE TABLE IF NOT EXISTS paper_decisions (
        id             UUID PRIMARY KEY,
        account_id     UUID NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
        trade_date     DATE NOT NULL,
        symbol         TEXT NOT NULL,
        side           TEXT NOT NULL,
        est_qty        INTEGER NOT NULL,
        est_price      NUMERIC(18, 4) NOT NULL,
        reason         TEXT NOT NULL DEFAULT '',
        event_id       TEXT,
        sources        JSONB,
        origin         TEXT NOT NULL DEFAULT 'strategy',
        status         TEXT NOT NULL,
        decided_at     TIMESTAMPTZ,
        fill_date      DATE,
        fill_qty       INTEGER,
        fill_price     NUMERIC(18, 4),
        fill_ref_price NUMERIC(18, 4),
        commission     NUMERIC(18, 4),
        stamp_tax      NUMERIC(18, 4),
        cash_delta     NUMERIC(18, 4),
        reject_code    TEXT,
        reject_reason  TEXT,
        UNIQUE (account_id, trade_date, symbol, side)
    )
    """,
    "CREATE INDEX IF NOT EXISTS paper_decisions_account_date_idx "
    "ON paper_decisions (account_id, trade_date DESC)",
    # E5：风控退出的来源标记（strategy / risk）；旧库补列走另起的迁移语句。
    "ALTER TABLE paper_decisions ADD COLUMN IF NOT EXISTS origin "
    "TEXT NOT NULL DEFAULT 'strategy'",
    """
    CREATE TABLE IF NOT EXISTS paper_equity (
        account_id   UUID NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
        trade_date   DATE NOT NULL,
        cash         NUMERIC(18, 4) NOT NULL,
        market_value NUMERIC(18, 4) NOT NULL,
        equity       NUMERIC(18, 4) NOT NULL,
        PRIMARY KEY (account_id, trade_date)
    )
    """,
    # ── E5 风控：决策审计（E7 的决策审计日志复用同一张表）────────────
    # `decision_id` **不设外键**：被拦截的提案在 paper_decisions 里没有行，但审计要留下
    # would-be id；放行/缩量/止损触发的行则与真实决策一一对应。`metrics` 存当时的读数
    # （百分比与金额），让一行审计可复算判据，而不是只看到一句结论。
    """
    CREATE TABLE IF NOT EXISTS decision_audit (
        id           UUID PRIMARY KEY,
        account_id   UUID NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
        decision_id  UUID,
        trade_date   DATE NOT NULL,
        symbol       TEXT,
        side         TEXT,
        origin       TEXT NOT NULL,
        kind         TEXT NOT NULL,
        verdict      TEXT NOT NULL,
        gate         TEXT,
        metrics      JSONB NOT NULL DEFAULT '{}'::jsonb,
        detail       TEXT NOT NULL DEFAULT '',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS decision_audit_account_date_idx "
    "ON decision_audit (account_id, trade_date DESC)",
    # ── M7 绩效研报：一次生成即冻结（分享链接永远看同一份）────────────
    # `snapshot` / `report` 都是冻结产物正文；两个 hash 由服务端算好落库，
    # `share_token` 为 NULL 表示未分享（撤销即置回 NULL，公开端点随之 404）。
    """
    CREATE TABLE IF NOT EXISTS research_reports (
        id            UUID PRIMARY KEY,
        user_id       BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        account_id    UUID        NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
        snapshot      JSONB       NOT NULL,
        snapshot_hash TEXT        NOT NULL,
        report        JSONB       NOT NULL,
        report_hash   TEXT        NOT NULL,
        share_token   TEXT        UNIQUE,
        shared_at     TIMESTAMPTZ,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS research_reports_user_created_idx "
    "ON research_reports (user_id, created_at DESC)",
    # 幂等复用按 (账户, 快照) 查：同一份输入不出第二份报告
    "CREATE INDEX IF NOT EXISTS research_reports_account_snapshot_idx "
    "ON research_reports (account_id, snapshot_hash)",
    # ── M8：深路径运行记录（过程可视化的真源）+ 报告容器扩列 ──
    # 深研报落**同一张** research_reports：分享 / 撤销 / 公开只读 / Markdown / PDF 全部按
    # report id 工作，另起一张表等于把 M7 的六个端点重写一遍（SPEC §9「不得重做」）。
    """
    CREATE TABLE IF NOT EXISTS research_runs (
        id          UUID PRIMARY KEY,
        user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        question    TEXT        NOT NULL,
        subject     JSONB       NOT NULL,
        status      TEXT        NOT NULL,
        events      JSONB       NOT NULL DEFAULT '[]'::jsonb,
        budget      JSONB       NOT NULL DEFAULT '{}'::jsonb,
        report_id   UUID,
        error       TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS research_runs_user_created_idx "
    "ON research_runs (user_id, created_at DESC)",
    # 三个幂等 ALTER：老行拿到 'paper_account_report'（M7 的账户报告），account_id 放开为可空
    "ALTER TABLE research_reports ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL "
    "DEFAULT 'paper_account_report'",
    "ALTER TABLE research_reports ADD COLUMN IF NOT EXISTS subject JSONB",
    "ALTER TABLE research_reports ALTER COLUMN account_id DROP NOT NULL",
    # ── P3-E1a：异步回测任务（SPEC §2.1）──
    # **队列实体就是这张表**：worker 轮询取 `status='queued'` 的行，不另设 `asyncio.Queue`
    # （它绑事件循环，而离线用例是模块级 TestClient、每请求一个 loop——SPEC §2.2）。
    # `request` 同时是「用户可见入参」与「冻结的执行材料」（用户策略源码全文在内），
    # 理由同 SPEC §2.1：异步化后出现了「提交后被改源码」的窗口。
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id          UUID PRIMARY KEY,
        user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind        TEXT        NOT NULL,
        status      TEXT        NOT NULL,
        request     JSONB       NOT NULL,
        progress    JSONB       NOT NULL DEFAULT '{}'::jsonb,
        result      JSONB,
        error       JSONB,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at  TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # 幂等键（E4b / L7）：客户端每次**用户动作**给一个，重试复用同一个。
    # 部分唯一索引：没给键的行（`NULL`）天然互不冲突，`ON CONFLICT` 的推断子句
    # 必须连 `WHERE` 一起写才能对上这个索引。
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_idempotency_idx "
    "ON tasks (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS tasks_user_created_idx "
    "ON tasks (user_id, created_at DESC)",
    # worker 取件与启动扫尾都按 status 过滤，且任务表是**热表**（每次回测一行）
    "CREATE INDEX IF NOT EXISTS tasks_status_created_idx ON tasks (status, created_at)",
    # ── P3-E2：用户配额（SPEC §3.1）──
    # 计数表：跨重启持久的累计用量。主键即「一个用户一个维度一个周期一行」，
    # 递增走 `ON CONFLICT` 的那条语句（见 `bump_usage_counter`）。
    """
    CREATE TABLE IF NOT EXISTS usage_counters (
        user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        metric     TEXT        NOT NULL,
        period_key TEXT        NOT NULL,
        used       INT         NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, metric, period_key)
    )
    """,
    # 拒绝记录：admin 的「审计」半边读它（只有用量快照的话，审计没有内容），
    # 验收「超配额被拒并给出明确提示」的可查证据也是它。只记不判，不进任何判定路径。
    """
    CREATE TABLE IF NOT EXISTS quota_rejections (
        id          BIGSERIAL   PRIMARY KEY,
        user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        metric      TEXT        NOT NULL,
        used        INT         NOT NULL,
        limit_value INT         NOT NULL,
        at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


class EmailTaken(RuntimeError):
    """邮箱已被注册（`users.email` 唯一约束）。"""


class SymbolTracked(RuntimeError):
    """该标的已在本人的自选股里（`watchlist` 的 `UNIQUE(user_id, symbol)`）。"""


class StrategyNameTaken(RuntimeError):
    """策略名已被本人占用（`strategies` 的 `UNIQUE(user_id, name)`）。"""


@asynccontextmanager
async def open_pool(
    conn_string: str, *, min_size: int = 1, max_size: int = 8
) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(
        conn_string,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close()


async def init_schema(pool: AsyncConnectionPool) -> None:
    """建表（幂等）。重复执行安全，表已存在时不动。"""
    async with pool.connection() as conn:
        for statement in SCHEMA:
            await conn.execute(statement)


class Database:
    """业务表的全部读写出口。

    刻意只有一个类、方法粒度即业务动作：端点拿到的就是这些语义化调用，
    不在端点里拼 SQL（离线测试注入同形替身即可跑归属矩阵，见 `tests/fakes.py`）。
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    # ── 用户 ────────────────────────────────────────────────

    async def create_user(self, email: str, password_hash: str) -> dict[str, Any]:
        """建用户；邮箱重复抛 `EmailTaken`（不返回 None，避免调用方漏判）。"""
        try:
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
                    "RETURNING id, email, created_at, role",
                    (email, password_hash),
                )
                row = await cursor.fetchone()
        except errors.UniqueViolation as exc:
            raise EmailTaken(email) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return row

    async def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        # `token_version` 一并取出：登录要按它签发（否则登出后再登录会签出一张立刻失效的票）
        return await self._one(
            "SELECT id, email, password_hash, token_version FROM users WHERE email = %s",
            (email,),
        )

    async def find_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        # `token_version` 随行返回：`require_user` 用它判撤销，省一次读库（E8a）
        # `role` 同理：`require_admin` 与配额豁免都吃这一行（E2b）
        return await self._one(
            "SELECT id, email, token_version, role FROM users WHERE id = %s", (user_id,)
        )

    async def bump_token_version(self, user_id: int) -> None:
        """把用户的撤销版本加一：此前签发的所有 token 立即失效（登出/改密用，E8a）。

        用户不存在时是空操作——登出不该因为「会话早已无效」而报错。
        """
        await self._exec(
            "UPDATE users SET token_version = token_version + 1 WHERE id = %s", (user_id,)
        )

    # ── 会话归属 ────────────────────────────────────────────

    async def claim_thread(self, thread_id: str, user_id: int) -> None:
        """新会话落归属行。`DO NOTHING`：同一 thread_id 重复调用不报错也不改主人。"""
        await self._exec(
            "INSERT INTO chat_threads (thread_id, user_id) VALUES (%s::uuid, %s) "
            "ON CONFLICT (thread_id) DO NOTHING",
            (thread_id, user_id),
        )

    async def touch_thread(self, thread_id: str, user_id: int) -> None:
        """续聊时刷新活动时间（会话列表按它倒序）。"""
        await self._exec(
            "UPDATE chat_threads SET last_active_at = now() "
            "WHERE thread_id = %s::uuid AND user_id = %s",
            (thread_id, user_id),
        )

    async def thread_owner(self, thread_id: str) -> int | None:
        """归属查询：None 表示无主（或不存在）——调用方一律翻 404，不区分。"""
        row = await self._one(
            "SELECT user_id FROM chat_threads WHERE thread_id = %s::uuid", (thread_id,)
        )
        return int(row["user_id"]) if row is not None else None

    async def list_threads(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """按最近活动倒序的会话（号 + 活动时间）。纯自有表查询，不碰 checkpointer 内部表。

        带出 `last_active_at` 而不是只给号：个人空间的会话历史要显示「最近活动」，
        而它正是排序依据本身，没有额外查询。
        """
        return [
            {"thread_id": str(row["thread_id"]), "last_active_at": row["last_active_at"]}
            for row in await self._all(
                "SELECT thread_id, last_active_at FROM chat_threads WHERE user_id = %s "
                "ORDER BY last_active_at DESC LIMIT %s",
                (user_id, limit),
            )
        ]

    async def drop_thread(self, thread_id: str, user_id: int) -> None:
        await self._exec(
            "DELETE FROM chat_threads WHERE thread_id = %s::uuid AND user_id = %s",
            (thread_id, user_id),
        )

    # ── 自选股 ──────────────────────────────────────────────

    async def list_watchlist(self, user_id: int) -> list[dict[str, Any]]:
        """本人全部自选，按（分组, 加入时间）升序。分组视图由上层聚合，这里只给扁平事实。"""
        rows = await self._all(
            "SELECT symbol, group_name, added_at, added_price FROM watchlist "
            "WHERE user_id = %s ORDER BY group_name, added_at",
            (user_id,),
        )
        return [_watchlist_row(row) for row in rows]

    async def add_watchlist_item(
        self, user_id: int, symbol: str, group_name: str, added_price: float | None
    ) -> dict[str, Any]:
        """加自选；已在自选里抛 `SymbolTracked`（不返回 None，避免调用方漏判）。

        `added_price` 允许为 None —— 样例数据只覆盖少数标的，取不到价就如实留空。
        """
        try:
            row = await self._one(
                "INSERT INTO watchlist (user_id, symbol, group_name, added_price) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING symbol, group_name, added_at, added_price",
                (user_id, symbol, group_name, added_price),
            )
        except errors.UniqueViolation as exc:
            raise SymbolTracked(symbol) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return _watchlist_row(row)

    async def move_watchlist_item(self, user_id: int, symbol: str, group_name: str) -> bool:
        """改分组。返回是否命中（False 由调用方翻 404，不区分「不存在」与「不是本人」）。"""
        return (
            await self._one(
                "UPDATE watchlist SET group_name = %s "
                "WHERE user_id = %s AND symbol = %s RETURNING id",
                (group_name, user_id, symbol),
            )
            is not None
        )

    async def drop_watchlist_item(self, user_id: int, symbol: str) -> bool:
        return (
            await self._one(
                "DELETE FROM watchlist WHERE user_id = %s AND symbol = %s RETURNING id",
                (user_id, symbol),
            )
            is not None
        )

    async def rename_watchlist_group(self, user_id: int, old: str, new: str) -> bool:
        """重命名分组 = 一条 UPDATE。目标名已存在时两组自然合并（不设 409）。"""
        return await self._group_update(user_id, new, old)

    async def drop_watchlist_group(self, user_id: int, group: str) -> bool:
        """删组 = 组内标的回落 `DEFAULT_GROUP`，同样只是一条 UPDATE（无分组实体表）。"""
        return await self._group_update(user_id, DEFAULT_GROUP, group)

    async def _group_update(self, user_id: int, new: str, old: str) -> bool:
        return (
            await self._one(
                "UPDATE watchlist SET group_name = %s "
                "WHERE user_id = %s AND group_name = %s RETURNING id",
                (new, user_id, old),
            )
            is not None
        )

    # ── 用户策略（M4c）─────────────────────────────────────

    async def list_strategies(self, user_id: int) -> list[dict[str, Any]]:
        """本人全部策略的**摘要**（不带 code / params——列表不为每行拖一份源码），最近改的在前。

        摘要**不复用 `_strategy_row`**：那个函数吃的是整行（要 `params` 列），而这条 SELECT
        刻意没取它——复用会 KeyError（真库上踩过，离线替身自己拼 dict 反而看不出来）。
        """
        rows = await self._all(
            "SELECT id, name, created_at, updated_at FROM strategies "
            "WHERE user_id = %s ORDER BY updated_at DESC",
            (user_id,),
        )
        return [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    async def create_strategy(
        self,
        strategy_id: str,
        user_id: int,
        name: str,
        code: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """建策略。撞名抛 `StrategyNameTaken`（不返回 None，避免调用方漏判）。"""
        try:
            row = await self._one(
                "INSERT INTO strategies (id, user_id, name, code, params) "
                "VALUES (%s::uuid, %s, %s, %s, %s) "
                "RETURNING id, name, code, params, created_at, updated_at",
                (strategy_id, user_id, name, code, Jsonb(params)),
            )
        except errors.UniqueViolation as exc:
            raise StrategyNameTaken(name) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return _strategy_row(row)

    async def get_strategy(self, user_id: int, strategy_id: str) -> dict[str, Any] | None:
        """单条（含 code）。带 `user_id` 过滤，越权与不存在同为 None（由调用方翻 404）。"""
        row = await self._one(
            "SELECT id, name, code, params, created_at, updated_at FROM strategies "
            "WHERE id = %s::uuid AND user_id = %s",
            (strategy_id, user_id),
        )
        return _strategy_row(row) if row is not None else None

    async def update_strategy(
        self,
        user_id: int,
        strategy_id: str,
        *,
        name: str | None = None,
        code: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """改策略（部分更新：`None` = 不动该列）。

        一条 UPDATE + `COALESCE`，不做「先读再写」——那样两次请求之间会有竞态窗口。
        `params` 的 `Jsonb(None)` 是 SQL NULL，正好落进「不动该列」的语义。
        """
        try:
            row = await self._one(
                "UPDATE strategies SET name = COALESCE(%s, name), code = COALESCE(%s, code), "
                "params = COALESCE(%s, params), updated_at = now() "
                "WHERE id = %s::uuid AND user_id = %s "
                "RETURNING id, name, code, params, created_at, updated_at",
                (name, code, Jsonb(params) if params is not None else None, strategy_id, user_id),
            )
        except errors.UniqueViolation as exc:  # 改名撞上本人另一条策略
            raise StrategyNameTaken(name or "") from exc
        return _strategy_row(row) if row is not None else None

    async def drop_strategy(self, user_id: int, strategy_id: str) -> bool:
        """删策略。返回是否命中（回测记录**不级联删**：`strategy_id` 悬空即可）。"""
        return (
            await self._one(
                "DELETE FROM strategies WHERE id = %s::uuid AND user_id = %s RETURNING id",
                (strategy_id, user_id),
            )
            is not None
        )

    # ── 回测记录 ────────────────────────────────────────────

    async def save_backtest_run(
        self,
        run_id: str,
        user_id: int,
        request: dict[str, Any],
        report: dict[str, Any],
        *,
        strategy_id: str | None = None,
        code_sha256: str | None = None,
        data_digest: str | None = None,
    ) -> None:
        """落一次回测。`request` 与 `report` 都是普通 dict，必须显式包 `Jsonb`——
        psycopg 不会把 dict 自动转 jsonb，而 `list` 会被适配成数组。

        `strategy_id` / `code_sha256` 只对用户策略有值：前者供「我的回测」认出策略，
        后者供前端比对「代码是否已改」（M4c 运行契约）。`data_digest`（E7d）是这次运行
        读到的数据指纹（窗口内分片），供「实验可复现」核对；报告 meta 里也有一份。
        """
        await self._exec(
            "INSERT INTO backtest_runs "
            "(id, user_id, request, report, strategy_id, code_sha256, data_digest) "
            "VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s, %s)",
            (run_id, user_id, Jsonb(request), Jsonb(report), strategy_id, code_sha256, data_digest),
        )

    async def list_backtest_runs(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """摘要列表。**只抽 JSONB 子集**，不把整份报告拉回来。

        `metrics` 整块取（`->` 而非 `->>`）：逐键抽出来的是 text，数字会静默变成字符串，
        前端按比例格式化时不会报错、只会算错。

        `strategy_name` 走 `report->'meta'->>'strategy_name'`（M4c）：内置策略为 null，
        用户策略给名字——列表要能显示「用户策略 · 双均线」而不是一个 `user`。
        """
        rows = await self._all(
            "SELECT id, created_at, "
            "request->>'symbol' AS symbol, request->>'strategy' AS strategy, "
            "request->>'start' AS start, request->>'end' AS end, "
            "request->>'pit_mode' AS pit_mode, report->'metrics' AS metrics, "
            "report->'meta'->>'strategy_name' AS strategy_name, "
            # E7d：数据指纹——列表行也显示（旧记录为 NULL，前端按缺失显示）
            "data_digest "
            "FROM backtest_runs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
        return [{**row, "id": str(row["id"])} for row in rows]

    async def get_backtest_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        """按 id 取回完整报告。带 `user_id` 过滤，越权与不存在同为 None（由调用方翻 404）。

        `strategy_id` / `code_sha256` 一并带出（M4c）：前端拿它与策略**当前**的 hash 比对，
        得出「已非当次运行的代码」。服务端不做这个判断——事实给出去，结论由读取方下。
        """
        row = await self._one(
            "SELECT id, created_at, request, report, strategy_id, code_sha256, data_digest "
            "FROM backtest_runs WHERE id = %s::uuid AND user_id = %s",
            (run_id, user_id),
        )
        if row is None:
            return None
        return {
            **row,
            "id": str(row["id"]),
            "strategy_id": str(row["strategy_id"]) if row["strategy_id"] else None,
        }

    # ── 批量 / 网格汇总（M5b）────────────────────────────────

    async def save_optimization_run(
        self,
        run_id: str,
        user_id: int,
        request: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        """落一次批处理汇总。与 `save_backtest_run` 同形：dict 必须显式包 `Jsonb`。

        与回测记录**分开存**是 SPEC §6 M5b 定的：网格落的是「每格一行摘要」，
        与「我的回测」混流会让列表里一半是没法打开的残缺报告。
        """
        await self._exec(
            "INSERT INTO optimization_runs (id, user_id, request, summary) "
            "VALUES (%s::uuid, %s, %s, %s)",
            (run_id, user_id, Jsonb(request), Jsonb(summary)),
        )

    async def list_optimization_runs(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """摘要列表（最近在前）。**取 `summary - 'cells'`**：去掉最大的那一块（每格矩阵），
        其余整块原样带出。

        比逐键抽字段好在两点：① 不会踩「`->>` 把数字变成 text」那个坑（见
        `list_backtest_runs` 的注释）；② 新增汇总字段时列表页自动就有了，不必两处同步。
        """
        rows = await self._all(
            "SELECT id, created_at, request, summary - 'cells' AS summary "
            "FROM optimization_runs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
        return [{**row, "id": str(row["id"])} for row in rows]

    async def get_optimization_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        """按 id 取回完整汇总（含每格矩阵）。越权与不存在同为 None（调用方翻 404）。"""
        row = await self._one(
            "SELECT id, created_at, request, summary FROM optimization_runs "
            "WHERE id = %s::uuid AND user_id = %s",
            (run_id, user_id),
        )
        return {**row, "id": str(row["id"])} if row is not None else None

    # ── 模拟盘（M6）─────────────────────────────────────────
    #
    # 三条约定：① 一次「推进」写多张表，**必须在一个事务里**（半推进的状态比没推进更难收场）；
    # ② 账户行带**乐观并发守卫**（`WHERE as_of = 期望值`），0 行受影响即返 False，由端点翻 409；
    # ③ 决策的 `decided_at` 由重放从日志里带回来，所以整份 upsert 不会抹掉用户做过的动作。

    async def create_paper_account(
        self,
        *,
        account_id: str,
        user_id: int,
        name: str,
        config: dict[str, Any],
        cash: float,
        as_of: date,
        rules: dict[str, Any],
        risk_state: dict[str, Any],
        positions: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        equity: list[dict[str, Any]],
        risk_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """建会话 + 首批决策/持仓/净值点/风控审计，一个事务。"""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO paper_accounts "
                "(id, user_id, name, config, cash, as_of, rules, risk_state) "
                "VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)",
                (
                    account_id,
                    user_id,
                    name,
                    Jsonb(config),
                    cash,
                    as_of,
                    Jsonb(rules),
                    Jsonb(risk_state or {}),
                ),
            )
            await _write_paper_state(
                conn, account_id, positions, decisions, equity, risk_events
            )
        row = await self.get_paper_account(user_id, account_id)
        assert row is not None  # 刚插进去的行必然取得到
        return row

    async def paper_advance(
        self,
        *,
        account_id: str,
        user_id: int,
        expected_as_of: date,
        cash: float,
        realized_pnl: float,
        as_of: date,
        status: str,
        rules: dict[str, Any],
        risk_state: dict[str, Any],
        positions: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        equity: list[dict[str, Any]],
        risk_events: list[dict[str, Any]],
    ) -> bool:
        """推进一个交易日（一个事务）。返回 False = 守卫没中（已被别的请求推进过），**什么都没写**。"""
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                "UPDATE paper_accounts SET cash = %s, realized_pnl = %s, as_of = %s, "
                "status = %s, rules = %s, risk_state = %s, updated_at = now() "
                "WHERE id = %s::uuid AND user_id = %s AND as_of = %s RETURNING id",
                (
                    cash,
                    realized_pnl,
                    as_of,
                    status,
                    Jsonb(rules),
                    Jsonb(risk_state or {}),
                    account_id,
                    user_id,
                    expected_as_of,
                ),
            )
            if await cursor.fetchone() is None:
                return False
            # 持仓整体换一遍（不改写别的会话的行）：陈旧行必须消失，否则平掉的仓位会长留
            await conn.execute(
                "DELETE FROM paper_positions WHERE account_id = %s::uuid", (account_id,)
            )
            await _write_paper_state(
                conn, account_id, positions, decisions, equity, risk_events
            )
        return True

    async def list_paper_accounts(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """会话列表（最近创建在前）。摘要只取界面要用的几列，不拖整份 config。"""
        return await self._all(
            "SELECT id, name, status, cash, as_of, rules, created_at, "
            "config->>'strategy' AS strategy, config->>'strategy_name' AS strategy_name, "
            "config->'symbols' AS symbols, config->>'start' AS start, config->>'end' AS end "
            "FROM paper_accounts WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )

    async def list_all_paper_accounts(self, limit: int = 500) -> list[dict[str, Any]]:
        """**跨用户**列出账户（M7b 的结算 job 用）。

        这是全项目唯一一条不带 `user_id` 的业务读：定时结算要为所有用户跑，没有「当前用户」。
        使用面因此收窄到调度器——**端点一律走带 `user_id` 的那条**（M1 归属口径不破）。
        """
        return await self._all(
            "SELECT id, user_id, name, status, as_of, config FROM paper_accounts "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    async def get_paper_account(self, user_id: int, account_id: str) -> dict[str, Any] | None:
        """单条（含 config）。带 `user_id` 过滤：越权与不存在同为 None（由调用方翻 404）。

        本层一律**原样返回行**（UUID / Decimal / date 都不动）——转成 JSON 契约形状是
        `app.paper.store` 的职责，让驱动类型只在一处被翻译。
        """
        return await self._one(
            "SELECT id, name, config, status, cash, realized_pnl, as_of, rules, risk_state, "
            "created_at, updated_at FROM paper_accounts WHERE id = %s::uuid AND user_id = %s",
            (account_id, user_id),
        )

    async def paper_positions(self, account_id: str) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT account_id, symbol, shares, entry_price, entry_fees, entry_date, "
            "entry_reason FROM paper_positions WHERE account_id = %s::uuid ORDER BY symbol",
            (account_id,),
        )

    async def paper_decisions(self, account_id: str, limit: int) -> list[dict[str, Any]]:
        """决策流水（新的在前）。`limit` 只是防呆——池子 ≤20、区间 ≤250 个交易日，
        信号本来就稀（M6 规划期实测：20 标的 × 181 个交易日约 110 条）。"""
        return await self._all(
            "SELECT * FROM paper_decisions WHERE account_id = %s::uuid "
            "ORDER BY trade_date DESC, symbol, side LIMIT %s",
            (account_id, limit),
        )

    async def paper_equity(self, account_id: str) -> list[dict[str, Any]]:
        """净值曲线（按交易日升序——画图要的顺序）。"""
        return await self._all(
            "SELECT account_id, trade_date, cash, market_value, equity FROM paper_equity "
            "WHERE account_id = %s::uuid ORDER BY trade_date",
            (account_id,),
        )

    async def paper_audit(self, account_id: str, limit: int) -> list[dict[str, Any]]:
        """风控审计（新的在前）。`limit` 是展示面防呆——完整台账在库里，按账户级联删除。"""
        return await self._all(
            "SELECT * FROM decision_audit WHERE account_id = %s::uuid "
            "ORDER BY trade_date DESC, created_at DESC, id LIMIT %s",
            (account_id, limit),
        )

    async def paper_decision(self, user_id: int, decision_id: str) -> dict[str, Any] | None:
        """按 id 取一条决策（**连账户归属一起判**）。越权与不存在同为 None。"""
        return await self._one(
            "SELECT d.* FROM paper_decisions d JOIN paper_accounts a ON a.id = d.account_id "
            "WHERE d.id = %s::uuid AND a.user_id = %s",
            (decision_id, user_id),
        )

    async def set_paper_decision(self, decision_id: str, status: str) -> bool:
        """把一条**待审批**决策改成 `status`。返回 False = 它已不是待审批（端点翻 409）。

        守卫写在 SQL 里（`status = 'pending'`）而不是「先读再写」：两次请求之间会开竞态窗口，
        而这里正是「同一张单被批两次」会发生的地方。`decided_at` 取库里的 `now()`——
        时间基准只有一处（应用进程的时钟不参与）。
        """
        return (
            await self._one(
                "UPDATE paper_decisions SET status = %s, decided_at = now() "
                "WHERE id = %s::uuid AND status = 'pending' RETURNING id",
                (status, decision_id),
            )
            is not None
        )

    async def set_paper_decisions_bulk(self, account_id: str, status: str) -> int:
        """一键全批 / 全驳：把该会话所有待审批决策改成 `status`，返回改了几条。"""
        rows = await self._all(
            "UPDATE paper_decisions SET status = %s, decided_at = now() "
            "WHERE account_id = %s::uuid AND status = 'pending' RETURNING id",
            (status, account_id),
        )
        return len(rows)

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
    ) -> dict[str, Any]:
        """落一份冻结报告。`account_id=None` = 深研报（M8，没有账户主体）。"""
        row = await self._one(
            "INSERT INTO research_reports "
            "(id, user_id, account_id, snapshot, snapshot_hash, report, report_hash, kind, subject) "
            "VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, created_at",
            (report_id, user_id, account_id, Jsonb(snapshot), snapshot_hash,
             Jsonb(report), report_hash, kind,
             Jsonb(subject) if subject is not None else None),
        )
        assert row is not None  # RETURNING 必有一行
        return row

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

    async def get_research_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        return await self._one(
            "SELECT id, question, subject, status, events, budget, report_id, error, "
            "created_at, updated_at FROM research_runs WHERE user_id = %s AND id = %s::uuid",
            (user_id, run_id),
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
    ) -> dict[str, Any]:
        """建一条 `queued` 任务。`total` 是格数——单次回测恒 1（E1b 的网格是展开后的格数）。

        **带幂等键时返回既有的那一行**（E4b / L7）：`ON CONFLICT DO NOTHING` 撞上就回查，
        调用方凭 `row["id"] != task_id` 判断这是不是一次重放。冲突判定交给唯一索引而不是
        「先查后插」——两次调用之间的窗口正是要防的那个窗口。
        """
        row = await self._one(
            "INSERT INTO tasks (id, user_id, kind, status, request, progress, idempotency_key) "
            "VALUES (%s::uuid, %s, %s, 'queued', %s, %s, %s) "
            "ON CONFLICT (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL "
            "DO NOTHING "
            f"RETURNING {_TASK_COLUMNS}",
            (
                task_id,
                user_id,
                kind,
                Jsonb(request),
                Jsonb({"done": 0, "total": total}),
                idempotency_key,
            ),
        )
        if row is not None:
            return row
        existing = await self._one(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE user_id = %s AND idempotency_key = %s",
            (user_id, idempotency_key),
        )
        assert existing is not None  # 冲突了却查不到 = 唯一索引与这行不一致
        return existing

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

    # ── 内部 ────────────────────────────────────────────────

    async def _one(self, sql: str, params: tuple) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchone()

    async def _all(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            return list(await cursor.fetchall())

    async def _exec(self, sql: str, params: tuple) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, params)


async def _write_paper_state(
    conn: Any,
    account_id: str,
    positions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    equity: list[dict[str, Any]],
    risk_events: list[dict[str, Any]],
) -> None:
    """把一批持仓 / 决策 / 净值点 / 风控审计写进库。**调用方负责开事务**（见 `paper_advance`）。

    行 dict 由 `app.paper.store` 造好（本层不 import 业务包）。决策走 upsert：
    重放每次都会把**全量**决策交回来，同 id 重写一遍是幂等的——
    而 `decided_at` 由重放从日志里带回来，故用户做过的动作不会被抹掉。
    审计行走 `DO NOTHING`：它是**不可变事实**，过去的日子重放也不重写（与净值表同一条纪律）。
    """
    if positions:
        await _executemany(
            conn,
            "INSERT INTO paper_positions "
            "(account_id, symbol, shares, entry_price, entry_fees, entry_date, entry_reason) "
            "VALUES (%s::uuid, %s, %s, %s, %s, %s, %s)",
            [
                (
                    p["account_id"],
                    p["symbol"],
                    p["shares"],
                    p["entry_price"],
                    p["entry_fees"],
                    p["entry_date"],
                    p["entry_reason"],
                )
                for p in positions
            ],
        )
    if decisions:
        await _executemany(
            conn,
            "INSERT INTO paper_decisions "
            "(id, account_id, trade_date, symbol, side, est_qty, est_price, reason, event_id, "
            " sources, origin, status, decided_at, fill_date, fill_qty, fill_price, "
            " fill_ref_price, commission, stamp_tax, cash_delta, reject_code, reject_reason) "
            "VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            " origin = EXCLUDED.origin, status = EXCLUDED.status, "
            " decided_at = EXCLUDED.decided_at, "
            " est_qty = EXCLUDED.est_qty, est_price = EXCLUDED.est_price, "
            " reason = EXCLUDED.reason, event_id = EXCLUDED.event_id, sources = EXCLUDED.sources, "
            " fill_date = EXCLUDED.fill_date, fill_qty = EXCLUDED.fill_qty, "
            " fill_price = EXCLUDED.fill_price, fill_ref_price = EXCLUDED.fill_ref_price, "
            " commission = EXCLUDED.commission, stamp_tax = EXCLUDED.stamp_tax, "
            " cash_delta = EXCLUDED.cash_delta, reject_code = EXCLUDED.reject_code, "
            " reject_reason = EXCLUDED.reject_reason",
            [
                (
                    d["id"],
                    d["account_id"],
                    d["trade_date"],
                    d["symbol"],
                    d["side"],
                    d["est_qty"],
                    d["est_price"],
                    d["reason"],
                    d["event_id"],
                    Jsonb(d["sources"]) if d["sources"] else None,
                    d["origin"],
                    d["status"],
                    d["decided_at"],
                    d["fill_date"],
                    d["fill_qty"],
                    d["fill_price"],
                    d["fill_ref_price"],
                    d["commission"],
                    d["stamp_tax"],
                    d["cash_delta"],
                    d["reject_code"],
                    d["reject_reason"],
                )
                for d in decisions
            ],
        )
    if risk_events:
        await _executemany(
            conn,
            "INSERT INTO decision_audit "
            "(id, account_id, decision_id, trade_date, symbol, side, origin, kind, verdict, "
            " gate, metrics, detail) "
            "VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            [
                (
                    e["id"],
                    e["account_id"],
                    e["decision_id"],
                    e["trade_date"],
                    e["symbol"],
                    e["side"],
                    e["origin"],
                    e["kind"],
                    e["verdict"],
                    e["gate"],
                    Jsonb(e["metrics"]),
                    e["detail"],
                )
                for e in risk_events
            ],
        )
    if equity:
        # 估值点是**不可变事实**：写过的日子不再改（重放重算也该得到同一个数）
        await _executemany(
            conn,
            "INSERT INTO paper_equity (account_id, trade_date, cash, market_value, equity) "
            "VALUES (%s::uuid, %s, %s, %s, %s) ON CONFLICT (account_id, trade_date) DO NOTHING",
            [
                (e["account_id"], e["trade_date"], e["cash"], e["market_value"], e["equity"])
                for e in equity
            ],
        )


async def _executemany(conn: Any, sql: str, rows: list[tuple]) -> None:
    """批量写。**`executemany` 在异步连接上不存在**（那是同步 API），得走游标——
    psycopg 的 `AsyncConnection` 只提供 `execute`（返回游标），批量要自己开 cursor。"""
    async with conn.cursor() as cursor:
        await cursor.executemany(sql, rows)


def _strategy_row(row: dict[str, Any]) -> dict[str, Any]:
    """`id` 是 UUID，`params` 是 Jsonb——在数据层就转成 JSON 契约里的形状。

    同 `_watchlist_row` 的理由：让 `UUID` / `Jsonb` 这类驱动类型止步于数据层，
    端点与离线替身拿到的都是普通 dict。
    """
    return {**row, "id": str(row["id"]), "params": dict(row["params"] or {})}


def _watchlist_row(row: dict[str, Any]) -> dict[str, Any]:
    """`added_price` 是 NUMERIC，psycopg 给的是 `Decimal`。

    在数据层就转成 float：JSON 契约里它是 number，而 `Decimal - float` 会直接 TypeError——
    让 Decimal 漂到端点里做算术是个只会炸在运行期的坑。
    """
    price = row["added_price"]
    return {**row, "added_price": float(price) if price is not None else None}
