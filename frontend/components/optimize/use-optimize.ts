"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, describeError, streamTask, submitOptimize } from "@/lib/api";
import { SubmissionKey } from "@/lib/idempotency";
import {
  applyFrame,
  emptyStream,
  markAttached,
  markDetached,
  startStream,
} from "@/lib/optimize-stream";
import type { OptimizeStreamState } from "@/lib/optimize-stream";
import type { BatchRequest, GridRequest, OptimizeRunDetail } from "@/lib/types";

/**
 * 跑一次网格 / 批量：**提交 → 订阅**（P3-E1b）。
 *
 * 与 M5b 的三处不同，都因为执行搬进了 worker：
 *
 * 1. **提交与订阅分开**：`POST /optimize/*` 只回 202 + 任务号，进度由
 *    `GET /tasks/{id}/events` 订阅。帧形状与旧流逐字同形（同一份 `task_frames`），
 *    所以消费逻辑没变——**变的只是「流断了不再等于白跑」**；
 * 2. **断线保留已收到的格**：`detached` 状态只说「仍在后台跑」，不清空——留半份结果会让人
 *    以为那就是结果，但**清空**会让人以为白跑了（M5b 的旧行为），两者都错。真相是：
 *    格是真的，任务还在跑；
 * 3. **`stop()` 是真取消**（`POST /tasks/{id}/cancel`）：协作式中止，已派发的格跑完，
 *    结果**不落库**——界面据此说「已取消（未落库）」。
 *
 * 重开（`?run=`）走 `api.optimizeRun`，**不重跑**：结果必须与当初一致。
 */
