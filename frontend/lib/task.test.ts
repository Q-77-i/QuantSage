/**
 * 任务纯逻辑的用例（`lib/task.ts`，P3-E1a）。
 *
 * 轮询是「悄悄错」的重灾区：终态判错会一直转圈、失败映射错会把沙箱超限显示成「未知错误」、
 * 超时判错会让页面永远停在「执行中」。取数与等待都可注入，这里全部用假件跑，不 sleep。
 */

import { describe, expect, it, vi } from "vitest";

import { isTerminal, pollTask, taskFailure, taskPhaseLabel, taskRunId } from "./task";
import type { TaskDetail, TaskStatus } from "./types";

function task(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    id: "t1",
    kind: "backtest",
    status: "queued",
    progress: { done: 0, total: 1 },
    result: null,
    error: null,
    created_at: "2026-10-14T10:00:00Z",
    started_at: null,
    finished_at: null,
    request: {} as TaskDetail["request"],
    ...overrides,
  };
}

describe("isTerminal", () => {
  it("done / failed / cancelled 是终态；queued / running 不是", () => {
    const cases: Array<[TaskStatus, boolean]> = [
      ["queued", false],
      ["running", false],
      ["done", true],
      ["failed", true],
      ["cancelled", true],
    ];
    for (const [status, expected] of cases) {
      expect(isTerminal(status)).toBe(expected);
    }
  });
});

describe("taskPhaseLabel", () => {
  it("排队中 / 执行中；多格任务才报进度（单次回测恒 1/1，说了等于没说）", () => {
    expect(taskPhaseLabel(task({ status: "queued" }))).toBe("排队中");
    expect(taskPhaseLabel(task({ status: "running" }))).toBe("执行中");
    expect(
      taskPhaseLabel(task({ status: "running", progress: { done: 3, total: 25 } })),
    ).toBe("执行中 3/25");
    expect(taskPhaseLabel(task({ status: "done" }))).toBe("");
  });
});

describe("taskRunId / taskFailure", () => {
  it("只有 done 才给 run_id；其余（含 cancelled）一律 null", () => {
    expect(taskRunId(task({ status: "done", result: { run_id: "r1" } }))).toBe("r1");
    expect(taskRunId(task({ status: "failed", result: { run_id: "r1" } }))).toBeNull();
    expect(taskRunId(task({ status: "cancelled" }))).toBeNull();
    expect(taskRunId(task({ status: "done", result: null }))).toBeNull();
  });

  it("失败映射保留 kind（沙箱档位文案靠它），findings 恒空——静态检查那类根本不会排队", () => {
    const failure = taskFailure(
      task({ status: "failed", error: { kind: "memory", message: "内存超限" } }),
    );
    expect(failure).toEqual({ message: "内存超限", findings: [], kind: "memory" });
  });

  it("没有 error 的失败也给得出人话（不显示 undefined）", () => {
    expect(taskFailure(task({ status: "cancelled" })).message).toContain("cancelled");
  });
});

describe("pollTask", () => {
  const noSleep = () => Promise.resolve();

  it("轮询到终态即返回，并逐次回调进度", async () => {
    const ticks: TaskStatus[] = [];
    const fetchTask = vi
      .fn()
      .mockResolvedValueOnce(task({ status: "running" }))
      .mockResolvedValueOnce(task({ status: "running", progress: { done: 1, total: 2 } }))
      .mockResolvedValue(task({ status: "done", result: { run_id: "r9" } }));

    const result = await pollTask("t1", {
      fetchTask,
      sleep: noSleep,
      onTick: (t) => ticks.push(t.status),
    });

    expect(result.status).toBe("done");
    expect(ticks).toEqual(["running", "running", "done"]);
    expect(fetchTask).toHaveBeenCalledTimes(3);
  });

  it("failed / cancelled 也是终态，不再继续拉", async () => {
    const fetchTask = vi.fn().mockResolvedValue(task({ status: "failed" }));
    const result = await pollTask("t1", { fetchTask, sleep: noSleep });
    expect(result.status).toBe("failed");
    expect(fetchTask).toHaveBeenCalledTimes(1);
  });

  it("超过上限**抛错**，不返回一个还在跑的半成品", async () => {
    const fetchTask = vi.fn().mockResolvedValue(task({ status: "running" }));
    await expect(
      pollTask("t1", { fetchTask, sleep: noSleep, maxWaitMs: 2000 }),
    ).rejects.toThrow(/仍未结束/);
  });

  it("signal 已 abort 时不再取数（P2 卸载即停）", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchTask = vi.fn();

    await expect(
      pollTask("t1", { fetchTask, sleep: noSleep, signal: controller.signal }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchTask).not.toHaveBeenCalled();
  });

  it("首轮比其后快（首轮 400ms / 其后 1s）：单次回测常见情形一次就拿到", async () => {
    const sleeps: number[] = [];
    const fetchTask = vi
      .fn()
      .mockResolvedValueOnce(task({ status: "running" }))
      .mockResolvedValue(task({ status: "done" }));

    await pollTask("t1", {
      fetchTask,
      sleep: (ms) => {
        sleeps.push(ms);
        return Promise.resolve();
      },
    });

    expect(sleeps).toEqual([400, 1000]);
  });
});
