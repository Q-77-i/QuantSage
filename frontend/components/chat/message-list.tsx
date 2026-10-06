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
  onPick,
}: {
  messages: ChatMessage[];
  loading: boolean;
  error: string | null;
  /** 空态示例卡被点选：把问题灌进输入框（发不发由用户决定） */
  onPick: (prompt: string) => void;
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
        <Body messages={messages} loading={loading} error={error} onPick={onPick} />
      </div>
    </div>
  );
}

function Body({
  messages,
  loading,
  error,
  onPick,
}: {
  messages: ChatMessage[];
  loading: boolean;
  error: string | null;
  onPick: (prompt: string) => void;
}) {
  if (loading) return <Skeleton />;
  if (error) return <p className="py-8 text-sm text-destructive">{error}</p>;
  if (!messages.length) return <Empty onPick={onPick} />;

  return (
    <div className="divide-y divide-border">
      {messages.map((message) => (
        <MessageItem key={message.id} message={message} />
      ))}
    </div>
  );
}

/**
 * 空态引导：三条示例问题按能力分类（行情 / 对比 / 事件）。
 *
 * 三个问题都**照着本地数据的能力写**——示例点下去答不出来，比没有示例更伤。
 * 点选只灌进输入框并聚焦，不直接发送：发不发、要不要改，由用户决定。
 */
const EXAMPLES: { kind: string; prompt: string; note: string }[] = [
  {
    kind: "行情",
    prompt: "贵州茅台最近行情怎么样？",
    note: "行情 + 事件，带来源标注",
  },
  {
    kind: "对比",
    prompt: "对比宁德时代与招商银行近三个月的走势",
    note: "多标的取数叠加",
  },
  {
    kind: "事件",
    prompt: "最近有哪些影响银行板块的新闻？",
    note: "PIT 约束检索，可追溯时点",
  },
];

function Empty({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <div className="flex flex-1 flex-col justify-center py-10">
      <div className="text-center">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">
          问行情，查事件
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          回答的数据都带来源标注。可继续追问、切换会话，或前往回测页。
        </p>
      </div>

      <div className="mt-8 grid gap-3 sm:grid-cols-3">
        {EXAMPLES.map((example) => (
          <button
            key={example.prompt}
            type="button"
            onClick={() => onPick(example.prompt)}
            className="flex flex-col gap-1.5 rounded-[var(--radius)] border border-border bg-card px-4 py-3.5 text-left transition-colors hover:border-ring hover:bg-muted"
          >
            <span className="text-xs text-primary">{example.kind}</span>
            <span className="text-sm leading-snug text-foreground">{example.prompt}</span>
            <span className="mt-0.5 text-xs text-ink-3">{example.note}</span>
          </button>
        ))}
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
