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

import type { ParamValue } from "./strategy-form";
import { builtinDescriptors } from "./strategy-params";
import type { ParamDescriptor } from "./strategy-params";
import type {
  BacktestRequest,
  BuiltinStrategy,
  PitMode,
  StoredBacktestRequest,
  Strategy,
  StrategySummary,
} from "./types";
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

export const STRATEGIES: { value: BuiltinStrategy; label: string }[] = [
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
export const PARAMS: Record<BuiltinStrategy, ParamField[]> = {
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
  /** `strategy === "user"` 时必带：跑的是**库里那条**源码（M4c 的「先保存才能跑」） */
  strategyId: string | null;
  symbol: string;
  /** 空串 = 交给后端缺省（事件驱动从事件窗口起点，其余从首根 bar 起） */
  start: string;
  end: string;
  fees: boolean;
  slippage: boolean;
  /** 滑点档位，字符串保存以忠实反映输入框内容 */
  slippageBps: string;
  pitMode: PitMode;
  /** 内置策略的参数是数字串；用户策略还可能有开关（`bool`），故是联合 */
  params: Record<string, ParamValue>;
}

/**
 * 参数初值。**由 descriptor 给**（不是查 `PARAMS` 表）：内置描述符来自 PARAMS，
 * 用户策略的来自源码 schema——两者在 `lib/strategy-params.ts` 里已是同一形状。
 */
export function defaultParams(descriptors: ParamDescriptor[]): Record<string, ParamValue> {
  return Object.fromEntries(
    descriptors.map((field) => [
      field.key,
      field.type === "bool" ? Boolean(field.fallback) : String(field.fallback ?? ""),
    ]),
  );
}

/** 首次打开页面的初始值：事件驱动 + 双模式对比（一次点击直达 PIT 对比表）。 */
export function defaultForm(): FormState {
  const strategy: BuiltinStrategy = "event_driven";
  return {
    strategy,
    strategyId: null,
    symbol: SAMPLE_SYMBOLS[0].code,
    start: "",
    end: "",
    fees: true,
    slippage: true,
    slippageBps: "5.0",
    pitMode: "both",
    params: defaultParams(builtinDescriptors(strategy)),
  };
}

/**
 * 下拉里换了一条策略。
 *
 * 内置策略的参数表是**同步**可得的（前端常量），当场重建；用户策略要等一次请求拿 schema
 * （`useDescriptors`），所以这里把参数**清空**，由页面在 descriptors 到位后按
 * `paramsMatch` 补齐——清空而不是留旧值：留着旧策略的参数去提交，后端会按未知键 422，
 * 而用户看到的是「我刚换的策略却报旧参数不认识」。
 */
export function strategySelection(
  state: FormState,
  strategy: Strategy,
  strategyId: string | null,
): FormState {
  if (strategy === state.strategy && strategyId === state.strategyId) return state;
  if (strategy === "user") return { ...state, strategy, strategyId, params: {} };
  return {
    ...state,
    strategy,
    strategyId: null,
    params: defaultParams(builtinDescriptors(strategy)),
  };
}

/** 参数值的键集与当前 schema 是否一致——不一致就得重建（换策略、异步 schema 到位的那一刻） */
export function paramsMatch(state: FormState, descriptors: ParamDescriptor[]): boolean {
  const keys = descriptors.map((field) => field.key).sort();
  const have = Object.keys(state.params).sort();
  return keys.length === have.length && keys.every((key, index) => key === have[index]);
}

/**
 * 值域校验。**只校验 descriptor 里声明的参数**——schema 是唯一真源：
 * 内置的两条来自前端常量，用户策略的来自源码，后端也在同一份 schema 上再校验一次。
 */
export function validateForm(
  state: FormState,
  descriptors: ParamDescriptor[],
): Record<string, string> {
  const errors: Record<string, string> = {};
  const values: Record<string, number> = {};

  const symbolError = validateSymbol(state.symbol);
  if (symbolError) errors.symbol = symbolError;

  if (state.strategy === "user" && !state.strategyId) {
    errors.strategy = "请选择一条已保存的策略";
  }

  for (const field of descriptors) {
    const raw = state.params[field.key];
    if (field.type === "bool") continue; // 开关没有「填错」这回事
    const text = String(raw ?? "").trim();
    const value = Number(text);
    if (text === "" || !Number.isFinite(value)) {
      errors[field.key] = "请填数字";
      continue;
    }
    if (value < (field.min ?? -Infinity) || (field.max !== null && value > field.max)) {
      errors[field.key] = `取值范围 ${field.min ?? "不限"} ~ ${field.max ?? "不限"}`;
      continue;
    }
    values[field.key] = value;
  }

  // 跨字段约束只有内置双均线有（用户策略的跨字段规则跑在沙箱里，由后端 422/任务 error 给）
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
export function buildRequest(
  state: FormState,
  descriptors: ParamDescriptor[],
): BacktestRequest {
  const params: Record<string, number | boolean> = {};
  for (const field of descriptors) {
    const raw = state.params[field.key];
    params[field.key] = field.type === "bool" ? Boolean(raw) : Number(raw);
  }

  return {
    strategy: state.strategy,
    ...(state.strategy === "user" && state.strategyId ? { strategy_id: state.strategyId } : {}),
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
export function formFromRequest(
  request: StoredBacktestRequest,
  base: FormState,
  descriptors: ParamDescriptor[],
  /** 用户策略的记录：**取到当前策略之后**才有的 id；取不到（已删/读失败）传 null */
  strategyId: string | null = null,
): FormState {
  const common: FormState = {
    ...base,
    symbol: request.symbol,
    start: request.start,
    end: request.end,
    fees: request.costs.fees,
    slippage: request.costs.slippage,
    slippageBps: String(request.costs.slippage_bps),
    pitMode: request.pit_mode,
  };

  // **用户策略的记录**（M4c / M4c-3）：`PARAMS` 查表只在内置那一支——`"user"` 不在
  // 注册表里，查表会拿到 undefined 再 `.map` → TypeError（页面白屏，M4c 就点过名）。
  // 用户策略的参数值由 `descriptors`（源码 schema）与当次请求的 `params` 回填；
  // 策略位在**取到当前策略**时才填（`strategyId` 由调用方给）——已删的（404）保持原样，
  // 退化成 M4c 的行为：只回填与策略无关的几项 + 提示条。
  if (request.strategy === "user") {
    if (strategyId === null) return { ...common, strategyId: null };
    return {
      ...common,
      strategy: "user",
      strategyId,
      params: paramsFromRequest(descriptors, request.params),
    };
  }

  return {
    ...common,
    strategy: request.strategy,
    strategyId: null,
    params: paramsFromRequest(descriptors, request.params),
  };
}

/**
 * 当次请求的 `params` → 表单值。**缺的键用 schema 的初值补**（不是丢弃）：
 * 后端「未给的用 default 填满」，表单显示同一份缺省，点「运行」才等于「按当次重跑」。
 */
function paramsFromRequest(
  descriptors: ParamDescriptor[],
  stored: Record<string, number | boolean>,
): Record<string, ParamValue> {
  return Object.fromEntries(
    descriptors.map((field) => {
      const value = stored[field.key];
      if (field.type === "bool") {
        return [field.key, value === undefined ? Boolean(field.fallback) : Boolean(value)];
      }
      return [field.key, String(value ?? field.fallback ?? "")];
    }),
  );
}

export function symbolName(code: string): string {
  return SAMPLE_SYMBOLS.find((symbol) => symbol.code === code)?.name ?? code;
}

/**
 * 策略下拉的分组（M4c-3）：内置两条 + 我的策略。
 *
 * 空组**不出现**——一条用户策略都没有时，下拉里只有一个「内置」标题是噪音
 * （与优化页 `batchPicks` 同一取舍）。
 */
export interface StrategyOption {
  /** 内置策略就是策略名；用户策略是 `user:<id>`（见 `userOptionValue`） */
  value: string;
  label: string;
}

export function strategyOptions(
  userStrategies: StrategySummary[],
): { label: string; items: StrategyOption[] }[] {
  const groups: { label: string; items: StrategyOption[] }[] = [
    { label: "内置", items: STRATEGIES.map((item) => ({ value: item.value, label: item.label })) },
  ];
  if (userStrategies.length > 0) {
    groups.push({
      label: "我的策略",
      items: userStrategies.map((item) => ({ value: userOptionValue(item.id), label: item.name })),
    });
  }
  return groups;
}

/**
 * 下拉里用户策略的取值：`user:<id>`。
 *
 * 不能只写 `"user"`——**同一页面上可能有多条**，只写类型就分不出选的是哪一条
 * （优化页的批量复选框 M5b 起就是 `user:<id>` 的写法，两处同形）。
 */
export function userOptionValue(strategyId: string): string {
  return `user:${strategyId}`;
}

/** `user:<id>` → id；不是用户策略则 null */
export function parseUserOption(value: string): string | null {
  return value.startsWith("user:") ? value.slice("user:".length) : null;
}

/** 策略显示名。用户策略没有内置名，由调用方补上具体名字（`strategy_name` 在报告 meta 里） */
export function strategyLabel(strategy: Strategy): string {
  if (strategy === "user") return "用户策略";
  return STRATEGIES.find((item) => item.value === strategy)?.label ?? strategy;
}

export function pitModeLabel(mode: PitMode): string {
  return PIT_MODES.find((item) => item.value === mode)?.label ?? mode;
}
