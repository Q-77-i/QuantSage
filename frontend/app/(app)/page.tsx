"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { Composer } from "@/components/chat/composer";
import { MessageList } from "@/components/chat/message-list";
import { ThreadList } from "@/components/chat/thread-list";
import { useChat, useThreads } from "@/components/chat/use-chat";
import { canRetry } from "@/lib/chat-state";
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

  // 「上一轮没拿到回答」才给「重新生成」（判据与理由在 lib/chat-state.ts 的 canRetry）
  const retryable = canRetry(chat.state);
  const lastMessage = chat.messages.at(-1);

  return (
    <>
      {/* 边界只包一个渲染 null 的子组件：生产构建下边界内整棵子树降级为 CSR，
          把整页包进去会让全高布局先塌一下（登录页同款写法，见其注释） */}
      <Suspense fallback={null}>
        <ThreadUrlSync threadId={chat.threadId} onOpen={chat.openThread} />
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

          {chat.running ? (
            // 刷新断流后那一轮还在服务端跑：说清状态并自动跟上（轮询在 use-chat 里），
            // 别让用户以为卡住了又手动刷一次
            <p className="border-t border-border px-4 py-2 text-xs text-ink-3">
              <span className="mx-auto block max-w-3xl">回答还在生成中，完成后会自动出现。</span>
            </p>
          ) : null}

          {retryable && lastMessage ? (
            // 上一次提问没拿到回答（刷新断流、模型报错、配额用尽都会这样）：
            // 会话不会自己恢复，给一个重发入口，别让人重新打字
            <div className="border-t border-border px-4 py-2">
              <div className="mx-auto flex max-w-3xl items-center gap-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => chat.send(lastMessage.content)}
                >
                  重新生成
                </Button>
                <span className="text-xs text-ink-3">
                  上一次提问没有拿到回答。重发会在这个会话里再记一轮。
                </span>
              </div>
            </div>
          ) : null}

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
 * `/?thread=<id>` 与「当前打开的会话」**双向同步**（2026-10-09 改，用户反馈刷新丢会话）。
 *
 * 原来的做法是「深链用完即摘」（`router.replace("/")`）：参数留在地址栏时，用户接着点侧栏
 * 里的另一个会话会被旧 id 拽回去。代价是**刷新后回不到原位**——地址栏永远是 `/`，一刷新就
 * 落到欢迎页。现在改成两边都写：
 *
 *   * 状态 → URL：当前会话号进地址栏（发消息新建的会话号也进），刷新才回得到原位；
 *   * URL → 状态：地址栏带了别的会话号就打开它（深链、浏览器前进后退都走这条）。
 *
 * 互相触发用「最近一次已同步的号」去重（两边都写它）：侧栏切会话时先动状态、URL 随后跟上，
 * 那时 URL→状态 一看 `param === synced` 就不再动作——原来那个「被拽回去」的担忧由它解决，
 * 不必再靠摘参数。
 */
function ThreadUrlSync({
  threadId,
  onOpen,
}: {
  threadId: string | null;
  onOpen: (id: string) => void;
}) {
  const params = useSearchParams();
  const router = useRouter();
  const param = params.get("thread");
  const synced = useRef<string | null>(null);

  useEffect(() => {
    if (!param || param === synced.current) return;
    synced.current = param;
    onOpen(param);
  }, [param, onOpen]);

  useEffect(() => {
    if (threadId === synced.current) return;
    synced.current = threadId;
    // `replace` 不留历史：连点几个会话不该在历史里堆一串（前进后退仍可按 URL 变化生效）
    router.replace(threadId ? `/?thread=${threadId}` : "/", { scroll: false });
  }, [threadId, router]);

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
