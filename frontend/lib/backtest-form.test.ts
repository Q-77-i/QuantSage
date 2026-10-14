/**
 * 回测表单纯函数的用例。
 *
 * M4c-3 起有两处签名变化，用例一并跟上：**参数由 descriptor 驱动**（不再是 `PARAMS` 查表，
 * 因为用户策略的参数来自源码 schema），以及**策略位可能是 `user:<id>`**。
 */

import { describe, expect, it } from "vitest";

import {
  buildRequest,
  defaultForm,
  defaultParams,
  formFromRequest,
  hasErrors,
  paramsMatch,
  parseUserOption,
  pitModeLabel,
  strategyLabel,
  strategyOptions,
  strategySelection,
  symbolName,
  userOptionValue,
  validateForm,
} from "./backtest-form";
import type { FormState } from "./backtest-form";
import { builtinDescriptors, userDescriptors } from "./strategy-params";
import type { ParamDescriptor } from "./strategy-params";

/** 内置策略的描述符（真实路径：`useDescriptors` 对内置是同步给的） */
function descriptorsOf(strategy: "ma_cross" | "event_driven"): ParamDescriptor[] {
  return builtinDescriptors(strategy);
}

function form(patch: Partial<FormState> = {}): FormState {
  return { ...defaultForm(), ...patch };
}

/** 一条用户策略的参数 schema（由源码里的 `PARAMS` 字面量静态解析而来） */
const USER_SCHEMA = userDescriptors({
  window: { type: "int", default: 20, min: 2, max: 250, label: "均线窗口" },
  use_filter: { type: "bool", default: true, min: null, max: null, label: "启用过滤" },
});

describe("defaultForm", () => {
  it("首屏是事件驱动 + 双模式对比 —— 一次点击直达 PIT 对比表", () => {
    const state = defaultForm();

    expect(state.strategy).toBe("event_driven");
    expect(state.strategyId).toBeNull();
    expect(state.pitMode).toBe("both");
    expect(state.symbol).toBe("600519");
    expect(state.start).toBe("");
    expect(state.end).toBe("");
  });

  it("参数按策略预填，且总是随请求发出（不依赖后端缺省）", () => {
    expect(defaultForm().params).toEqual({ min_score: "50", hold_days: "5" });
    expect(defaultParams(descriptorsOf("ma_cross"))).toEqual({ fast: "5", slow: "20" });
  });

  it("布尔参数按 schema 的 default 预填（默认 true 的开关不能默认成 false）", () => {
    expect(defaultParams(USER_SCHEMA)).toEqual({ window: "20", use_filter: true });
  });
});

describe("strategySelection", () => {
  it("换内置策略时参数整组替换，其余字段保留", () => {
    const next = strategySelection(form({ start: "2026-07-01", fees: false }), "ma_cross", null);

    expect(next.params).toEqual({ fast: "5", slow: "20" });
    expect(next.start).toBe("2026-07-01");
    expect(next.fees).toBe(false);
  });

  it("选中当前项时返回同一引用，不触发无谓重渲", () => {
    const state = form();
    expect(strategySelection(state, "event_driven", null)).toBe(state);
  });

  it("换用户策略时参数**清空**（schema 要等一次请求）——留着旧参数提交会被后端按未知键 422", () => {
    const next = strategySelection(form(), "user", "str-1");

    expect(next.strategy).toBe("user");
    expect(next.strategyId).toBe("str-1");
    expect(next.params).toEqual({});
  });

  it("换回内置时把 strategyId 清掉（否则请求会带着 user 的 id 走内置分支）", () => {
    const userState = strategySelection(form(), "user", "str-1");
    expect(strategySelection(userState, "ma_cross", null).strategyId).toBeNull();
  });
});

describe("paramsMatch —— schema 到位后要不要重建参数", () => {
  it("键集一致就不动（重开时参数来自当次请求，覆盖成默认值等于换了一套参数）", () => {
    const state = form({
      strategy: "user",
      strategyId: "s1",
      params: { window: "30", use_filter: true },
    });
    expect(paramsMatch(state, USER_SCHEMA)).toBe(true);
  });

  it("键集不一致（换策略 / 异步 schema 刚到位）要重建", () => {
    expect(paramsMatch(form({ strategy: "user", strategyId: "s1", params: {} }), USER_SCHEMA)).toBe(
      false,
    );
    expect(paramsMatch(form(), USER_SCHEMA)).toBe(false);
  });
});

