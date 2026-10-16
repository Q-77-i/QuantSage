"use client";

import { useCallback, useEffect, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { useAuth } from "@/components/auth-provider";
import { api, describeError } from "@/lib/api";
import { count, dayStamp } from "@/lib/format";
import type { AdminUsage, QuotaRejection } from "@/lib/types";

/**
 * 运维面（E2b）：配额视图 + 拒绝记录。**只读**——没有改配额、没有增删用户
 * （配额上限是部署参数，改角色走 `scripts/grant_admin.py`；PRD 写死不做管理后台）。
 *
 * 两处刻意的写法：
 *  * **角色不够不跳登录**——会话是好的，缺的是角色。渲染一句说明，比把已登录的人
 *    送回登录页诚实；
 *  * **服务端才是门**：这里的 `role` 判断只管显示；直接敲 URL 会撞上 403。
 */
export default function AdminPage() {
  const { user } = useAuth();

  if (user && user.role !== "admin") {
    return (
      <main className="mx-auto max-w-[1400px] px-4 py-10">
        <p className="text-sm text-ink-2">需要管理员权限。当前账号是普通用户。</p>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-8">
      <h1 className="font-heading text-xl font-semibold tracking-tight">运维</h1>
      <p className="mt-1 text-sm text-ink-2">
        配额用量与拒绝记录。上限是部署参数（`QUOTA_*`），改角色走 `scripts/grant_admin.py`。
      </p>
      <UsagePanel />
      <RejectionsPanel />
    </main>
  );
}

function UsagePanel() {
  const [data, setData] = useState<AdminUsage | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.adminUsage());
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "用量加载失败：后端未启动或网络不通。"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <p role="alert" className="mt-6 text-sm text-destructive">
        {error}
      </p>
    );
  }
  if (data === null) return <p className="mt-6 text-sm text-ink-3">加载中…</p>;

  const { limits } = data;
  return (
    <section className="mt-6">
      <h2 className="font-heading text-base font-medium">配额用量</h2>
      <p className="mt-1 text-xs text-ink-3">
        在飞任务上限 {limits.active_tasks || "不限"} · 每日 Agent 调用上限{" "}
        {limits.agent_calls_per_day || "不限"}（0 = 不限额；admin 不在限额内）
      </p>
      <TableShell head={["账号", "角色", "在飞任务", "今日 Agent 调用", "今日被拒"]}>
        {data.users.map((row) => (
          <Row key={row.id}>
            <Cell>{row.email}</Cell>
            <Cell className="text-ink-3">{row.role}</Cell>
            <Cell numeric>{count(row.active_tasks)}</Cell>
            <Cell numeric>{count(row.agent_calls_today)}</Cell>
            <Cell numeric>{count(row.rejected_today)}</Cell>
          </Row>
        ))}
      </TableShell>
    </section>
  );
}

function RejectionsPanel() {
  const [rows, setRows] = useState<QuotaRejection[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows((await api.adminRejections()).rejections);
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "拒绝记录加载失败：后端未启动或网络不通。"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <p role="alert" className="mt-6 text-sm text-destructive">
        {error}
      </p>
    );
  }
  if (rows === null) return <p className="mt-6 text-sm text-ink-3">加载中…</p>;

  return (
    <section className="mt-8">
      <h2 className="font-heading text-base font-medium">最近的拒绝记录</h2>
      <p className="mt-1 text-xs text-ink-3">
        「超配额被拒」的可查证据：谁、哪条配额、当时的用量与上限。
      </p>
      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-ink-2">还没有被拒的记录。</p>
      ) : (
        <TableShell head={["时间", "账号", "配额", "当时用量", "上限"]}>
          {rows.map((row) => (
            <Row key={row.id}>
              <Cell className="text-ink-3">{dayStamp(row.at)}</Cell>
              <Cell>{row.email ?? "—"}</Cell>
              <Cell>{row.metric}</Cell>
              <Cell numeric>{count(row.used)}</Cell>
              <Cell numeric>{count(row.limit_value)}</Cell>
            </Row>
          ))}
        </TableShell>
      )}
    </section>
  );
}
