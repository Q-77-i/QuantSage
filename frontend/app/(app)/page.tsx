"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

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

  // nonce：同一个示例点两次也要重新灌入（只比字符串的话第二次不触发 effect）
  const [preset, setPreset] = useState<{ text: string; nonce: number } | null>(null);

  return (
    <>
      {/* 边界只包一个渲染 null 的子组件：生产构建下边界内整棵子树降级为 CSR，
          把整页包进去会让全高布局先塌一下（登录页同款写法，见其注释） */}
      <Suspense fallback={null}>
        <ThreadDeepLink onOpen={chat.openThread} />
      </Suspense>

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
            onPick={(text) => setPreset({ text, nonce: Date.now() })}
          />

          {chat.transportError && (
            <p className="border-t border-border px-4 py-2 text-xs text-destructive">
              <span className="mx-auto block max-w-3xl">{chat.transportError}</span>
            </p>
          )}

          <Composer
            disabled={chat.isStreaming}
            isStreaming={chat.isStreaming}
            preset={preset}
            onSend={chat.send}
            onStop={chat.stop}
          />
        </main>
      </div>
    </>
  );
}

/**
 * `/?thread=<id>`：个人空间的会话历史点进来时直接打开那条会话。
 *
 * 打开后**立刻把参数摘掉**（`router.replace("/")`）：留在地址栏的话，用户接着点侧栏
 * 里的另一个会话时，这个 effect 会拿旧 id 把他拽回去。摘掉之后地址栏回到「对话页」的
 * 常态——这一页本来就没有 URL 状态（发消息新建的会话号也不进地址栏）。
 */
function ThreadDeepLink({ onOpen }: { onOpen: (id: string) => void }) {
  const params = useSearchParams();
  const router = useRouter();

  useEffect(() => {
    const threadId = params.get("thread");
    if (!threadId) return;
    onOpen(threadId);
    router.replace("/");
  }, [params, router, onOpen]);

  return null;
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
