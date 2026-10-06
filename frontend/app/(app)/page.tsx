"use client";

import { Composer } from "@/components/chat/composer";
import { MessageList } from "@/components/chat/message-list";
import { ThreadList } from "@/components/chat/thread-list";
import { useChat, useThreads } from "@/components/chat/use-chat";
import { Button } from "@/components/ui/button";

/**
 * 对话页（T6b）。
 *
 * 状态机在 `lib/chat-state.ts`，与后端的接线在 `components/chat/use-chat.ts`，
 * 这里只负责排版与「哪些操作此刻不该可用」。页头与登录守卫在 `(app)/layout.tsx`。
 */
export default function ChatPage() {
  const { threads, error: threadsError, refresh } = useThreads();
  const chat = useChat(refresh);

  return (
    <>
      <div className="mx-auto flex h-[calc(100dvh-3.5rem)] max-w-[1400px]">
        <aside className="hidden w-60 shrink-0 flex-col border-r border-border p-3 md:flex">
          <Button
            variant="outline"
            size="sm"
            className="w-full"
            disabled={chat.isStreaming}
            onClick={chat.reset}
          >
            新会话
          </Button>

          <div className="mt-3 min-h-0 flex-1 overflow-y-auto">
            {threadsError ? (
              <p className="px-1 text-xs text-destructive">{threadsError}</p>
            ) : threads === null ? (
              <ThreadListSkeleton />
            ) : (
              <ThreadList
                threads={threads}
                activeId={chat.threadId}
                // 流式期间锁住切换与删除：点「停止」再操作是自然动作，
                // 比处理「半途换／删掉正在写的会话」那一堆竞态简单得多
                disabled={chat.isStreaming}
                onOpen={chat.openThread}
                onDelete={chat.removeThread}
              />
            )}
          </div>
        </aside>

        <main className="flex min-h-0 flex-1 flex-col">
          <MessageList
            messages={chat.messages}
            loading={chat.loading}
            error={chat.loadError}
          />

          {chat.transportError && (
            <p className="border-t border-border px-4 py-2 text-xs text-destructive">
              <span className="mx-auto block max-w-3xl">{chat.transportError}</span>
            </p>
          )}

          <Composer
            disabled={chat.isStreaming}
            isStreaming={chat.isStreaming}
            onSend={chat.send}
            onStop={chat.stop}
          />
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
