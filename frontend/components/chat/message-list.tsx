"use client";

import { useEffect, useRef } from "react";

import { MessageItem } from "@/components/chat/message-item";
import type { ChatMessage } from "@/lib/chat-state";

/**
 * 消息流。
 *
 * 滚动自己管：只有用户本来就在底部时才跟着新内容走——正在往上翻旧内容时被
 * 拽回底部，是聊天界面最烦人的行为之一。
 */
export function MessageList({
  messages,
  loading,
  error,
}: {
  messages: ChatMessage[];
  loading: boolean;
  error: string | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  useEffect(() => {
    const element = containerRef.current;
    if (element && stickRef.current) element.scrollTop = element.scrollHeight;
  }, [messages]);

  // 刚切过来的会话默认贴底——用户在**上一个**会话里上滑过，不该影响这一个
  useEffect(() => {
    if (loading) return;
    stickRef.current = true;
    const element = containerRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [loading]);

  function handleScroll() {
    const element = containerRef.current;
    if (!element) return;
    stickRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
  }

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="min-h-0 flex-1 overflow-y-auto px-4"
    >
      {/* min-h-full + flex-col 让空态能相对**消息区**垂直居中；
          用 50vh 的话居中位置会随视口高度飘，缩小窗口时尤其明显 */}
      <div className="mx-auto flex min-h-full max-w-3xl flex-col">
        <Body messages={messages} loading={loading} error={error} />
      </div>
    </div>
  );
}

function Body({
  messages,
  loading,
  error,
}: {
  messages: ChatMessage[];
  loading: boolean;
  error: string | null;
}) {
  if (loading) return <Skeleton />;
  if (error) return <p className="py-8 text-sm text-destructive">{error}</p>;
  if (!messages.length) return <Empty />;

  return (
    <div className="divide-y divide-border">
      {messages.map((message) => (
        <MessageItem key={message.id} message={message} />
      ))}
    </div>
  );
}

function Empty() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="max-w-md text-center">
        <h1 className="font-heading text-xl font-semibold">问行情，查事件</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          例如「贵州茅台最近行情怎么样」。回答的数据都带来源标注。
        </p>
      </div>
    </div>
  );
}

/** 与最终布局同形的骨架屏，不用居中转圈 */
function Skeleton() {
  return (
    <div className="space-y-6 py-6">
      {[0, 1].map((group) => (
        <div key={group} className="space-y-2">
          <div className="h-3 w-16 animate-pulse rounded-[var(--radius)] bg-muted" />
          <div className="h-4 w-3/5 animate-pulse rounded-[var(--radius)] bg-muted" />
          <div className="h-4 w-2/5 animate-pulse rounded-[var(--radius)] bg-muted" />
        </div>
      ))}
    </div>
  );
}
