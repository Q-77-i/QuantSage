/**
 * 异步任务的纯逻辑（P3-E1a）：终态判定、进度文案、失败映射、轮询循环。
 *
 * 放在 `lib/` 而不是 hook 里，是为了这一层能被 vitest 直接测——`pollTask` 的取数与
 * 等待都可注入（真跑的话测试要 sleep，那既慢又 flaky）。
 * 提交与状态渲染在 `components/tasks/use-task.ts`。
 */

import { api } from "./api";
import type { RunFailure } from "./strategy-form";
import type { TaskDetail, TaskStatus } from "./types";

/** 终态：不再轮询。`cancelled` 也是终态——排队中被取消，不会再执行 */
export const TERMINAL_STATUSES: readonly TaskStatus[] = ["done", "failed", "cancelled"];

export function isTerminal(status: TaskStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/** 排队/执行中的一句话。进度只在多格任务里出现（单次回测恒 1/1，说了等于没说） */
export function taskPhaseLabel(task: TaskDetail): string {
  if (task.status === "queued") return "排队中";
  if (task.status !== "running") return "";
  const { done, total } = task.progress;
  return total > 1 ? `执行中 ${done}/${total}` : "执行中";
}

/**
 * 任务失败 → `RunFailure`（工作台的错误面板与编辑器标注吃同一份形状）。
 *
 * 任务 `error` 与提交期 4xx 的响应体**同形**（`{kind, message, line}`），所以这里不用
 * 造第二套映射：`kind` 直接进沙箱档位文案，`findings` 为空（静态检查那类问题根本不会
 * 排上队——它们在提交期就被 422 拦下了）。
 */
export function taskFailure(task: TaskDetail): RunFailure {
  const error = task.error;
  return {
    message: error?.message ?? `任务未完成（${task.status}）`,
    findings: [],
    kind: error?.kind ?? null,
  };
}

/** 成功任务里的报告号；非成功（含排队中）一律 null */
export function taskRunId(task: TaskDetail): string | null {
  return task.status === "done" ? (task.result?.run_id ?? null) : null;
}

const FIRST_DELAY_MS = 400; // 单次回测 0.3–2s：首轮快一点，常见情形一次就拿到
const POLL_MS = 1000;
const MAX_WAIT_MS = 3 * 60 * 1000; // 与 use-chat 同族：服务端异常时不无限轮询

export interface PollTaskOptions {
  /** 每次拿到新状态回调一次（用来更新「排队中/执行中」） */
  onTick?: (task: TaskDetail) => void;
  /** 取数（测试注入） */
  fetchTask?: (taskId: string, signal?: AbortSignal) => Promise<TaskDetail>;
  /** 等待（测试注入成立即返回，免去真 sleep） */
  sleep?: (ms: number) => Promise<void>;
  /** 单次轮询的上限（测试注入小值） */
  maxWaitMs?: number;
  /** 卸载/切换时中止：不再取数，也不再把状态写回 */
  signal?: AbortSignal;
}

function abortError(): DOMException {
  return new DOMException("任务轮询已取消", "AbortError");
}

function sleepWithSignal(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * 轮询到终态。**超时抛 `Error`**（不返回一个「还在跑」的半成品：调用方拿它当失败处理）。
 */
export async function pollTask(
  taskId: string,
  options: PollTaskOptions = {},
): Promise<TaskDetail> {
  const fetchTask =
    options.fetchTask ?? ((id: string, signal?: AbortSignal) => api.task(id, signal));
  const sleep =
    options.sleep ?? ((ms: number) => sleepWithSignal(ms, options.signal));
  const maxWaitMs = options.maxWaitMs ?? MAX_WAIT_MS;

  let waited = 0;
  for (let attempt = 0; ; attempt += 1) {
    if (options.signal?.aborted) throw abortError();
    await sleep(attempt === 0 ? FIRST_DELAY_MS : POLL_MS);
    waited += attempt === 0 ? FIRST_DELAY_MS : POLL_MS;
    if (options.signal?.aborted) throw abortError();
    const task = await fetchTask(taskId, options.signal);
    options.onTick?.(task);
    if (isTerminal(task.status)) return task;
    if (waited >= maxWaitMs) {
      throw new Error(`任务超过 ${Math.round(maxWaitMs / 1000)} 秒仍未结束`);
    }
  }
}
