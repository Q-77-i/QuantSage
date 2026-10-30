"""模拟盘：账户、持仓、决策、净值与风控审计（M6 / E5）。"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.types.json import Jsonb


class PaperMixin:
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

    async def list_all_paper_accounts(
        self, *, after_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """**跨用户**列出一页账户（M7b 的结算 job 用，keyset 分页）。

        这是全项目唯一一条不带 `user_id` 的业务读：定时结算要为所有用户跑，没有「当前用户」。
        使用面因此收窄到调度器——**端点一律走带 `user_id` 的那条**（M1 归属口径不破）。

        keyset 按 `id` 升序：调度器从 None 开始循环到空页，**不会像 LIMIT 500 那样**
        永远漏掉第 501 个之后的账户。运行期间新建的账户下一轮自然补上。
        """
        if after_id is None:
            return await self._all(
                "SELECT id, user_id, name, status, as_of, config FROM paper_accounts "
                "ORDER BY id LIMIT %s",
                (limit,),
            )
        return await self._all(
            "SELECT id, user_id, name, status, as_of, config FROM paper_accounts "
            "WHERE id > %s::uuid ORDER BY id LIMIT %s",
            (after_id, limit),
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
        """决策流水（新的在前）。`limit` 只是展示防呆，**不得用于重放**。

        重放要的是完整历史（见 `paper_decisions_all`）：截断会让最早那批决策从日志里消失，
        策略输出会按同 id 重新生成并覆盖旧行。池子 ≤20、区间 ≤250 个交易日，信号本来就稀
        （M6 规划期实测：20 标的 × 181 个交易日约 110 条），但用户策略可以远超 500。
        """
        return await self._all(
            "SELECT * FROM paper_decisions WHERE account_id = %s::uuid "
            "ORDER BY trade_date DESC, symbol, side LIMIT %s",
            (account_id, limit),
        )

    async def paper_decisions_all(self, account_id: str) -> list[dict[str, Any]]:
        """重放用的**全量**决策日志（不截断）。排序与 `paper_decisions` 一致。

        上界是账户配置的理论最大值（20 标的 × 250 交易日 = 5,000 条）；单次重放多读
        几 MB 是一次性成本，远小于“旧决策被重生成覆盖”的数据损坏。`API` 的展示列表
        仍用 `paper_decisions(limit)`，两者不得混用（SPEC §7.6）。
        """
        return await self._all(
            "SELECT * FROM paper_decisions WHERE account_id = %s::uuid "
            "ORDER BY trade_date DESC, symbol, side",
            (account_id,),
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
        """一键全批 / 全驳：把该会话所有**策略**待审批决策改成 `status`。

        `origin='agent'` 的提案**排除在外**（收尾项 5a 拍板：Agent 提案必须逐条人工裁决）。

        ⚠ **`run` 不再调用它**（P0 修复）：批量裁决必须与推进同事务，`run_account` 在
        内存里把 pending 改成 approved/rejected，随 `paper_advance` 一次写回。本方法保留
        为 DB 原语（集成用例用它验 agent 排除语义），**不要在请求路径单独调用**——先写库
        再重放会在失败时留下已批准的决策。
        """
        rows = await self._all(
            "UPDATE paper_decisions SET status = %s, decided_at = now() "
            "WHERE account_id = %s::uuid AND status = 'pending' AND origin <> 'agent' "
            "RETURNING id",
            (status, account_id),
        )
        return len(rows)

    async def paper_pending_agent_count(self, account_id: str) -> int:
        """该会话还有几条待审批的 Agent 提案（`run` 的 HITL 守卫）。"""
        row = await self._one(
            "SELECT count(*) AS n FROM paper_decisions "
            "WHERE account_id = %s::uuid AND status = 'pending' AND origin = 'agent'",
            (account_id,),
        )
        return int(row["n"]) if row is not None else 0

    async def add_paper_decision(
        self,
        *,
        account_id: str,
        user_id: int,
        expected_as_of: Any,
        decision: dict[str, Any] | None,
        risk_events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> bool:
        """**单条**写入 Agent 提案（决策 + 审计，同一事务）。

        与 `paper_advance` 的区别：不推进 `as_of`、不重写持仓/净值；只写这一条决策与
        当日该标的的审计。守卫 `status='active' AND as_of=expected_as_of` 且 `FOR UPDATE`：
        提案生成期间账户若被推进，返回 False（端点/工具翻 409），不把决策写到错的日子。
        决策走 `ON CONFLICT DO NOTHING`，绝不用 upsert 覆盖同 id 的策略/风控决策。
        """
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT id FROM paper_accounts "
                "WHERE id = %s::uuid AND user_id = %s AND status = 'active' AND as_of = %s "
                "FOR UPDATE",
                (account_id, user_id, expected_as_of),
            )
            if await cur.fetchone() is None:
                return False
            if decision is not None:
                if str(decision.get("account_id")) != str(account_id):
                    raise ValueError("决策行的 account_id 与目标账户不一致")
                if not await _insert_paper_decision_once(conn, decision):
                    return False
            if risk_events:
                await _write_paper_state(conn, account_id, [], [], [], list(risk_events))
            return True



_PAPER_DECISION_COLUMNS = (
    "id, account_id, trade_date, symbol, side, est_qty, est_price, reason, event_id, "
    "sources, origin, status, decided_at, fill_date, fill_qty, fill_price, "
    "fill_ref_price, commission, stamp_tax, cash_delta, reject_code, reject_reason"
)
_PAPER_DECISION_VALUES = (
    "(%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s, %s, %s, %s, %s, %s, %s, %s)"
)
_PAPER_DECISION_INSERT = (
    f"INSERT INTO paper_decisions ({_PAPER_DECISION_COLUMNS}) VALUES {_PAPER_DECISION_VALUES}"
)


def _paper_decision_params(d: dict[str, Any]) -> tuple:
    return (
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


async def _insert_paper_decision_once(conn: Any, decision: dict[str, Any]) -> bool:
    """单条决策，`ON CONFLICT DO NOTHING`：冲突 = 同 id 已有策略/风控决策，返回 False。"""
    cur = await conn.execute(
        f"{_PAPER_DECISION_INSERT} ON CONFLICT DO NOTHING RETURNING id",
        _paper_decision_params(decision),
    )
    return await cur.fetchone() is not None


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
            f"{_PAPER_DECISION_INSERT} ON CONFLICT (id) DO UPDATE SET "
            " origin = EXCLUDED.origin, status = EXCLUDED.status, "
            " decided_at = EXCLUDED.decided_at, "
            " est_qty = EXCLUDED.est_qty, est_price = EXCLUDED.est_price, "
            " reason = EXCLUDED.reason, event_id = EXCLUDED.event_id, sources = EXCLUDED.sources, "
            " fill_date = EXCLUDED.fill_date, fill_qty = EXCLUDED.fill_qty, "
            " fill_price = EXCLUDED.fill_price, fill_ref_price = EXCLUDED.fill_ref_price, "
            " commission = EXCLUDED.commission, stamp_tax = EXCLUDED.stamp_tax, "
            " cash_delta = EXCLUDED.cash_delta, reject_code = EXCLUDED.reject_code, "
            " reject_reason = EXCLUDED.reject_reason",
            [_paper_decision_params(d) for d in decisions],
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
