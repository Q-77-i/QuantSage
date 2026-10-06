"use client";

import { memo } from "react";

import { Markdown } from "@/components/chat/markdown";
import { ToolSteps } from "@/components/chat/tool-steps";
import type { ChatMessage } from "@/lib/chat-state";

/**
 * 单条消息。
 *
 * `memo` 是流式性能的关键：状态机保证未触碰的消息对象引用不变，于是每个 token
 * 只重渲正在生成的那一条，而不是整条会话。
 */
export const MessageItem = memo(function MessageItem({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  return (
    <article className="py-3">
      <div className="mb-1.5 text-xs text-ink-3">{isUser ? "我" : "知策"}</div>

      {!isUser && <ToolSteps tools={message.tools} />}

      {isUser ? (
        <p className="whitespace-pre-wrap text-sm">{message.content}</p>
      ) : (
        <>
          {message.content ? (
            <Markdown>{message.content}</Markdown>
          ) : message.status === "streaming" ? (
            // 还没吐字时给个动静，之后文本自己在长，不需要额外指示器
            <span className="block h-4 w-24 animate-pulse rounded-[var(--radius)] bg-muted" />
          ) : null}

          {message.error && (
            <p className="mt-2 border-l-2 border-destructive pl-2 text-xs text-destructive">
              {message.error}
            </p>
          )}

          {message.status === "stopped" && !message.error && (
            <p className="mt-1 text-xs text-ink-3">已停止</p>
          )}
        </>
      )}
    </article>
  );
});