describe("strategyOptions —— 下拉的分组（M4c-3）", () => {
  it("没有用户策略时只有「内置」一组（空分组是噪音）", () => {
    const groups = strategyOptions([]);
    expect(groups.map((group) => group.label)).toEqual(["内置"]);
    expect(groups[0].items.map((item) => item.value)).toEqual(["event_driven", "ma_cross"]);
  });

  it("有用户策略时多一组，取值是 `user:<id>`（一页可能有多条，只写 user 分不出是哪条）", () => {
    const groups = strategyOptions([
      { id: "s1", name: "我的动量", created_at: "", updated_at: "" },
      { id: "s2", name: "我的反转", created_at: "", updated_at: "" },
    ]);
    expect(groups.map((group) => group.label)).toEqual(["内置", "我的策略"]);
    expect(groups[1].items).toEqual([
      { value: "user:s1", label: "我的动量" },
      { value: "user:s2", label: "我的反转" },
    ]);
  });

  it("`user:<id>` 往返一致；内置策略名不会被误判成用户策略", () => {
    expect(parseUserOption(userOptionValue("abc"))).toBe("abc");
    expect(parseUserOption("ma_cross")).toBeNull();
    expect(parseUserOption("event_driven")).toBeNull();
  });
});

describe("validateForm —— 只补后端不校验的那部分", () => {
  const ED = descriptorsOf("event_driven");

  it("默认表单无错", () => {
    expect(hasErrors(validateForm(defaultForm(), descriptorsOf("event_driven")))).toBe(false);
  });

  it("慢线不大于快线时拦下 —— 后端 from_params 会静默接受这种组合", () => {
    const state = form({ strategy: "ma_cross", params: { fast: "20", slow: "5" } });
    expect(validateForm(state, descriptorsOf("ma_cross")).slow).toContain("慢线必须大于快线");

    const equal = form({ strategy: "ma_cross", params: { fast: "20", slow: "20" } });
    expect(validateForm(equal, descriptorsOf("ma_cross")).slow).toContain("慢线必须大于快线");
  });

  it("空值与非数字都提示填数字", () => {
    expect(validateForm(form({ params: { min_score: "", hold_days: "5" } }), ED).min_score).toBe(
      "请填数字",
    );
    expect(validateForm(form({ params: { min_score: "5", hold_days: "abc" } }), ED).hold_days).toBe(
      "请填数字",
    );
  });

  it("越界值给出取值范围", () => {
    expect(
      validateForm(form({ params: { min_score: "-1", hold_days: "5" } }), ED).min_score,
    ).toContain("0 ~ 100");
    expect(
      validateForm(form({ params: { min_score: "150", hold_days: "5" } }), ED).min_score,
    ).toContain("0 ~ 100");
    expect(
      validateForm(form({ params: { min_score: "50", hold_days: "0" } }), ED).hold_days,
    ).toContain("取值范围 1");
  });

  it("滑点档位与前端的 0~100 口径一致（后端也有 ge/le，此处只是省一次往返）", () => {
    expect(hasErrors(validateForm(form({ slippageBps: "0" }), ED))).toBe(false);
    expect(hasErrors(validateForm(form({ slippageBps: "100" }), ED))).toBe(false);
    expect(validateForm(form({ slippageBps: "500" }), ED).slippageBps).toContain("0 ~ 100");
    expect(validateForm(form({ slippageBps: "" }), ED).slippageBps).toContain("0 ~ 100");
  });

  it("日期先后**不在这里校验** —— 交给后端 422，前端不重复实现同一规则", () => {
    const errors = validateForm(form({ start: "2026-09-01", end: "2026-07-01" }), ED);
    expect(hasErrors(errors)).toBe(false);
  });

  it("标的改成自由输入后，六位数字以外的值要挡下 —— 后端只有英文 pattern 报错", () => {
    expect(validateForm(form({ symbol: "" }), ED).symbol).toContain("请输入");
    expect(validateForm(form({ symbol: "60051" }), ED).symbol).toContain("6 位数字");
    expect(validateForm(form({ symbol: "abcdef" }), ED).symbol).toContain("6 位数字");
    expect(hasErrors(validateForm(form({ symbol: "000001" }), ED))).toBe(false);
  });

  it("用户策略：只校验 schema 里声明的参数，布尔不参与（开关没有「填错」这回事）", () => {
    const tooBig = form({
      strategy: "user",
      strategyId: "s1",
      params: { window: "999", use_filter: false },
    });
    expect(validateForm(tooBig, USER_SCHEMA).window).toContain("2 ~ 250");

    const fine = form({
      strategy: "user",
      strategyId: "s1",
      params: { window: "30", use_filter: false },
    });
    expect(validateForm(fine, USER_SCHEMA)).toEqual({});
  });

  it("选了用户策略却没有 id 时挡住（后端也会 422，但这里能先说清）", () => {
    const state = form({ strategy: "user", strategyId: null, params: {} });
    expect(validateForm(state, []).strategy).toContain("已保存的策略");
  });
});

