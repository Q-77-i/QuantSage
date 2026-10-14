/**
 * 网格/批量事件流的纯函数用例（P3-E1b）。
 *
 * 帧的落位错了是**静默**的：`cell` 按到达顺序 append 会让热力图串位（并发 2 时完成次序
 * 是乱的），`detached` 与 `error` 混成一个会让「任务还在跑」显示成「跑挂了」。
 */

import { describe, expect, it } from "vitest";

import {
  applyFrame,
  arrivedCount,
  emptyStream,
  markAttached,
  markDetached,
  startStream,
} from "./optimize-stream";
import type { OptimizeStreamState } from "./optimize-stream";

const frame = (event: string, payload: unknown) => ({ event, data: JSON.stringify(payload) });

function started(total = 3): OptimizeStreamState {
  return applyFrame(startStream(), frame("start", { kind: "grid", total, symbol: "600519" }));
}

describe("applyFrame", () => {
  it("`start` 建出 total 个空洞槽位（**不是**空数组——格按 index 落位）", () => {
    const state = started(3);
    expect(state.started?.total).toBe(3);
    expect(state.cells).toHaveLength(3);
    expect(arrivedCount(state)).toBe(0);
  });

  it("`cell` 按 index 落位：后到的 index=0 不会顶掉已到的 index=2", () => {
    let state = started(3);
    state = applyFrame(state, frame("cell", { index: 2, ok: true, symbol: "600519" }));
    state = applyFrame(state, frame("cell", { index: 0, ok: true, symbol: "600519" }));

    expect(state.cells[0]?.index).toBe(0);
    expect(state.cells[1]).toBeUndefined();
    expect(state.cells[2]?.index).toBe(2);
    expect(arrivedCount(state)).toBe(2);
  });

  it("`done` 带上 run_id（地址栏据此换 `?run=`）", () => {
    const state = applyFrame(started(1), frame("done", { run_id: "r1", cells_ok: 1 }));
    expect(state.done?.run_id).toBe("r1");
  });

  it("`cancelled` 与 `error` 是两回事（中止不是失败）", () => {
    const cancelled = applyFrame(started(1), frame("cancelled", { message: "已取消" }));
    expect(cancelled.cancelled).toBe(true);
    expect(cancelled.error).toBeNull();

    const failed = applyFrame(started(1), frame("error", { message: "内部错误" }));
    expect(failed.error).toBe("内部错误");
    expect(failed.cancelled).toBe(false);
  });

  it("半截 JSON 当没发生（保活与网络分片都会产生不完整块）", () => {
    const state = started(1);
    expect(applyFrame(state, { event: "cell", data: '{"index": 0' })).toBe(state);
  });

  it("认不出来的事件名原样返回（前向兼容：服务端加事件不该让页面炸）", () => {
    const state = started(1);
    expect(applyFrame(state, frame("progress", { done: 1 }))).toBe(state);
  });
});

describe("断线（detached）", () => {
  it("断线**保留已收到的格**——它们是真跑出来的，清空会让人以为白跑了", () => {
    let state = started(3);
    state = applyFrame(state, frame("cell", { index: 0, ok: true }));
    const detached = markDetached(state);

    expect(detached.detached).toBe(true);
    expect(arrivedCount(detached)).toBe(1);
  });

  it("已终态时断线不改状态（跑完了才断，没有「还在后台」这回事）", () => {
    const done = applyFrame(started(1), frame("done", { run_id: "r1" }));
    expect(markDetached(done)).toBe(done);
  });

  it("续看把提示收掉，但不动已经收到的格", () => {
    let state = markDetached(started(2));
    state = applyFrame(state, frame("cell", { index: 1, ok: true }));
    const attached = markAttached(state);

    expect(attached.detached).toBe(false);
    expect(arrivedCount(attached)).toBe(1);
  });
});

describe("startStream", () => {
  it("起跑清空上一次的一切（包括 detached）", () => {
    const dirty = markDetached(started(2));
    expect(startStream()).toEqual(emptyStream());
    expect(dirty.cells).toHaveLength(2); // 只是确认构造出来的确实是「脏」状态
  });
});
