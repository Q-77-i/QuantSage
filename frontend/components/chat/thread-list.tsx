"use client";

import { useState } from "react";

import type { ThreadSummary } from "@/lib/types";

/**
 * 会话列表。
 *
 * 删除做**内联二次确认**而不是 `window.confirm`：原生弹窗阻塞整页，样式也不跟随主题。
 * 每问一句就会新建一个会话，列表只增不减，删除是必需项而不是锦上添花。
 */
export function ThreadList({
  threads,
  activeId,
  disabled,
  onOpen,
  onDelete,
}: {
  threads: ThreadSummary[];
  activeId: string | null;
  disabled: boolean;
  onOpen: (threadId: string) => void;
  onDelete: (threadId: string) => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);

  if (!threads.length) {
    return <p className="px-1 text-xs text-muted-foreground">还没有会话</p>;
  }

  return (
    <ul className="space-y-0.5">
      {threads.map((thread) =>
        confirming === thread.thread_id ? (
          <li
            key={thread.thread_id}
            className="flex items-center gap-1 rounded-[var(--radius)] bg-muted px-2 py-1.5"
          >
            <span className="flex-1 truncate text-xs text-ink-2">删除这个会话？</span>
            <button
              type="button"
              onClick={() => {
                setConfirming(null);
                onDelete(thread.thread_id);
              }}
              className="shrink-0 rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-destructive hover:bg-card"
            >
              删除
            </button>
            <button
              type="button"
              onClick={() => setConfirming(null)}
              className="shrink-0 rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-2 hover:bg-card"
            >
              取消
            </button>
          </li>
        ) : (
          <li key={thread.thread_id} className="group/item relative">
            {/* pr-12 给删除按钮留位，否则标题会压到它下面 */}
            <button
              type="button"
              disabled={disabled}
              onClick={() => onOpen(thread.thread_id)}
              className={`w-full truncate rounded-[var(--radius)] py-1.5 pl-2 pr-12 text-left text-sm hover:bg-muted disabled:cursor-not-allowed ${
                activeId === thread.thread_id ? "bg-muted text-foreground" : "text-muted-foreground"
              }`}
            >
              {thread.title}
              <span className="num ml-2 text-xs text-ink-3">{thread.messages}</span>
            </button>

            <button
              type="button"
              disabled={disabled}
              onClick={() => setConfirming(thread.thread_id)}
              aria-label={`删除会话：${thread.title}`}
              // 用 visibility 而不是 opacity：隐藏时不占事件，免得误点看不见的按钮
              className="invisible absolute right-1.5 top-1/2 -translate-y-1/2 rounded-[var(--radius)] px-1.5 py-0.5 text-xs text-ink-3 hover:bg-card hover:text-destructive focus-visible:visible group-hover/item:visible disabled:cursor-not-allowed"
            >
              ✕
            </button>
          </li>
        ),
      )}
    </ul>
  );
}