describe("buildRequest", () => {
  const ED = descriptorsOf("event_driven");

  it("区间留空时不带该字段（后端的缺省语义依赖字段缺失，送空串会解析失败）", () => {
    const body = buildRequest(defaultForm(), ED);

    expect(body).not.toHaveProperty("start");
    expect(body).not.toHaveProperty("end");
  });

  it("填了区间就带上，字符串原样（date input 的 value 就是 ISO）", () => {
    const body = buildRequest(form({ start: "2026-07-01", end: "2026-09-30" }), ED);

    expect(body.start).toBe("2026-07-01");
    expect(body.end).toBe("2026-09-30");
  });

  it("参数字符串转成数字随请求发出", () => {
    const body = buildRequest(form({ params: { min_score: "60", hold_days: "3" } }), ED);

    expect(body.params).toEqual({ min_score: 60, hold_days: 3 });
  });

  it("成本开关与滑点档位映射成后端的结构", () => {
    const body = buildRequest(form({ fees: false, slippage: true, slippageBps: "8.5" }), ED);

    expect(body.costs).toEqual({ fees: false, slippage: true, slippage_bps: 8.5 });
  });

  it("策略、标的、模式原样带出；换策略走 strategySelection，参数跟着换组", () => {
    const body = buildRequest(
      strategySelection(form({ symbol: "300750", pitMode: "pit" }), "ma_cross", null),
      descriptorsOf("ma_cross"),
    );

    expect(body.strategy).toBe("ma_cross");
    expect(body.symbol).toBe("300750");
    expect(body.pit_mode).toBe("pit");
    expect(body.params).toEqual({ fast: 5, slow: 20 });
    expect(body).not.toHaveProperty("strategy_id"); // 内置策略不带它（后端会 422）
  });

  it("用户策略带 strategy_id，参数按 schema 转（数字转数、开关转布尔）", () => {
    const body = buildRequest(
      form({ strategy: "user", strategyId: "s1", params: { window: "30", use_filter: true } }),
      USER_SCHEMA,
    );

    expect(body.strategy).toBe("user");
    expect(body.strategy_id).toBe("s1");
    expect(body.params).toEqual({ window: 30, use_filter: true });
  });
});

describe("formFromRequest —— 重开历史回测时把表单填回当时的样子", () => {
  const stored = {
    strategy: "ma_cross" as const,
    symbol: "300750",
    start: "2026-07-05",
    end: "2026-09-30",
    adjust: "qfq" as const,
    pit_mode: "pit" as const,
    costs: { fees: false, slippage: true, slippage_bps: 8 },
    params: { fast: 3, slow: 15 },
  };
  const MA = descriptorsOf("ma_cross");

  it("区间、成本开关、参数原样回填（不回填的话表单与报告对不上）", () => {
    expect(formFromRequest(stored, defaultForm(), MA)).toEqual({
      strategy: "ma_cross",
      strategyId: null,
      symbol: "300750",
      start: "2026-07-05",
      end: "2026-09-30",
      fees: false,
      slippage: true,
      slippageBps: "8",
      pitMode: "pit",
      params: { fast: "3", slow: "15" },
    });
  });

  it("回填后再构造请求，跑的是当时那段区间", () => {
    const body = buildRequest(formFromRequest(stored, defaultForm(), MA), MA);
    expect(body.start).toBe("2026-07-05");
    expect(body.end).toBe("2026-09-30");
    expect(body.params).toEqual({ fast: 3, slow: 15 });
  });

  it("参数缺项回落到默认值（老记录里可能没有该键）", () => {
    const partial = { ...stored, params: { fast: 3 } as Record<string, number> };
    expect(formFromRequest(partial, defaultForm(), MA).params).toEqual({ fast: "3", slow: "20" });
  });

  it("用户策略记录：取到当前策略（有 id）时回填策略位与当次参数（M4c-3）", () => {
    const userRun = {
      ...stored,
      strategy: "user" as const,
      strategy_id: "s1",
      params: { window: 30, use_filter: false },
    };
    const filled = formFromRequest(userRun, defaultForm(), USER_SCHEMA, "s1");

    expect(filled.strategy).toBe("user");
    expect(filled.strategyId).toBe("s1");
    expect(filled.params).toEqual({ window: "30", use_filter: false });
    expect([filled.symbol, filled.start, filled.end]).toEqual([
      "300750",
      "2026-07-05",
      "2026-09-30",
    ]);
  });

  it("用户策略记录：策略已删（没给 id）时退化成 M4c 的行为，且**不炸**", () => {
    // `strategy: "user"` 不在 `PARAMS` 表里，查表再 .map 会 TypeError —— 这条把它钉住
    const userRun = {
      ...stored,
      strategy: "user" as const,
      strategy_id: "s1",
      params: { window: 30 },
    };
    const base = { ...defaultForm(), symbol: "600036" };
    const filled = formFromRequest(userRun, base, [], null);

    expect(filled.strategyId).toBeNull();
    expect(filled.strategy).toBe(base.strategy); // 策略位沿用当前选择
    expect(filled.params).toEqual(base.params);
    // 与策略无关的那几项照常回填，报告与表单至少在这几项上对得上
    expect([filled.symbol, filled.start, filled.end]).toEqual([
      "300750",
      "2026-07-05",
      "2026-09-30",
    ]);
    expect(filled.fees).toBe(false);
    expect(filled.slippageBps).toBe("8");
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
