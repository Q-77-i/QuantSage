import { describe, expect, it } from "vitest";

import {
  buildRequest,
  defaultForm,
  defaultParams,
  hasErrors,
  pitModeLabel,
  strategyLabel,
  switchStrategy,
  symbolName,
  validateForm,
} from "./backtest-form";
import type { FormState } from "./backtest-form";

function form(patch: Partial<FormState> = {}): FormState {
  return { ...defaultForm(), ...patch };
}

describe("defaultForm", () => {
  it("首屏是事件驱动 + 双模式对比 —— 一次点击直达 PIT 对比表", () => {
    const state = defaultForm();

    expect(state.strategy).toBe("event_driven");
    expect(state.pitMode).toBe("both");
    expect(state.symbol).toBe("600519");
    expect(state.start).toBe("");
    expect(state.end).toBe("");
  });

  it("参数按策略预填，且总是随请求发出（不依赖后端缺省）", () => {
    expect(defaultForm().params).toEqual({ min_score: "50", hold_days: "5" });
    expect(defaultParams("ma_cross")).toEqual({ fast: "5", slow: "20" });
  });
});

describe("switchStrategy", () => {
  it("换策略时参数整组替换，其余字段保留", () => {
    const next = switchStrategy(form({ start: "2026-07-01", fees: false }), "ma_cross");

    expect(next.params).toEqual({ fast: "5", slow: "20" });
    expect(next.start).toBe("2026-07-01");
    expect(next.fees).toBe(false);
  });

  it("选中当前策略时返回同一引用，不触发无谓重渲", () => {
    const state = form();
    expect(switchStrategy(state, "event_driven")).toBe(state);
  });
});

describe("validateForm —— 只补后端不校验的那部分", () => {
  it("默认表单无错", () => {
    expect(hasErrors(validateForm(defaultForm()))).toBe(false);
  });

  it("慢线不大于快线时拦下 —— 后端 from_params 会静默接受这种组合", () => {
    const state = form({ strategy: "ma_cross", params: { fast: "20", slow: "5" } });
    expect(validateForm(state).slow).toContain("慢线必须大于快线");

    const equal = form({ strategy: "ma_cross", params: { fast: "20", slow: "20" } });
    expect(validateForm(equal).slow).toContain("慢线必须大于快线");
  });

  it("空值与非数字都提示填数字", () => {
    expect(validateForm(form({ params: { min_score: "", hold_days: "5" } })).min_score).toBe(
      "请填数字",
    );
    expect(validateForm(form({ params: { min_score: "5", hold_days: "abc" } })).hold_days).toBe(
      "请填数字",
    );
  });

  it("越界值给出取值范围", () => {
    expect(validateForm(form({ params: { min_score: "-1", hold_days: "5" } })).min_score).toContain(
      "0 ~ 100",
    );
    expect(validateForm(form({ params: { min_score: "150", hold_days: "5" } })).min_score).toContain(
      "0 ~ 100",
    );
    expect(validateForm(form({ params: { min_score: "50", hold_days: "0" } })).hold_days).toContain(
      "取值范围 1",
    );
  });

  it("滑点档位与前端的 0~100 口径一致（后端也有 ge/le，此处只是省一次往返）", () => {
    expect(hasErrors(validateForm(form({ slippageBps: "0" })))).toBe(false);
    expect(hasErrors(validateForm(form({ slippageBps: "100" })))).toBe(false);
    expect(validateForm(form({ slippageBps: "500" })).slippageBps).toContain("0 ~ 100");
    expect(validateForm(form({ slippageBps: "" })).slippageBps).toContain("0 ~ 100");
  });

  it("日期先后**不在这里校验** —— 交给后端 422，前端不重复实现同一规则", () => {
    const errors = validateForm(form({ start: "2026-09-01", end: "2026-07-01" }));
    expect(hasErrors(errors)).toBe(false);
  });
});

describe("buildRequest", () => {
  it("区间留空时不带该字段（后端的缺省语义依赖字段缺失，送空串会解析失败）", () => {
    const body = buildRequest(defaultForm());

    expect(body).not.toHaveProperty("start");
    expect(body).not.toHaveProperty("end");
  });

  it("填了区间就带上，字符串原样（date input 的 value 就是 ISO）", () => {
    const body = buildRequest(form({ start: "2026-07-01", end: "2026-09-30" }));

    expect(body.start).toBe("2026-07-01");
    expect(body.end).toBe("2026-09-30");
  });

  it("参数字符串转成数字随请求发出", () => {
    const body = buildRequest(form({ params: { min_score: "60", hold_days: "3" } }));

    expect(body.params).toEqual({ min_score: 60, hold_days: 3 });
  });

  it("成本开关与滑点档位映射成后端的结构", () => {
    const body = buildRequest(form({ fees: false, slippage: true, slippageBps: "8.5" }));

    expect(body.costs).toEqual({ fees: false, slippage: true, slippage_bps: 8.5 });
  });

  it("策略、标的、模式原样带出；换策略走 switchStrategy，参数跟着换组", () => {
    // 按应用里的真实路径构造：先改字段，再过 switchStrategy（它会整组换参数）
    const body = buildRequest(
      switchStrategy(form({ symbol: "300750", pitMode: "pit" }), "ma_cross"),
    );

    expect(body.strategy).toBe("ma_cross");
    expect(body.symbol).toBe("300750");
    expect(body.pit_mode).toBe("pit");
    expect(body.params).toEqual({ fast: 5, slow: 20 });
  });
});

describe("文案查询", () => {
  it("标的名与策略名有中文标签", () => {
    expect(symbolName("600519")).toBe("贵州茅台");
    expect(symbolName("999999")).toBe("999999");
    expect(strategyLabel("event_driven")).toBe("事件驱动");
    expect(pitModeLabel("both")).toBe("PIT / 非 PIT 对比");
  });
});
