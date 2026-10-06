/**
 * 回测表单：状态 ↔ 请求体，以及**后端不校验的那部分值域**。
 *
 * 职责边界刻意划在这里：
 *   · 策略参数的值域由前端管——后端 `from_params` 只认键名（拼错返回 422），
 *     但值本身照单全收，`fast=20 / slow=5` 会静默跑出无意义的结果（`MaCross` 不做交叉校验）
 *   · 日期区间的先后由**后端**管——`BacktestRequest` 已有 `start > end` 的校验且文案清楚，
 *     前端不重复实现，这条路也正好验证「后端 422 → 内联显示」的通路
 *   · 标的格式（六位数字）后端也只有 pydantic 的 pattern 报错、文案是英文的
 *     `String should match pattern ...`，落到页面上是一句看不懂的话，故前端先挡一道
 */

import type { BacktestRequest, PitMode, StoredBacktestRequest, Strategy } from "./types";
import { validateSymbol } from "./watchlist";

/**
 * 示例标的：快捷键，**不是可用范围**。
 *
 * 本地行情库自 M2a 起覆盖全市场 A 股（约 5500 只，2020-01-02 起），标的是自由输入的
 * 六位代码。这里留三只只是给一个点得动的起点；名称映射也只覆盖它们，其余标的
 * `symbolName()` 回退成裸代码（数据源没有名称数据集，硬造一份等于引第二数据源）。
 */
export const SAMPLE_SYMBOLS = [
  { code: "600519", name: "贵州茅台" },
  { code: "300750", name: "宁德时代" },
  { code: "600036", name: "招商银行" },
] as const;

export const STRATEGIES: { value: Strategy; label: string }[] = [
  { value: "event_driven", label: "事件驱动" },
  { value: "ma_cross", label: "双均线" },
];

export const PIT_MODES: { value: PitMode; label: string }[] = [
  { value: "both", label: "PIT / 非 PIT 对比" },
  { value: "pit", label: "仅 PIT" },
  { value: "non_pit", label: "仅非 PIT" },
];

export interface ParamField {
  key: string;
  label: string;
  /** 与后端 `DEFAULT_*` 对应（`strategies/ma_cross.py`、`strategies/event_driven.py`），改一处须改两处 */
  fallback: number;
  min: number;
  max?: number;
  step: number;
}

/**
 * 策略参数表。默认值在页面打开时就填进输入框并**总是随请求发出**——
 * 用户看得见将要跑的是什么，不依赖后端缺省（缺省值两处漂移时，写死的那份至少是可见的）。
 */
export const PARAMS: Record<Strategy, ParamField[]> = {
  ma_cross: [
    { key: "fast", label: "快线", fallback: 5, min: 1, step: 1 },
    { key: "slow", label: "慢线", fallback: 20, min: 2, step: 1 },
  ],
  event_driven: [
    { key: "min_score", label: "最低评分", fallback: 50, min: 0, max: 100, step: 1 },
    { key: "hold_days", label: "持有天数", fallback: 5, min: 1, step: 1 },
  ],
};

export interface FormState {
  strategy: Strategy;
  symbol: string;
  /** 空串 = 交给后端缺省（事件驱动从事件窗口起点，其余从首根 bar 起） */
  start: string;
  end: string;
  fees: boolean;
  slippage: boolean;
  /** 滑点档位，字符串保存以忠实反映输入框内容 */
  slippageBps: string;
  pitMode: PitMode;
  params: Record<string, string>;
}

export function defaultParams(strategy: Strategy): Record<string, string> {
  return Object.fromEntries(PARAMS[strategy].map((field) => [field.key, String(field.fallback)]));
}

/** 首次打开页面的初始值：事件驱动 + 双模式对比（一次点击直达 PIT 对比表）。 */
export function defaultForm(): FormState {
  const strategy: Strategy = "event_driven";
  return {
    strategy,
    symbol: SAMPLE_SYMBOLS[0].code,
    start: "",
    end: "",
    fees: true,
    slippage: true,
    slippageBps: "5.0",
    pitMode: "both",
    params: defaultParams(strategy),
  };
}

