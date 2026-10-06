"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Cell, Row, TableShell } from "@/components/backtest/table";
import { api, describeError } from "@/lib/api";
import { count, dayStamp } from "@/lib/format";
import type { ThreadSummary } from "@/lib/types";

/**
 * 会话历史：与对话页侧栏同一个数据源（`GET /api/v1/chat/threads`），这里多给一列
 * 「最近活动」——它本来就是列表的排序依据，后端顺手带出来了。
 *
 * 「打开」走 `/?thread=<id>`：对话页消费掉这个参数后会把地址还原，所以从这儿跳过去
 * 之后再点侧栏切换会话不会被拽回来。
 */
export function ThreadsPanel() {
  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setThreads(await api.threads());
      setError(null);
    } catch (cause) {
      setError(describeError(cause, "会话列表加载失败：后端未启动或网络不通。"));
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

  if (threads === null) return <Skeleton />;

  if (threads.length === 0) {
    return (
      <p className="text-sm text-ink-2">
        还没有会话。
        <Link href="/" className="ml-1 text-brand hover:underline">
          去问一句
        </Link>
      </p>
    );
  }

  return (
    <TableShell head={["标题", "消息", "最近活动", ""]}>
      {threads.map((thread) => (
        <Row key={thread.thread_id}>
          <Cell className="max-w-0 truncate">{thread.title}</Cell>
          <Cell numeric className="text-ink-3">
            {count(thread.messages)}
          </Cell>
          <Cell numeric className="text-ink-3">
            {dayStamp(thread.last_active_at)}
          </Cell>
          <Cell className="text-right">
            <Link
              href={`/?thread=${thread.thread_id}`}
              className="rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-brand hover:bg-muted"
            >
              打开
            </Link>
          </Cell>
        </Row>
      ))}
    </TableShell>
  );
}

function Skeleton() {
  return (
    <div className="space-y-2">
      {[0, 1, 2].map((index) => (
        <div key={index} className="h-9 animate-pulse rounded-[var(--radius)] bg-muted" />
      ))}
    </div>
  );
}
