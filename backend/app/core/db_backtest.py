"""回测记录与批量/网格汇总（M1c / M5b）。"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb


class BacktestMixin:
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
        strategy_version: int | None = None,
        task_id: str | None = None,
    ) -> str:
        """落一次回测，返回**最终**的 `run_id`。`request` 与 `report` 都是普通 dict，
        必须显式包 `Jsonb`——psycopg 不会把 dict 自动转 jsonb，而 `list` 会被适配成数组。

        `strategy_id` / `code_sha256` 只对用户策略有值：前者供「我的回测」认出策略，
        后者供前端比对「代码是否已改」（M4c 运行契约）。`data_digest`（E7d）是这次运行
        读到的数据指纹（窗口内分片），供「实验可复现」核对；报告 meta 里也有一份。

        `task_id`（P4-S2）是结果幂等键：同一任务被重排后第二次执行时，`ON CONFLICT`
        不新增行，回查既有 `run_id` 返回——重跑不产生第二条记录。
        """
        row = await self._one(
            "INSERT INTO backtest_runs "
            "(id, user_id, request, report, strategy_id, code_sha256, data_digest, "
            " strategy_version, task_id) "
            "VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s, %s, %s, %s::uuid) "
            "ON CONFLICT (task_id) DO NOTHING RETURNING id",
            (run_id, user_id, Jsonb(request), Jsonb(report), strategy_id, code_sha256,
             data_digest, strategy_version, task_id),
        )
        if row is not None:
            return str(row["id"])
        existing = await self.backtest_run_id_by_task(task_id) if task_id else None
        if existing is None:  # pragma: no cover —— 唯一冲突必然能回查到
            raise RuntimeError("backtest_runs 冲突却回查不到既有行")
        return existing

    async def backtest_run_id_by_task(self, task_id: str) -> str | None:
        """按任务号回查结果（P4-S2 重排幂等）。旧记录 `task_id` 为 NULL，查不到。"""
        row = await self._one(
            "SELECT id FROM backtest_runs WHERE task_id = %s::uuid", (task_id,)
        )
        return str(row["id"]) if row is not None else None

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
        *,
        task_id: str | None = None,
    ) -> str:
        """落一次批处理汇总，返回**最终**的 `run_id`。与 `save_backtest_run` 同形：
        dict 必须显式包 `Jsonb`，`task_id` 走同一条幂等口径。

        与回测记录**分开存**是 SPEC §6 M5b 定的：网格落的是「每格一行摘要」，
        与「我的回测」混流会让列表里一半是没法打开的残缺报告。
        """
        row = await self._one(
            "INSERT INTO optimization_runs (id, user_id, request, summary, task_id) "
            "VALUES (%s::uuid, %s, %s, %s, %s::uuid) "
            "ON CONFLICT (task_id) DO NOTHING RETURNING id",
            (run_id, user_id, Jsonb(request), Jsonb(summary), task_id),
        )
        if row is not None:
            return str(row["id"])
        existing = await self.optimization_run_by_task(task_id) if task_id else None
        if existing is None:  # pragma: no cover —— 唯一冲突必然能回查到
            raise RuntimeError("optimization_runs 冲突却回查不到既有行")
        return str(existing["run_id"])

    async def optimization_run_by_task(self, task_id: str) -> dict[str, Any] | None:
        """按任务号回查批处理结果（P4-S2 重排幂等）：`{run_id, summary}` 或 None。"""
        row = await self._one(
            "SELECT id, summary FROM optimization_runs WHERE task_id = %s::uuid",
            (task_id,),
        )
        return {"run_id": str(row["id"]), "summary": row["summary"]} if row else None

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
