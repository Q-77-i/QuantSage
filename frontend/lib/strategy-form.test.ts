import { describe, expect, it } from "vitest";

import {
  buildRunRequest,
  findingsToMarkers,
  formValues,
  hasBlockingFindings,
  paramFields,
  paramsFromForm,
  readRunFailure,
  sandboxKindLabel,
  sortFindings,
} from "./strategy-form";
import type { Finding, StrategyMeta } from "./types";

const finding = (over: Partial<Finding> = {}): Finding => ({
  rule: "R3",
  severity: "error",
  line: 12,
  message: "ctx.history 在物理上没有下一根",
  snippet: "    future = ctx.history[ctx.index + 1]",
  ...over,
});

const META: StrategyMeta = {
  params: {
    fast: { type: "int", default: 5, min: 1, max: 250, label: "快线周期" },
    slow: { type: "int", default: 20, min: 2, max: 250, label: "" },
    min_score: { type: "float", default: 50.5, min: null, max: 100, label: "最低评分" },
    enabled: { type: "bool", default: true, min: null, max: null, label: "" },
  },
  uses_events: false,
};

const RUN_FORM = {
  strategyId: "11111111-1111-1111-1111-111111111111",
  symbol: " 600519 ",
  start: "",
  end: "",
  pitMode: "both" as const,
};

describe("findingsToMarkers", () => {
  it("把规则号写进话术，供编辑器悬停显示", () => {
    expect(findingsToMarkers([finding()], 20)).toEqual([
      { line: 12, severity: "error", message: "[R3] ctx.history 在物理上没有下一根" },
    ]);
  });

  it("行号越界钳到合法区间（检查的是上一次保存的文本，编辑器里可能已短了）", () => {
    const markers = findingsToMarkers([finding({ line: 99 }), finding({ line: 0 })], 7);
    expect(markers.map((marker) => marker.line)).toEqual([7, 1]);
  });

  it("空文件也钳到 1，不产生 0 行标记（编辑器会静默丢弃）", () => {
    expect(findingsToMarkers([finding({ line: 3 })], 0)[0].line).toBe(1);
  });

  it("同行多条不合并", () => {
    const markers = findingsToMarkers([finding(), finding({ rule: "R5" })], 20);
    expect(markers).toHaveLength(2);
  });
});

describe("hasBlockingFindings / sortFindings", () => {
  it("只有 error 拦运行", () => {
    expect(hasBlockingFindings([finding({ severity: "warning" })])).toBe(false);
    expect(hasBlockingFindings([finding(), finding({ severity: "warning" })])).toBe(true);
    expect(hasBlockingFindings([])).toBe(false);
  });

  it("error 在前、组内按行号，且不改原数组", () => {
    const input = [
      finding({ severity: "warning", line: 3 }),
      finding({ severity: "error", line: 9 }),
      finding({ severity: "error", line: 2 }),
    ];
    expect(sortFindings(input).map((item) => [item.severity, item.line])).toEqual([
      ["error", 2],
      ["error", 9],
      ["warning", 3],
    ]);
    expect(input[0].severity).toBe("warning");
  });
});

describe("paramFields / formValues", () => {
  it("字段顺序即声明顺序，label 缺省回落参数名", () => {
    expect(paramFields(META).map((field) => [field.key, field.label])).toEqual([
      ["fast", "快线周期"],
      ["slow", "slow"],
      ["min_score", "最低评分"],
      ["enabled", "enabled"],
    ]);
    expect(paramFields(null)).toEqual([]);
  });

  it("初值：库里存的优先，缺的用 default 填满；数字进输入框变字符串", () => {
    expect(formValues(META)).toEqual({
      fast: "5",
      slow: "20",
      min_score: "50.5",
      enabled: true,
    });
    expect(formValues(META, { fast: 9, enabled: false })).toMatchObject({
      fast: "9",
      enabled: false,
    });
  });

  it("库里多余的键（schema 改过）静默丢弃，不塞进表单", () => {
    expect(Object.keys(formValues(META, { removed_key: 1, fast: 8 }))).toEqual([
      "fast",
      "slow",
      "min_score",
      "enabled",
    ]);
  });
});

describe("paramsFromForm", () => {
  it("数字搬运 + 布尔直传，不做值域判断（后端管）", () => {
    expect(
      paramsFromForm(META, { fast: "9", slow: "20", min_score: "50.5", enabled: false }),
    ).toEqual({ fast: 9, slow: 20, min_score: 50.5, enabled: false });
  });

  it("空串变 NaN 交给后端报错，不在这里编一个数", () => {
    const params = paramsFromForm(META, { fast: "", enabled: true });
    expect(Number.isNaN(params.fast as number)).toBe(true);
  });
});

describe("buildRunRequest", () => {
  it("缺省区间不带字段（后端的缺省语义依赖「字段缺失」）", () => {
    const request = buildRunRequest(RUN_FORM, META, { fast: 5 });
    expect(request).toEqual({
      strategy: "user",
      strategy_id: RUN_FORM.strategyId,
      symbol: "600519",
      pit_mode: "pit",
      params: { fast: 5 },
    });
  });

  it("不消费事件的策略不给 both（两模式必然同结果，别让后端白跑）", () => {
    expect(buildRunRequest(RUN_FORM, META, {}).pit_mode).toBe("pit");
    expect(buildRunRequest(RUN_FORM, { ...META, uses_events: true }, {}).pit_mode).toBe("both");
    expect(buildRunRequest(RUN_FORM, null, {}).pit_mode).toBe("pit");
  });

  it("显式区间照传", () => {
    const request = buildRunRequest(
      { ...RUN_FORM, start: "2026-02-01", end: "2026-02-10" },
      META,
      {},
    );
    expect([request.start, request.end]).toEqual(["2026-02-01", "2026-02-10"]);
  });
});

describe("readRunFailure", () => {
  it("422 闸门：findings 原样取回（面板与编辑器都要用）", () => {
    const failure = readRunFailure(
      { detail: "策略未通过前视静态检查：1 个 error", findings: [finding()] },
      "策略未通过前视静态检查：1 个 error",
    );
    expect(failure.findings).toHaveLength(1);
    expect(failure.kind).toBeNull();
  });

  it("沙箱终止：kind 取回", () => {
    expect(readRunFailure({ detail: "内存超限", kind: "memory" }, "内存超限").kind).toBe("memory");
  });

  it("畸形响应不当崩：非数组 findings / 非法 severity / 缺字段一律忽略", () => {
    expect(readRunFailure(null, "x").findings).toEqual([]);
    expect(readRunFailure({ findings: "oops" }, "x").findings).toEqual([]);
    expect(
      readRunFailure({ findings: [{ rule: "R1", severity: "fatal", line: 1, message: "m" }] }, "x")
        .findings,
    ).toEqual([]);
    expect(readRunFailure({ findings: [{ line: 1 }] }, "x").findings).toEqual([]);
  });
});

describe("sandboxKindLabel", () => {
  it("认识的档位给人话，不认识的回落到中性说法", () => {
    expect(sandboxKindLabel("memory")).toBe("内存超限");
    expect(sandboxKindLabel("who-knows")).toBe("沙箱终止");
  });
});
