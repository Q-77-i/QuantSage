"use client";

import { useEffect, useState } from "react";

import { AppHeader } from "@/components/app-header";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import type { ThreadSummary } from "@/lib/types";

/**
 * 对话页骨架（T6a）。
 *
 * 本阶段只把「壳 + 数据通路」立起来：会话列表走真实接口，消息流与工具步骤留 T6b。
 * 未实现的交互一律 `disabled`，不做「看起来能用但点了没反应」的假按钮。
 */
export default function ChatPage() {
  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .threads()
      .then((rows) => alive && setThreads(rows))
      .catch((cause: unknown) => {
        if (!alive) return;
        setError(cause instanceof ApiError ? cause.message : "会话列表加载失败");
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <>
      <AppHeader />
      <div className="mx-auto flex h-[calc(100dvh-3.5rem)] max-w-[1400px]">
        <aside className="hidden w-60 shrink-0 border-r border-border p-3 md:block">
          <Button variant="outline" size="sm" className="w-full" disabled>
            新会话
          </Button>

          <div className="mt-3">
            {error ? (
              <p className="text-xs text-destructive">{error}</p>
            ) : threads === null ? (
              <ThreadListSkeleton />
            ) : threads.length === 0 ? (
              <p className="px-1 text-xs text-muted-foreground">还没有会话</p>
            ) : (
              <ul className="space-y-0.5">
                {threads.map((thread) => (
                  <li key={thread.thread_id}>
                    <button
                      type="button"
                      disabled
                      title="历史会话回看待定，见 docs/private/待办与灵感.md"
                      className="w-full truncate rounded-[var(--radius)] px-2 py-1.5 text-left text-sm text-muted-foreground"
                    >
                      {thread.title}
                      <span className="num ml-2 text-xs text-ink-3">{thread.messages}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>

        <main className="flex flex-1 flex-col">
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="max-w-md text-center">
              <h1 className="font-heading text-xl">问行情，查事件</h1>
              <p className="mt-2 text-sm text-muted-foreground">
                例如「贵州茅台最近行情怎么样」。回答的数据都带来源标注。
              </p>
            </div>
          </div>

          <div className="border-t border-border p-3">
            <div className="flex items-center gap-2">
              <input
                disabled
                placeholder="对话功能开发中"
                className="h-9 flex-1 rounded-[var(--radius)] border border-border bg-card px-3 text-sm outline-none focus-visible:border-ring"
              />
              <Button disabled>发送</Button>
            </div>
          </div>
        </main>
      </div>
    </>
  );
}

function ThreadListSkeleton() {
  return (
    <ul className="space-y-1.5">
      {[0, 1, 2].map((index) => (
        <li key={index} className="h-7 animate-pulse rounded-[var(--radius)] bg-muted" />
      ))}
    </ul>
  );
}
