"use client";

/**
 * 深路径运行的取数钩子（M8c）：**回看轮询** + **实时流**两条路，喂同一个展示层。
 *
 * 为什么不一律用 SSE：SSE 只在「你正在创建这次运行」时存在（POST 开了流才有），
 * 而回看/断连重连时没有流可挂——那种情形下 `GET /research/runs/{id}` 是唯一入口。
 * 所以：
 *   * **回看**：进页面拉一次详情；状态是 `running` 时按 2s 轮询到终态（运行记录本身就是
 *     过程真源，轮询它不会漏东西）；
 *   * **实时**：创建时用 `streamPost` 收帧，帧载荷与落库事件**同形**（服务端刻意如此），
 *     所以两条路能拼出同一种视图模型。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import { shouldPollRun } from "@/lib/agent-run";
import type { ApprovalPayload, RunDetail, RunNodeEvent, RunStatus } from "@/lib/agent-run";
import { streamPost } from "@/lib/sse";

const POLL_MS = 2000;

export function useRunDetail(id: string | null) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    if (!id) return null;
    const seq = ++seqRef.current;
    try {
      const data = (await api.researchRun(id)) as RunDetail;
      if (seq !== seqRef.current) return null;
      setDetail(data);
      setError(null);
      return data;
    } catch (cause) {
      if (seq !== seqRef.current) return null;
      setError(describeError(cause, "这次运行打不开，稍后重试"));
      return null;
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // 只在「还在跑」时轮询；到终态立刻停（报告是冻结产物，反复拉只会让人怀疑数在变）
  useEffect(() => {
    if (!detail || !shouldPollRun(detail.status)) return;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [detail, load]);

  return { detail, error, reload: load };
}

export interface LiveRun {
  runId: string | null;
  events: RunNodeEvent[];
  status: RunStatus;
  budget: Record<string, unknown>;
  reportId: string | null;
  error: string | null;
  running: boolean;
  /** `mode=auto` 判成快路径时服务端只回一帧 `route`——界面据此引导去对话页 */
  route: { path: string; reason: string } | null;
}

/** 创建时用：POST 开流，逐帧累积成与详情同形的视图模型。 */
export function useRunStream() {
  const [state, setState] = useState<LiveRun>({
    runId: null,
    events: [],
    status: "running",
    budget: {},
    reportId: null,
    error: null,
    running: false,
    route: null,
  });
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  /** 帧 → 状态。创建与恢复**共用同一条**（帧载荷同形，两处各写一遍必然漂移）。 */
  const applyFrame = useCallback((frame: { event: string; data: string }) => {
          let payload: Record<string, unknown>;
          try {
            payload = JSON.parse(frame.data) as Record<string, unknown>;
          } catch {
            return;
          }
          setState((prev) => {
            if (!mountedRef.current) return prev;
            if (frame.event === "node_start" || frame.event === "node_end") {
              // 帧载荷与落库事件同形（服务端刻意如此）——直接累积即可
              return { ...prev, events: [...prev.events, payload as unknown as RunNodeEvent] };
            }
            if (frame.event === "interrupt") {
              // 帧与落库事件同形：`{phase, at, payload}`——**载荷在 `payload.payload` 里**。
              // 取错层不会报错，只会让 `data-approval` 变成 undefined（React 不渲染该属性），
              // 界面上看起来就是「卡片没出现」（真逮过一次）。
              const approval = (payload.payload ?? null) as ApprovalPayload | null;
              if (!approval) return prev;
              return {
                ...prev,
                status: "awaiting_approval",
                events: [...prev.events, { phase: "interrupt", at: Number(payload.at ?? 0), payload: approval }],
              };
            }
            if (frame.event === "route") {
              return {
                ...prev,
                runId: null,
                route: {
                  path: String(payload.path ?? ""),
                  reason: String(payload.reason ?? ""),
                },
              };
            }
            if (frame.event === "report") {
              return { ...prev, reportId: String(payload.report_id ?? "") || null };
            }
            if (frame.event === "done") {
              const status = String(payload.status ?? "done") as RunStatus;
              return {
                ...prev,
                status,
                running: status === "running",
                budget: (payload.budget as Record<string, unknown>) ?? prev.budget,
                reportId: (payload.report_id as string) ?? prev.reportId,
              };
            }
            if (frame.event === "error") {
              return { ...prev, error: String(payload.message ?? "运行失败") };
            }
            return prev;
          });
  }, []);

  const drive = useCallback(
    async (path: string, body: Record<string, unknown>) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        await streamPost(path, body, {
          signal: controller.signal,
          onResponse: (response) => {
            const runId = response.headers.get("X-Run-Id");
            if (runId && mountedRef.current) setState((prev) => ({ ...prev, runId }));
          },
          onFrame: applyFrame,
        });
      } catch (cause) {
        if (!controller.signal.aborted && mountedRef.current) {
          setState((prev) => ({ ...prev, error: describeError(cause, "这条流断了"), running: false }));
        }
      } finally {
        if (mountedRef.current) {
          setState((prev) => (prev.running ? { ...prev, running: false } : prev));
        }
      }
    },
    [applyFrame],
  );

  const start = useCallback(
    async (body: Record<string, unknown>) => {
      setState((prev) => ({
        ...prev, events: [], status: "running", error: null, running: true, reportId: null, route: null, runId: null,
      }));
      await drive("/api/v1/research/runs", body);
    },
    [drive],
  );

  /** HITL 恢复：**在同一条视图里接着跑**——跑着跑着被拦下，用户当然要当场决定。 */
  const resume = useCallback(
    async (runId: string, choice: "continue" | "stop") => {
      setState((prev) => ({
        ...prev,
        runId,
        status: "running",
        running: true,
        error: null,
        // 已经做过的决定从事件流里去掉，避免恢复后还挂着一张旧审批卡
        events: prev.events.filter((event) => event.phase !== "interrupt"),
      }));
      await drive(`/api/v1/research/runs/${runId}/resume`, { choice });
    },
    [drive],
  );

  return { ...state, start, resume };
}

/** HITL 恢复：同样开一条流，帧与创建时同形。 */
export async function resumeRun(
  runId: string,
  choice: "continue" | "stop",
  onFrame: (frame: { event: string; data: string }) => void,
): Promise<void> {
  await streamPost(`/api/v1/research/runs/${runId}/resume`, { choice }, { onFrame });
}