export function useOptimize() {
  const [running, setRunning] = useState(false);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [stream, setStream] = useState<OptimizeStreamState>(emptyStream);
  const [runId, setRunId] = useState<string | null>(null);
  const seqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const keyRef = useRef(new SubmissionKey());
  /** 上一单落定了没有：它决定下一次提交是「同一次动作的重试」还是「新动作」 */
  const runningRef = useRef(false);

  /** 订阅一条任务流。发起与「继续查看」共用——同一份帧，同一条路。 */
  const subscribe = useCallback((id: string, seq: number) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    void streamTask(id, {
      signal: controller.signal,
      onFrame: (frame) => {
        if (seq !== seqRef.current) return;
        setStream((prev) => {
          const next = applyFrame(prev, frame);
          if (next.done) setRunId(next.done.run_id);
          // 收到终态帧才算这一单落定——**断线不算**（任务还在后台跑，
          // 那时再点「运行」应当复用同一个键把同一件事接回来，而不是又派一单）
          if (next.done) runningRef.current = false;
          return next;
        });
      },
    })
      .catch((cause: unknown) => {
        if (seq !== seqRef.current) return;
        if (controller.signal.aborted) return; // 用户主动中止：不是错误
        // 流断了：**任务仍在后台跑**（E1b）——如实说，别清空
        setStream((prev) => markDetached(prev));
        setRunning(false);
        void cause;
      })
      .finally(() => {
        if (seq === seqRef.current) setRunning(false);
      });
  }, []);

  const run = useCallback(
    (kind: "grid" | "batch", body: GridRequest | BatchRequest) => {
      const seq = ++seqRef.current;
      // 幂等键（E4b）：任务还在跑时再点「运行」是**同一次动作**（复用键，后端只跑一遍）；
      // 上一单落定之后再点是新动作（换键——用户就是想再跑一遍）
      const key = keyRef.current.current(runningRef.current);
      setRunning(true);
      runningRef.current = true;
      setStream(startStream());
      setRunId(null);

      void (async () => {
        try {
          const { task_id } = await submitOptimize(kind, body, key);
          if (seq !== seqRef.current) return;
          setTaskId(task_id);
          subscribe(task_id, seq);
        } catch (cause) {
          if (seq !== seqRef.current) return;
          setRunning(false);
          setStream((prev) => ({
            ...prev,
            error: describeError(cause, "运行失败：后端未启动或网络不通。"),
          }));
        }
      })();
    },
    [subscribe],
  );

  /**
   * 续看：重新订阅（回放补齐断线期间跑完的格）。
   *
   * `id` 可传可不传：页刷新后 hook 是全新的（`taskId` 为空），那时从 `?task=` 把号带进来。
   */
  const reattach = useCallback(
    (id?: string) => {
      const target = id ?? taskId;
      if (!target) return;
      const seq = ++seqRef.current;
      setTaskId(target);
      setStream((prev) => markAttached(prev));
      setRunning(true);
      subscribe(target, seq);
    },
    [subscribe, taskId],
  );

  /** 中止：**真取消任务**（协作式），不是只掐掉流 */
  const stop = useCallback(() => {
    const id = taskId;
    abortRef.current?.abort();
    abortRef.current = null;
    seqRef.current += 1; // 作废在途回调
    setRunning(false);
    if (!id) return;
    void api
      .cancelTask(id)
      .then(() => setStream((prev) => ({ ...prev, cancelled: true, detached: false })))
      .catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.status === 409) {
          // 409 = 点下去那一刻它已经跑完了（窗口很小但真实存在）：**把订阅接回来让结果出场**，
          // 比甩一句「取消失败」有用得多
          reattach(id);
          return;
        }
        setStream((prev) => ({ ...prev, error: describeError(cause, "中止没能送达。") }));
      });
  }, [reattach, taskId]);

  /** 重开一次优化：直接吃存下来的 `request` + `summary`，不重跑 */
  const loadRun = useCallback((id: string): Promise<OptimizeRunDetail | null> => {
    const seq = ++seqRef.current;
    abortRef.current?.abort();
    setRunning(false);
    setTaskId(null);

    return api
      .optimizeRun(id)
      .then((detail) => {
        if (seq !== seqRef.current) return null;
        setStream({
          started: startFrameOf(detail),
          cells: detail.summary.cells,
          done: {
            run_id: detail.id,
            kind: detail.summary.kind,
            cells_total: detail.summary.cells_total,
            cells_ok: detail.summary.cells_ok,
            best_index: detail.summary.best_index,
            overfit: detail.summary.overfit,
            duration_s: detail.summary.duration_s,
          },
          error: null,
          cancelled: false,
          detached: false,
        });
        setRunId(detail.id);
        return detail;
      })
      .catch((cause: unknown) => {
        if (seq !== seqRef.current) return null;
        setStream((prev) => ({
          ...prev,
          error: describeError(cause, "优化记录加载失败：后端未启动或网络不通。"),
        }));
        return null;
      });
  }, []);

  // 离开页面只掐订阅，**不取消任务**（E1b：执行不认识连接）——回来还能续看
  useEffect(() => () => abortRef.current?.abort(), []);

  return {
    running,
    taskId,
    started: stream.started,
    cells: stream.cells,
    done: stream.done,
    error: stream.error,
    cancelled: stream.cancelled,
    detached: stream.detached,
    runId,
    run,
    stop,
    reattach,
    loadRun,
  };
}

/**
 * 重开的记录 → `start` 帧。
 *
 * 存的 `request` 与跑时的 `start` 帧**不是同一个形状**（前者是请求体、后者带
 * 解析后的窗口与展示名），这里把它们对齐到渲染层真正要用的那几个字段：
 * 总格数取 `cells_total`（**不是 `cells.length`**——有格失败时后者会少）。
 */
function startFrameOf(detail: OptimizeRunDetail) {
  const { request, summary } = detail;
  return {
    kind: summary.kind,
    total: summary.cells_total,
    symbol: request.symbol,
    strategy: request.strategy,
    strategy_name: request.strategy_name ?? null,
    symbols: request.symbols,
    strategies: request.strategies?.map((item) => ({
      strategy: item.strategy,
      strategy_name: null,
    })),
    axes: request.axes,
    window: summary.window
      ? { start: summary.window.start ?? "", end: summary.window.end ?? "" }
      : undefined,
    pit_mode: request.pit_mode,
  };
}
