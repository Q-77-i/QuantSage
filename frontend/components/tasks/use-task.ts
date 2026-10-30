"use client";

/**
 * 提交一次回测并轮询到终态（P3-E1a）。
 *
 * 端点改成「提交 → 202 → 轮询」之后，三个入口（回测页、工作台「保存并运行」、优化页
 * 「点一格重跑」）走的是同一条路：提交拿 `task_id` → 轮询 `GET /tasks/{id}` → 终态里取
 * `run_id`。这一段只写一次，三处共用——三份手写副本的代价，本仓在 SSE 帧编码上已经付过。
 *
 * **两类失败不要混**（这是异步化后最容易漏的一条）：
 *   * 提交期 4xx（归属 404 / 静态检查 422 + findings / 参数 422）→ `ApiError.payload`，
 *     与同步时代逐字同形，`readRunFailure` 直接吃；
 *   * 执行期失败（沙箱配额、用户代码异常）→ 任务的 `error`，形状与前者一致。
 * 两者都收进 `failure`，调用方不必分情况渲染。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api, describeError } from "@/lib/api";
import { SubmissionKey } from "@/lib/idempotency";
import { readRunFailure } from "@/lib/strategy-form";
import type { RunFailure } from "@/lib/strategy-form";
import { pollTask, taskFailure, taskPhaseLabel, taskRunId } from "@/lib/task";
import type { BacktestRequest, TaskDetail } from "@/lib/types";

export interface BacktestTaskState {
  running: boolean;
  /** 「排队中」/「执行中」；不在跑时为 null */
  phase: string | null;
}

/**
 * 提交结果**直接返回**，不靠 state 回读。
 *
 * 理由不是风格：`await submit(...)` 之后读 hook 返回对象上的 `failure` 拿到的是**本次渲染的
 * 快照**（React 的 setState 是排队的），调用方会读到上一次的值——那种错只在连点两次时才
 * 显形，是最难查的一类。
 */
export type TaskSubmitResult =
  | { ok: true; runId: string }
  | { ok: false; failure: RunFailure };

const IDLE: BacktestTaskState = { running: false, phase: null };

export function useBacktestTask() {
  const [state, setState] = useState<BacktestTaskState>(IDLE);
  /** 序号戳：连点「运行」时，先发的那次不许覆盖后发的状态（与 `use-backtest.ts` 同法） */
  const seqRef = useRef(0);
  /** 幂等键（E4b）：同一个动作在跑时再点，复用同一个键——后端只跑一遍 */
  const keyRef = useRef(new SubmissionKey());
  /** 上一次提交落定了没有。它决定下一次提交是「同一次动作的重试」还是「新动作」 */
  const inFlightRef = useRef(false);
  /** 卸载/新提交时中止上一次轮询（P2）：离开页面后不再取数、不再写状态 */
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      seqRef.current += 1; // 让在飞的 submit 认为自己是过期的那一次
    };
  }, []);

  /** 提交并等到终态：成功给 `run_id`，失败给 `failure`（提交期与执行期两类都收好了）。 */
  const submit = useCallback(async (body: BacktestRequest): Promise<TaskSubmitResult> => {
    const seq = ++seqRef.current;
    const current = () => seq === seqRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const inFlight = inFlightRef.current; // 取键之前先记下：这一次是不是「同一个动作的重试」
    inFlightRef.current = true;
    setState({ running: true, phase: "排队中" });

    try {
      const { task_id } = await api.submitBacktest(body, keyRef.current.current(inFlight));
      const task = await pollTask(task_id, {
        signal: controller.signal,
        onTick: (tick: TaskDetail) => {
          if (current()) setState((prev) => ({ ...prev, phase: taskPhaseLabel(tick) || null }));
        },
      });
      const runId = taskRunId(task);
      if (current()) inFlightRef.current = false; // 落定了：下一次提交是新动作，换键
      if (runId !== null) {
        if (current()) setState(IDLE);
        return { ok: true, runId };
      }
      // 终态但不是成功：超时那条路抛异常，走下面的 catch
      if (current()) setState(IDLE);
      return { ok: false, failure: taskFailure(task) };
    } catch (cause) {
      if (current()) inFlightRef.current = false;
      if (current()) setState(IDLE);
      return { ok: false, failure: failureOf(cause) };
    }
  }, []);

  return { ...state, submit };
}

/** 提交期异常 → `RunFailure`。带 payload 的（我们的 4xx）走既有映射，其余是网络类兜底。 */
function failureOf(cause: unknown): RunFailure {
  if (cause instanceof ApiError && cause.payload) {
    return readRunFailure(cause.payload, cause.message);
  }
  return {
    message: describeError(cause, "提交失败：后端未启动或网络不通。"),
    findings: [],
    kind: null,
  };
}
