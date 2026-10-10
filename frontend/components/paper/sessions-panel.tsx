"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { api, describeError } from "@/lib/api";
import { strategyLabel } from "@/lib/backtest-form";
import { amount, dayStamp } from "@/lib/format";
import type { PaperAccountSummary } from "@/lib/types";

/**
 * 我的模拟盘（个人空间的第六个页签）：会话列表，点进去回到那一份账户与决策流水。
 *
 * 与「我的回测」同一个道理：列表只列摘要，「点开」才取整份详情。
 */
export function PaperPanel() {
  const [sessions, setSessions] = useState<PaperAccountSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSessions(await api.paperAccounts());
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "模拟盘会话加载失败：后端未启动或网络不通。"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {error}
      </p>
    );
  }
  if (!sessions) {
    return <div className="mt-3 h-24 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />;
  }
  if (!sessions.length) {
    return (
      <p className="rounded-[var(--radius)] border border-dashed border-border px-4 py-8 text-center text-sm text-ink-2">
        还没有模拟盘会话。
        <Link href="/paper" className="ml-1 text-brand hover:underline">
          去建一个
        </Link>
      </p>
    );
  }

  return (
    <TableShell head={["会话", "策略", "标的池", "区间", "模拟到", "现金", "状态", ""]}>
      {sessions.map((session) => (
        <Row key={session.id}>
          <Cell className="font-medium">{session.name}</Cell>
          <Cell className="text-ink-2">
            {session.strategy_name ?? strategyLabel(session.strategy)}
          </Cell>
          <Cell className="num text-ink-2">{session.symbols.length} 只</Cell>
          <Cell className="num whitespace-nowrap text-ink-2">
            {dayStamp(session.start)} → {dayStamp(session.end)}
          </Cell>
          <Cell className="num whitespace-nowrap">{dayStamp(session.as_of)}</Cell>
          <Cell numeric>{amount(session.cash)}</Cell>
          <Cell className="text-ink-2">
            {session.status === "finished" ? "已跑到末端" : "进行中"}
          </Cell>
          <Cell>
            <Link href={`/paper?id=${session.id}`} className="text-brand hover:underline">
              打开
            </Link>
          </Cell>
        </Row>
      ))}
    </TableShell>
  );
}
