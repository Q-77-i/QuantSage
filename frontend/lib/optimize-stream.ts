/**
 * 网格/批量事件流的**纯视图模型**（P3-E1b）：帧进、状态出。
 *
 * 抽出来的理由与 `parseFrames` 同源：帧的落位错了是**静默**的——`cell` 按到达顺序 append
 * 会让热力图串位（完成次序是乱的，并发 2），`done` 漏了会让地址栏永远不带 `?run=`。
 * 纯函数能把这些钉在用例里，不必手点页面。
 *
 * E1b 新增两态：`detached`（流断了但任务还在跑）与 `cancelled`（用户中止，结果不落库）。
 * 它们与「出错」是三件事，界面说法不同，别混成一个 `error`。
 */

import type { OptimizeCell, OptimizeDoneFrame, OptimizeStartFrame } from "./types";

export interface OptimizeStreamState {
  started: OptimizeStartFrame | null;
  /** 按 `index` 落位；未到达的槽位是空洞（渲染前一律 `filter(Boolean)`） */
  cells: OptimizeCell[];
  done: OptimizeDoneFrame | null;
  error: string | null;
  /** 用户中止（E1b）：结果**不落库**，界面要说明白 */
  cancelled: boolean;
  /** 流断了、任务还在后台跑（E1b）：可续看 */
  detached: boolean;
}

export function emptyStream(): OptimizeStreamState {
  return { started: null, cells: [], done: null, error: null, cancelled: false, detached: false };
}

/** 起跑：清空上一次的一切 */
export function startStream(): OptimizeStreamState {
  return emptyStream();
}

/** 断线：**保留已收到的格**（E1b 的要点——它们是真跑出来的，不是垃圾） */
export function markDetached(state: OptimizeStreamState): OptimizeStreamState {
  if (state.done || state.error || state.cancelled) return state; // 已经终态了，断线无意义
  return { ...state, detached: true };
}

/** 续看：重开订阅时先把「断了」的提示收掉 */
export function markAttached(state: OptimizeStreamState): OptimizeStreamState {
  return { ...state, detached: false };
}

export function applyFrame(
  state: OptimizeStreamState,
  frame: { event: string; data: string },
): OptimizeStreamState {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(frame.data) as Record<string, unknown>;
  } catch {
    return state; // 半截 JSON 不是事件（保活帧已在解析层被丢掉）
  }

  if (frame.event === "start") {
    const start = payload as unknown as OptimizeStartFrame;
    const slots: OptimizeCell[] = new Array(start.total);
    return { ...state, started: start, cells: slots, detached: false };
  }

  if (frame.event === "cell") {
    const cell = payload as unknown as OptimizeCell;
    const slots = [...state.cells];
    slots[cell.index] = cell; // **按 index 落位**：完成次序是乱的，append 会串位
    return { ...state, cells: slots };
  }

  if (frame.event === "done") {
    return { ...state, done: payload as unknown as OptimizeDoneFrame, detached: false };
  }

  if (frame.event === "cancelled") {
    return { ...state, cancelled: true, detached: false };
  }

  if (frame.event === "error") {
    return {
      ...state,
      error: String(payload.message ?? "运行中断"),
      detached: false,
    };
  }

  return state;
}

/** 已到达的格数（`cells` 里有空洞，别拿 `.length` 当进度） */
export function arrivedCount(state: OptimizeStreamState): number {
  return state.cells.filter(Boolean).length;
}
