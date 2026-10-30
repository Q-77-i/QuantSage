"""业务库的幂等 DDL 与建表常量。

与数据访问拆开：表结构变更集中在这里，`db.py` 只负责连接池与门面组装。
"""

from __future__ import annotations

#: 自选股的回落分组：删组时组内标的回落到它，它自己不可重命名 / 删除。
#: 建表默认值与它同源（下面的 DDL 由它拼出），避免两处各写一份字面量而漂移。
DEFAULT_GROUP = "默认分组"

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
    # 收尾项 5d：策略版本（append-only）。`strategies` 继续存「当前版本」的物化副本，
    # `version` 是 head 计数器 + 乐观锁；旧行默认 1，启动时幂等回填一条版本行。
    "ALTER TABLE strategies ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1",
    """
    CREATE TABLE IF NOT EXISTS strategy_versions (
        id          UUID PRIMARY KEY,
        strategy_id UUID NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
        version_no  INT  NOT NULL,
        name        TEXT NOT NULL,
        code        TEXT NOT NULL,
        params      JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (strategy_id, version_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS strategy_versions_strategy_idx "
    "ON strategy_versions (strategy_id, version_no DESC)",
    # 回填：每条策略若还没有任何版本行，就用它**当前**的内容补一条（version_no = head）。
    # 幂等：有行就跳过；`ON CONFLICT` 兜住多实例同时启动的窗口。
    """
    INSERT INTO strategy_versions (id, strategy_id, version_no, name, code, params, created_at)
    SELECT gen_random_uuid(), s.id, s.version, s.name, s.code, s.params, s.updated_at
    FROM strategies s
    WHERE NOT EXISTS (
        SELECT 1 FROM strategy_versions v WHERE v.strategy_id = s.id
    )
    ON CONFLICT DO NOTHING
    """,
    # M4c：`backtest_runs` 增列走**另起的迁移语句**——`CREATE TABLE IF NOT EXISTS` 对已存在的表
    # 什么都不做，只改上面的 DDL 是不会生效的（见模块 docstring 第二条约定）。
    # `strategy_id` **不设外键**：策略删了记录仍在（报告 JSONB 自足，见 SPEC §5 M4c）。
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS strategy_id UUID",
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS code_sha256 TEXT",
    # E7d：实验可复现的数据维度身份——**窗口内分片**的指纹（`app/report/snapshot.py::window_digest`）。
    # 旧记录为 NULL（不做回填：那要重算数据、不可信），前端按缺失显示。
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS data_digest TEXT",
    # 收尾项 5d：用户策略当次运行绑定的版本。旧记录 NULL（不回填：没有可证明的版本）。
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS strategy_version INT",
    # P4-S2：任务 → 结果幂等键。**普通唯一索引**即可：PG 唯一索引允许多个 NULL，
    # 旧记录（task_id 为 NULL）天然不冲突；`ON CONFLICT (task_id)` 靠它推断。
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS task_id UUID",
    "CREATE UNIQUE INDEX IF NOT EXISTS backtest_runs_task_idx ON backtest_runs (task_id)",
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
    # P4-S2：与 `backtest_runs.task_id` 同一套幂等口径（普通唯一索引 + NULL 不冲突）
    "ALTER TABLE optimization_runs ADD COLUMN IF NOT EXISTS task_id UUID",
    "CREATE UNIQUE INDEX IF NOT EXISTS optimization_runs_task_idx "
    "ON optimization_runs (task_id)",
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
        cancel_requested_at TIMESTAMPTZ,
        priority    INT         NOT NULL DEFAULT 0,
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        attempts    INT         NOT NULL DEFAULT 0,
        max_attempts INT        NOT NULL DEFAULT 2,
        lease_expires_at TIMESTAMPTZ,
        worker_id   TEXT,
        last_heartbeat_at TIMESTAMPTZ,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at  TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # 收尾项 6：running 的协作式中止改成 DB 标志（跨进程可见；单实例下也更稳）。
    # 老库补列；`queued` 的取消仍走状态条件更新，不经过这一列。
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ",
    # 幂等键（E4b / L7）：客户端每次**用户动作**给一个，重试复用同一个。
    # 部分唯一索引：没给键的行（`NULL`）天然互不冲突，`ON CONFLICT` 的推断子句
    # 必须连 `WHERE` 一起写才能对上这个索引。
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_idempotency_idx "
    "ON tasks (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
    # ── 在飞任务配额：槽位而不是锁（2026-10-30）──
    # 配额从「advisory lock + count(*)」改成「占一个 0..limit-1 的空槽」：并发提交
    # 各插各的，撞槽由部分唯一索引判负、重试换槽；任务进终态后索引项自动消失，
    # 槽位释放，不需要任何减计数/对账。老库的正在飞任务在下面先补槽位再建索引。
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS quota_slot SMALLINT",
    """
    WITH ranked AS (
        SELECT t.id,
               coalesce(
                   (SELECT max(x.quota_slot) FROM tasks x
                    WHERE x.user_id = t.user_id
                      AND x.status IN ('queued', 'running')
                      AND x.quota_slot IS NOT NULL),
                   -1
               ) + row_number() OVER (
                   PARTITION BY t.user_id ORDER BY t.created_at, t.id
               ) AS slot
        FROM tasks t
        WHERE t.status IN ('queued', 'running') AND t.quota_slot IS NULL
    )
    UPDATE tasks t SET quota_slot = ranked.slot
    FROM ranked WHERE t.id = ranked.id
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS tasks_active_slot_idx "
    "ON tasks (user_id, quota_slot) "
    "WHERE status IN ('queued', 'running') AND quota_slot IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS tasks_user_created_idx "
    "ON tasks (user_id, created_at DESC)",
    # worker 取件与启动扫尾都按 status 过滤，且任务表是**热表**（每次回测一行）
    "CREATE INDEX IF NOT EXISTS tasks_status_created_idx ON tasks (status, created_at)",
    # ── P4-S2：队列 v2（SPEC §3.2）──
    # 取件（`SKIP LOCKED`）的排序即这条索引：status → available_at → priority DESC → created_at。
    # reaper 只扫 running 里租约过期的行，部分索引足够。
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS max_attempts INT NOT NULL DEFAULT 2",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS worker_id TEXT",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS tasks_claim_idx "
    "ON tasks (status, available_at, priority DESC, created_at)",
    "CREATE INDEX IF NOT EXISTS tasks_lease_idx "
    "ON tasks (lease_expires_at) WHERE status = 'running'",
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
