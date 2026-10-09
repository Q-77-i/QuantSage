"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError, streamOptimize } from "@/lib/api";
import type {
  BatchRequest,
  GridRequest,
  OptimizeCell,
  OptimizeDoneFrame,
  OptimizeRunDetail,
  OptimizeStartFrame,
} from "@/lib/types";

/**
 * 跑一次网格 / 批量：SSE 逐格填充。
 *
 * 三条与回测页不同的地方，都有出处：
 *
 * 1. **格按 `index` 落位**，不是按到达顺序 append——完成次序是乱的（并发 2），
 *    而热力图与批量表的行列由请求定义，顺序错了图就串位；
 * 2. **断线即断**：`stop()` 掉 AbortController 后本地状态**清空**，不保留半份结果。
 *    SPEC §6 M5b 定死「刷新等于重跑、不做补偿」——留半份会让人以为那就是结果；
 * 3. **收尾的 `done` 帧带 `run_id`**：地址栏随即换成 `?run=<id>`，刷新/分享能回到这一份。
 *
 * 重开（`?run=`）走 `api.optimizeRun`，**不重跑**：结果必须与当初一致。
 */
export function useOptimize() {
  const [running, setRunning] = useState(false);
  const [started, setStarted] = useState<OptimizeStartFrame | null>(null);
  const [cells, setCells] = useState<OptimizeCell[]>([]);
  const [done, setDone] = useState<OptimizeDoneFrame | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);

  const reset = useCallback(() => {
    setStarted(null);
    setCells([]);
    setDone(null);
    setError(null);
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    seqRef.current += 1; // 作废在途回调
    setRunning(false);
    reset();
  }, [reset]);

  const run = useCallback(
    (kind: "grid" | "batch", body: GridRequest | BatchRequest) => {
      const seq = ++seqRef.current;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setRunning(true);
      setError(null);
      setDone(null);
      setStarted(null);
      setCells([]);
      setRunId(null);

      // 逐格落位：`index` 是服务端给的槽位，不许按到达顺序排
      const slots: OptimizeCell[] = [];

      void streamOptimize(kind, body, {
        signal: controller.signal,
        onFrame: (frame) => {
          if (seq !== seqRef.current) return;
          if (frame.event === "start") {
            const payload = JSON.parse(frame.data) as OptimizeStartFrame;
            slots.length = payload.total;
            setStarted(payload);
          } else if (frame.event === "cell") {
            const cell = JSON.parse(frame.data) as OptimizeCell;
            slots[cell.index] = cell;
            setCells([...slots]);
          } else if (frame.event === "done") {
            const payload = JSON.parse(frame.data) as OptimizeDoneFrame;
            setDone(payload);
            // 落库是在服务端发 `done` 之前做的，这一帧一到就说明记录已经在了
            setRunId(payload.run_id);
          } else if (frame.event === "error") {
            const payload = JSON.parse(frame.data) as { message?: string };
            setError(payload.message ?? "运行中断");
          }
        },
      })
        .catch((cause: unknown) => {
          if (seq !== seqRef.current) return;
          // 用户主动中止不是错误（断线即断是明确的产品行为）
          if (controller.signal.aborted) return;
          setError(describeError(cause, "运行失败：后端未启动或网络不通。"));
        })
        .finally(() => {
          if (seq === seqRef.current) setRunning(false);
        });
    },
    [],
  );

  /** 重开一次优化：直接吃存下来的 `request` + `summary`，不重跑 */
  const loadRun = useCallback(
    (id: string): Promise<OptimizeRunDetail | null> => {
      const seq = ++seqRef.current;
      abortRef.current?.abort();
      setRunning(false);
      setError(null);

      return api
        .optimizeRun(id)
        .then((detail) => {
          if (seq !== seqRef.current) return null;
          setStarted(startFrameOf(detail));
          setCells(detail.summary.cells);
          setDone({
            run_id: detail.id,
            kind: detail.summary.kind,
            cells_total: detail.summary.cells_total,
            cells_ok: detail.summary.cells_ok,
            best_index: detail.summary.best_index,
            overfit: detail.summary.overfit,
            duration_s: detail.summary.duration_s,
          });
          setRunId(detail.id);
          return detail;
        })
        .catch((cause: unknown) => {
          if (seq !== seqRef.current) return null;
          setError(describeError(cause, "优化记录加载失败：后端未启动或网络不通。"));
          return null;
        });
    },
    [],
  );

  // 离开页面时掐断在途的流：后端据此停止派发新格（SPEC §6 M5b 的断线语义）
  useEffect(() => () => abortRef.current?.abort(), []);

  return { running, started, cells, done, error, runId, run, stop, loadRun, reset };
}

/**
 * 重开的记录 → `start` 帧。
 *
 * 存的 `request` 与跑时的 `start` 帧**不是同一个形状**（前者是请求体、后者带
 * 解析后的窗口与展示名），这里把它们对齐到渲染层真正要用的那几个字段：
 * 总格数取 `cells_total`（**不是 `cells.length`**——有格失败时后者会少）。
 */
function startFrameOf(detail: OptimizeRunDetail): OptimizeStartFrame {
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
