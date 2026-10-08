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

/** 轮询间隔：一次问答几秒到几十秒，2s 足够跟手 */
const POLL_INTERVAL_MS = 2000;
/** 轮询上限：服务端异常时不无限轮询（3 分钟足够覆盖最长的一次问答） */
const POLL_TIMEOUT_MS = 180_000;

export function useChat(onTurnEnd?: () => void) {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [threadId, setThreadId] = useState<string | null>(null);

  const threadIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const loadSeqRef = useRef(0);
  const onTurnEndRef = useRef(onTurnEnd);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    onTurnEndRef.current = onTurnEnd;
  });

  useEffect(
    () => () => {
      // 卸载即断流：后端**继续跑完那一轮**并落 checkpoint（断连不取消），重进会话就能看到
      abortRef.current?.abort();
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
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
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    abortRef.current?.abort();
    loadSeqRef.current += 1;
    threadIdRef.current = null;
    setThreadId(null);
    dispatch({ type: "reset" });
  }, []);

  /**
   * 「回答还在路上」时轮询历史，跑完自动出现。
   *
   * 场景：提问后刷新。前端断流、服务端继续跑（断连不再取消图），刷新那一刻回答还没落
   * checkpoint——不轮询的话用户得**再手动刷一次**才看得到。轮询间隔取 2s（一次问答几秒到
   * 几十秒，2s 足够跟手），并设上限，避免服务端异常时无限轮询。
   */
  const pollUntilDone = useCallback((id: string) => {
    if (pollRef.current !== null) window.clearInterval(pollRef.current);
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    pollRef.current = window.setInterval(() => {
      if (threadIdRef.current !== id || Date.now() > deadline) {
        if (pollRef.current !== null) window.clearInterval(pollRef.current);
        pollRef.current = null;
        return;
      }
      void api
        .threadMessages(id)
        .then((body) => {
          if (threadIdRef.current !== id) return;
          dispatch({ type: "loaded", messages: body.messages, running: body.running });
          if (!body.running && pollRef.current !== null) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
            onTurnEndRef.current?.(); // 跑完了：顺手刷一次侧栏（标题/排序会变）
          }
        })
        .catch(() => {
          if (pollRef.current !== null) window.clearInterval(pollRef.current);
          pollRef.current = null;
        });
    }, POLL_INTERVAL_MS);
  }, []);

  const openThread = useCallback(
    (id: string) => {
      abortRef.current?.abort();
      const seq = ++loadSeqRef.current; // 连点两个会话时，旧响应不得覆盖新响应
      threadIdRef.current = id;
      setThreadId(id);
      dispatch({ type: "loading" });

      api
        .threadMessages(id)
        .then((body) => {
          if (seq !== loadSeqRef.current) return;
          dispatch({ type: "loaded", messages: body.messages, running: body.running });
          if (body.running) pollUntilDone(id);
        })
        .catch((cause: unknown) => {
          if (seq !== loadSeqRef.current) return;
          dispatch({ type: "load_failed", message: describe(cause) });
        });
    },
    [pollUntilDone],
  );

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
    /** 完整状态也交出去：`canRetry(state)` 一类判据是纯函数，页面直接用，不必再摊一套字段 */
    state,
    messages: state.messages,
    /** 服务端正在为这个会话跑图（刷新断流后的那一轮） */
    running: state.running,
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