/** 换策略时参数整组换成该策略的默认值——两套参数没有可复用的语义。 */
export function switchStrategy(state: FormState, strategy: Strategy): FormState {
  if (strategy === state.strategy) return state;
  return { ...state, strategy, params: defaultParams(strategy) };
}

export function validateForm(state: FormState): Record<string, string> {
  const errors: Record<string, string> = {};
  const values: Record<string, number> = {};

  const symbolError = validateSymbol(state.symbol);
  if (symbolError) errors.symbol = symbolError;

  for (const field of PARAMS[state.strategy]) {
    const raw = (state.params[field.key] ?? "").trim();
    const value = Number(raw);
    if (raw === "" || !Number.isFinite(value)) {
      errors[field.key] = "请填数字";
      continue;
    }
    if (value < field.min || (field.max !== undefined && value > field.max)) {
      errors[field.key] = `取值范围 ${field.min} ~ ${field.max ?? "不限"}`;
      continue;
    }
    values[field.key] = value;
  }

  if (state.strategy === "ma_cross" && !errors.fast && !errors.slow && values.fast >= values.slow) {
    errors.slow = "慢线必须大于快线";
  }

  const bps = Number(state.slippageBps.trim());
  if (state.slippageBps.trim() === "" || !Number.isFinite(bps) || bps < 0 || bps > 100) {
    errors.slippageBps = "滑点取值范围 0 ~ 100 bps";
  }

  return errors;
}

export function hasErrors(errors: Record<string, string>): boolean {
  return Object.keys(errors).length > 0;
}

/**
 * 表单 → 请求体。**调用前必须已通过 `validateForm`**。
 *
 * `start` / `end` 为空时不带该字段（而不是送空串）：后端的缺省语义依赖「字段缺失」，
 * 送空串会变成日期解析失败。
 */
export function buildRequest(state: FormState): BacktestRequest {
  const params: Record<string, number> = {};
  for (const field of PARAMS[state.strategy]) {
    params[field.key] = Number(state.params[field.key]);
  }

  return {
    strategy: state.strategy,
    symbol: state.symbol,
    ...(state.start ? { start: state.start } : {}),
    ...(state.end ? { end: state.end } : {}),
    costs: {
      fees: state.fees,
      slippage: state.slippage,
      slippage_bps: Number(state.slippageBps),
    },
    pit_mode: state.pitMode,
    params,
  };
}

/**
 * 存下来的请求 → 表单（重开历史回测用）。
 *
 * 必须回填：不回填的话表单显示的是默认值、报告却是那一次的，两者对不上，用户会以为
 * 参数没生效。区间存的是**解析后的结果**（不是空串），所以填回去之后再点「运行」=
 * 「按当时那段区间重跑」——这正是「重开」该有的语义。
 */
export function formFromRequest(request: StoredBacktestRequest): FormState {
  return {
    strategy: request.strategy,
    symbol: request.symbol,
    start: request.start,
    end: request.end,
    fees: request.costs.fees,
    slippage: request.costs.slippage,
    slippageBps: String(request.costs.slippage_bps),
    pitMode: request.pit_mode,
    params: Object.fromEntries(
      PARAMS[request.strategy].map((field) => [
        field.key,
        String(request.params[field.key] ?? field.fallback),
      ]),
    ),
  };
}

export function symbolName(code: string): string {
  return SAMPLE_SYMBOLS.find((symbol) => symbol.code === code)?.name ?? code;
}

export function strategyLabel(strategy: Strategy): string {
  return STRATEGIES.find((item) => item.value === strategy)?.label ?? strategy;
}

export function pitModeLabel(mode: PitMode): string {
  return PIT_MODES.find((item) => item.value === mode)?.label ?? mode;
}
