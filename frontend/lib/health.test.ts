import { describe, expect, it } from "vitest";

import {
  durationText,
  groupChecks,
  levelMarker,
  levelTone,
  nextRunText,
  runSummary,
  staleSourceText,
  statusText,
} from "./health";
import type { HealthCheckBrief } from "./types";

const brief = (id: string, level: HealthCheckBrief["level"]): HealthCheckBrief => ({
  id,
  level,
  title: `${id} 的标题`,
});

describe("groupChecks", () => {
  it("按 error → warn → info 排，不看输入顺序", () => {
    const groups = groupChecks([brief("B1", "info"), brief("E3", "error"), brief("X6", "warn")]);
    expect(groups.map((g) => g.level)).toEqual(["error", "warn", "info"]);
    expect(groups[0].items[0].id).toBe("E3");
  });

  it("空组丢掉——一排空标题是纯噪音", () => {
    const groups = groupChecks([brief("B1", "info"), brief("B2", "info")]);
    expect(groups.map((g) => g.level)).toEqual(["info"]);
  });

  it("没有检查项时给空数组，不抛", () => {
    expect(groupChecks([])).toEqual([]);
  });
});

describe("配色与记号", () => {
  it("error 红、warn 琥珀、info 中性——与 M4 静态检查面板同一套", () => {
    expect(levelTone("error")).toBe("text-destructive");
    expect(levelTone("warn")).toBe("text-warn");
    expect(levelTone("info")).toBe("text-ink-3");
  });

  it("记号三档各不相同", () => {
    expect(new Set([levelMarker("error"), levelMarker("warn"), levelMarker("info")]).size).toBe(3);
  });
});

describe("statusText", () => {
  it("三档各说各的，且颜色不重样", () => {
    expect(statusText("red")).toEqual({ text: "有 error", tone: "text-destructive" });
    expect(statusText("yellow")).toEqual({ text: "有告警", tone: "text-warn" });
    expect(statusText("green").text).toBe("无告警");
    expect(new Set([statusText("red").tone, statusText("yellow").tone, statusText("green").tone]).size).toBe(3);
  });

  it("null（还没跑过 / 那一轮被跳过）不是绿——没结论与没问题必须分得开", () => {
    expect(statusText(null).text).toBe("还没跑过");
    expect(statusText(undefined).tone).toBe("text-ink-3");
  });
});

describe("runSummary", () => {
  it("正常一轮报三个计数", () => {
    expect(runSummary({ error: 0, warn: 6, info: 17 })).toBe("error 0 · warn 6 · info 17");
  });

  it("被写锁跳过的那轮如实说跳过，不编 0", () => {
    expect(runSummary({ skipped: "locked" })).toBe("跳过（locked）");
  });
});

describe("durationText", () => {
  it("秒为单位，保留一位", () => {
    expect(durationText(5490)).toBe("5.5s");
  });

  it("缺读数给破折号，不给 0s（0 秒是个错误的读数）", () => {
    expect(durationText(null)).toBe("—");
    expect(durationText(undefined)).toBe("—");
  });
});

describe("staleSourceText", () => {
  const base = {
    source_last: "2026-10-09",
    stalled_trading_days: 5,
    manifest_version: "history-public-v1-20261012T200508Z",
    release_at: "2026-10-13T04:05:08+08:00",
    last_probe_at: "2026-10-16T13:44:41+00:00",
  };

  it("停滞时把日期与交易日数都写出来——归因就是这一句的全部意义", () => {
    expect(staleSourceText(base)).toBe("源侧止于 2026-10-09（5 个交易日未推进）");
  });

  it("停滞为 0 时说已推进", () => {
    expect(staleSourceText({ ...base, stalled_trading_days: 0 })).toContain("已推进到");
  });

  it("null 是「无从判断」而不是 0——不得显示成「已推进」", () => {
    const text = staleSourceText({ ...base, stalled_trading_days: null });
    expect(text).toContain("无从判断");
    expect(text).not.toContain("已推进");
  });

  it("整个读数缺失时也走无从判断", () => {
    expect(staleSourceText(null)).toContain("无从判断");
  });
});

describe("nextRunText", () => {
  it("开着且有排期时报时刻", () => {
    expect(
      nextRunText({ enabled: true, hour: 22, minute: 10, next_run_at: "2026-10-17T22:10:00+08:00" }),
    ).toBe("下次 22:10");
  });

  it("关着时说清是关的，不显示一个假的下次时刻", () => {
    const text = nextRunText({ enabled: false, hour: 22, minute: 10, next_run_at: null });
    expect(text).toContain("未开启");
    expect(text).toContain("22:10"); // 配置里是几点仍要说（它是有用的信息）
  });

  it("开着但进程内没排期时不撒谎", () => {
    expect(nextRunText({ enabled: true, hour: 9, minute: 5, next_run_at: null })).toContain("暂无排期");
  });
});
