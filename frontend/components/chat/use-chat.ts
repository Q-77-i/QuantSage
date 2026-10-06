"use client";

/**
 * 对话页的 React 粘合层：状态机在 `lib/chat-state.ts`，这里只管「什么时候派发」。
 *
 * 两处必须用 ref 而不是 state：
 *   * `busyRef` 防连点——`dispatch` 之后读到的 state 还是旧值，只有 ref 是同步的；
 *   * `threadIdRef` 供发送时读取——闭包里的 state 会滞后一轮。
 */

import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { chatReducer, initialChatState } from "@/lib/chat-state";
import { streamChat } from "@/lib/sse";
import type { ThreadSummary } from "@/lib/types";

function describe(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  return "连接失败，请确认后端已启动";
}

export function useThreads() {
  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api
      .threads()
      .then((rows) => {
        setThreads(rows);
        setError(null);
      })
      .catch((cause: unknown) => setError(describe(cause)));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { threads, error, refresh };
}

export function useChat(onTurnEnd?: () => void) {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [threadId, setThreadId] = useState<string | null>(null);

  const threadIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const loadSeqRef = useRef(0);
  const onTurnEndRef = useRef(onTurnEnd);

  useEffect(() => {
    onTurnEndRef.current = onTurnEnd;
  });

  useEffect(
    () => () => {
      // 卸载即断流：后端的 finally 会取消图，不让它白跑
      abortRef.current?.abort();
      loadSeqRef.current += 1;
    },
    [],
  );

  const send = useCallback((text: string) => {
    const message = text.trim();
    if (!message || busyRef.current) return;
    busyRef.current = true;

    const controller = new AbortController();
    abortRef.current = controller;
    dispatch({ type: "sent", text: message });

    void streamChat({
      message,
      threadId: threadIdRef.current,
      signal: controller.signal,
      onFrame: (frame) => dispatch({ type: "frame", frame }),
      onThreadId: (id) => {
        threadIdRef.current = id;
        setThreadId(id);
      },
    })
      .then(() => dispatch({ type: "ended" }))
      .catch((cause: unknown) => {
        dispatch(
          controller.signal.aborted
            ? { type: "stopped" }
            : { type: "failed", message: describe(cause) },
        );
      })
      .finally(() => {
        busyRef.current = false;
        abortRef.current = null;
        // done / 停止 / 失败都要刷一次：三种情形下 checkpoint 都可能已落盘
        onTurnEndRef.current?.();
      });
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    loadSeqRef.current += 1;
    threadIdRef.current = null;
    setThreadId(null);
    dispatch({ type: "reset" });
  }, []);

  const openThread = useCallback((id: string) => {
    abortRef.current?.abort();
    const seq = ++loadSeqRef.current; // 连点两个会话时，旧响应不得覆盖新响应
    threadIdRef.current = id;
    setThreadId(id);
    dispatch({ type: "loading" });

    api
      .threadMessages(id)
      .then((body) => {
        if (seq !== loadSeqRef.current) return;
        dispatch({ type: "loaded", messages: body.messages });
      })
      .catch((cause: unknown) => {
        if (seq !== loadSeqRef.current) return;
        dispatch({ type: "load_failed", message: describe(cause) });
      });
  }, []);

  const removeThread = useCallback((id: string) => {
    api
      .deleteThread(id)
      .then(() => {
        if (threadIdRef.current === id) {
          // 删的正是当前打开的那个：主区回到空态，别留着一份已经不在服务端的会话
          threadIdRef.current = null;
          setThreadId(null);
          dispatch({ type: "reset" });
        }
        onTurnEndRef.current?.();
      })
      .catch((cause: unknown) => dispatch({ type: "failed", message: describe(cause) }));
  }, []);

  return {
    messages: state.messages,
    isStreaming: state.streamingId !== null,
    loading: state.loading,
    transportError: state.transportError,
    loadError: state.loadError,
    threadId,
    send,
    stop,
    reset,
    openThread,
    removeThread,
  };
}
