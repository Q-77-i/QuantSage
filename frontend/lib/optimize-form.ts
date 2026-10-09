/**
 * 网格 / 批量表单：状态 ↔ 请求体，以及与后端**同规则**的边界校验。
 *
 * 为什么前端要再实现一遍后端的规则：后端的规则是「**整单 422，不静默跳过**」
 * （SPEC §6 M5b）——`fast=[5,20] × slow=[10,30]` 里那个非法格会让**整次请求**被拒。
 * 若前端不先算一遍，用户点了「运行」才收到一句 422，既不知道是哪一格、也不知道怎么改。
 * 这里的 `validateGrid` 与后端 `batch.grid_cells` 是同一套判据，**逐条对应**：
 *
 * | 规则 | 后端 | 前端 |
 * |---|---|---|
 * | 轴数 1–2 | `MAX_AXES` | `MAX_AXES` |
 * | 轴参数须在策略参数集内 | `known` | `knownParams`（内置查表 / 用户策略查 `PARAMS` schema） |
 * | 轴参数不得与基座撞键 | `grid_cells` | `validateGrid` |
 * | 轴值去重后 ≥2、原样不重复 | `grid_cells` | `parseAxisValues` + `validateGrid` |
 * | 总格数 2–100 | `MAX_CELLS` | `MAX_CELLS` |
 * | **展开后每格都过参数校验** | `normalize` | `cellErrors` |
 *
 * 「最终裁决权仍在后端」这条不变：前端只是把「点了才知道」提前成「输的时候就知道」，
 * 与自选股的判重同一姿态（那里也是前端提示、后端 409 裁决）。
 */

import type {
  BatchRequest,
  BuiltinStrategy,
  GridRequest,
  OptimizeAxis,
  Strategy,
  StoredOptimizeRequest,
} from "./types";
import { PARAMS, type ParamField } from "./backtest-form";
import { validateSymbol } from "./watchlist";

/** 与后端 `batch.MAX_*` 同值——改一处必须改两处，故都写在这一个文件里 */
export const MAX_AXES = 2;
export const MAX_CELLS = 100;
export const MAX_SYMBOLS = 20;
export const MAX_BATCH_STRATEGIES = 10;

/** 一条参数轴在**表单里**的样子：值是一串原文，用户还在打的时候不能当数字看 */
export interface AxisInput {
  param: string;
  values: string;
}

export interface GridFormState {
  strategy: Strategy;
  /** `strategy === "user"` 时必填 */
  strategyId: string | null;
  symbol: string;
  start: string;
  end: string;
  /** 基座参数（不扫的那些）。刷新策略时整组重建——两套参数没有可复用的语义 */
  baseParams: Record<string, string>;
  axes: AxisInput[];
}

export interface BatchFormState {
  /** 多行或逗号分隔的原文 */
  symbols: string;
  /** 选中的策略键：内置用策略名，用户策略用 `user:<id>` */
  picks: string[];
  start: string;
  end: string;
}

/** 策略参数的展示与值域信息，内置与用户策略在这里统一（用户策略由 `PARAMS` schema 转来） */
export interface ParamDescriptor {
  key: string;
  label: string;
  min: number | null;
  max: number | null;
  fallback: number | null;
}

export function builtinDescriptors(strategy: BuiltinStrategy): ParamDescriptor[] {
  return PARAMS[strategy].map((field: ParamField) => ({
    key: field.key,
    label: field.label,
    min: field.min,
    max: field.max ?? null,
    fallback: field.fallback,
  }));
}

/** 用户策略的 schema（`StrategyMeta.params`）→ 同一形状 */
export function userDescriptors(
  params: Record<string, { type: string; default: number | boolean; min: number | null; max: number | null; label: string }>,
): ParamDescriptor[] {
  return Object.entries(params).map(([key, spec]) => ({
    key,
    label: spec.label || key,
    min: spec.min,
    max: spec.max,
    fallback: typeof spec.default === "number" ? spec.default : null,
  }));
}

// ── 取值解析 ────────────────────────────────────────────────

/**
 * 「3, 5, 8」→ `[3, 5, 8]`。逗号、中文逗号、空白都当分隔符——用户不会只按一种打。
 * 空段（`3,,5`）算错误而不是静默忽略：那是打字打漏了，跳过会悄悄少一格。
 */
export function parseAxisValues(raw: string): { values: number[]; error: string | null } {
  // 空段检查必须**按单个分隔符**切：`split(/[,，]+/)` 会把 `3,,5` 里的连续逗号
  // 一起吃掉，"空项"就永远查不出来（第一版就是这么写的，用例逮到的）
  const rawParts = raw.trim() === "" ? [] : raw.trim().split(/[,，]/).map((p) => p.trim());
  if (rawParts.some((part) => part === "")) {
    return { values: [], error: "取值里有空的项（多打了一个逗号？）" };
  }

  const parts = raw
    .split(/[,，\s]+/)
    .map((part) => part.trim())
    .filter((part) => part !== "");
  if (parts.length === 0) return { values: [], error: "请填至少 2 个取值，用逗号分隔" };

  const values: number[] = [];
  for (const part of parts) {
    const value = Number(part);
    if (!Number.isFinite(value)) return { values: [], error: `「${part}」不是数字` };
    values.push(value);
  }
  return { values, error: null };
}

/** 数值列表的展开（笛卡尔积）。返回的是**格数**与每格的轴取值组合 */
export function expandAxisValues(axes: { param: string; values: number[] }[]): Record<string, number>[] {
  let combos: Record<string, number>[] = [{}];
  for (const axis of axes) {
    combos = combos.flatMap((combo) =>
      axis.values.map((value) => ({ ...combo, [axis.param]: value })),
    );
  }
  return combos;
}

/**
 * **实际生效的基座参数** = 填了值的、且**没有被选作参数轴**的那些。
 *
 * 这一条是界面验证逮出来的：参数被选作轴之后，它的基座输入框会收起（用户看不见了），
 * 但 state 里那份默认值还在——请求照发，后端按「轴参数与基座撞键」判 422。
 * **界面上看不见的东西不该进请求**：`effectiveBaseParams` 是校验与请求体共用的唯一口径。
 */
export function effectiveBaseParams(
  state: Pick<GridFormState, "baseParams" | "axes">,
): Record<string, number> {
  const swept = new Set(state.axes.map((axis) => axis.param).filter(Boolean));
  const base: Record<string, number> = {};
  for (const [key, raw] of Object.entries(state.baseParams)) {
    if (raw.trim() === "" || swept.has(key)) continue;
    base[key] = Number(raw);
  }
  return base;
}

/** 当前表单会展开出多少格（0 = 还填不出） */
export function gridSize(axes: AxisInput[]): number {
  const parsed = axes.map((axis) => parseAxisValues(axis.values));
  if (parsed.some((item) => item.error || item.values.length === 0)) return 0;
  return parsed.reduce((total, item) => total * item.values.length, 1);
}

// ── 校验 ────────────────────────────────────────────────────

/**
 * 单格参数校验：逐字段值域 + **策略自己的跨字段规则**。
 *
 * 跨字段只有双均线有（`fast < slow`）——后端内置策略的 `validate_params` 也是这一条
 * （`strategies/ma_cross.py`）。用户策略的跨字段约束不进 schema，后端在网格路径上
 * 只按 schema 校验（`params.validate_params`），故这里同样只管值域。
 */
export function cellErrors(
  values: Record<string, number>,
  strategy: Strategy,
  descriptors: ParamDescriptor[],
): string[] {
  const errors: string[] = [];
  for (const field of descriptors) {
    const value = values[field.key];
    if (value === undefined) continue;
    if (field.min !== null && value < field.min) {
      errors.push(`${field.label || field.key}=${value} 小于 ${field.min}`);
    }
    if (field.max !== null && value > field.max) {
      errors.push(`${field.label || field.key}=${value} 大于 ${field.max}`);
    }
  }
  if (strategy === "ma_cross") {
    const { fast, slow } = values;
    if (fast !== undefined && slow !== undefined && fast >= slow) {
      errors.push(`fast=${fast} 不小于 slow=${slow}`);
    }
  }
  return errors;
}

export interface GridValidation {
  /** 字段级错误（键与表单控件对应：`symbol` / `start` / `axis-0` / `axis-1` / `grid`） */
  errors: Record<string, string>;
  /** 展开后的格数（校验不过时为 0） */
  size: number;
}

export function validateGrid(
  state: GridFormState,
  descriptors: ParamDescriptor[],
): GridValidation {
  const errors: Record<string, string> = {};

  const symbolError = validateSymbol(state.symbol);
  if (symbolError) errors.symbol = symbolError;
  if (state.strategy === "user" && !state.strategyId) errors.strategy = "请选择一条我的策略";
  if (state.start && state.end && state.start > state.end) errors.end = "终点早于起点";

  if (state.axes.length === 0) {
    errors.grid = "至少要有 1 条参数轴";
  } else if (state.axes.length > MAX_AXES) {
    errors.grid = `参数轴最多 ${MAX_AXES} 条`;
  }

  const known = new Set(descriptors.map((item) => item.key));
  // 撞键在界面上**结构性地不可能**（选作轴之后基座输入框就收起了），所以这里不比这一条——
  // 后端的 `grid_cells` 仍然管它，那是给直接打 API 的人准备的
  const seen = new Set<string>();
  const parsedAxes: OptimizeAxis[] = [];

  state.axes.slice(0, MAX_AXES).forEach((axis, index) => {
    const field = `axis-${index}`;
    if (!axis.param) {
      errors[field] = "请选择参数";
      return;
    }
    if (!known.has(axis.param)) {
      const allowed = [...known].join("、") || "（该策略没有参数）";
      errors[field] = `该策略不接受参数 ${axis.param}；可用：${allowed}`;
      return;
    }
    if (seen.has(axis.param)) {
      errors[field] = `${axis.param} 出现了不止一次`;
      return;
    }
    seen.add(axis.param);

    const { values, error } = parseAxisValues(axis.values);
    if (error) {
      errors[field] = error;
      return;
    }
    if (values.length < 2) {
      errors[field] = "至少要有 2 个取值";
      return;
    }
    if (new Set(values).size !== values.length) {
      errors[field] = `取值有重复：${values.join("、")}`;
      return;
    }
    parsedAxes.push({ param: axis.param, values });
  });

  if (Object.keys(errors).length > 0) return { errors, size: 0 };

  const size = parsedAxes.reduce((total, axis) => total * axis.values.length, 1);
  if (size < 2) {
    errors.grid = "网格至少要有 2 格";
    return { errors, size: 0 };
  }
  if (size > MAX_CELLS) {
    errors.grid = `网格共 ${size} 格，超过单次上限 ${MAX_CELLS} 格`;
    return { errors, size: 0 };
  }

  // **逐格**过一遍参数——后端就是这么判的（任一格非法即整单 422）
  const base = effectiveBaseParams(state);
  for (const [index, combo] of expandAxisValues(parsedAxes).entries()) {
    const issues = cellErrors({ ...base, ...combo }, state.strategy, descriptors);
    if (issues.length > 0) {
      const shown = Object.entries(combo)
        .map(([key, value]) => `${key}=${value}`)
        .join("、");
      errors.grid = `第 ${index + 1} 格（${shown}）参数不合法：${issues.join("；")}`;
      return { errors, size: 0 };
    }
  }

  return { errors, size };
}

/** 表单 → 网格请求体。**调用前必须已通过 `validateGrid`** */
export function buildGridRequest(state: GridFormState): GridRequest {
  return {
    strategy: state.strategy,
    ...(state.strategy === "user" && state.strategyId ? { strategy_id: state.strategyId } : {}),
    symbol: state.symbol,
    ...(state.start ? { start: state.start } : {}),
    ...(state.end ? { end: state.end } : {}),
    // 网格不收 `both`（那等于把每格工作量翻倍），故此处固定 pit
    pit_mode: "pit",
    params: effectiveBaseParams(state),
    axes: state.axes.map((axis) => ({
      param: axis.param,
      values: parseAxisValues(axis.values).values,
    })),
  };
}

/** 存下来的请求 → 表单（`?run=` 重开时回填）。**不重跑**，只是把当时的样子还回去 */
export function gridFormFromRequest(request: StoredOptimizeRequest): GridFormState {
  return {
    strategy: request.strategy ?? "ma_cross",
    strategyId: request.strategy_id ?? null,
    symbol: request.symbol ?? "",
    start: request.start ?? "",
    end: request.end ?? "",
    baseParams: Object.fromEntries(
      Object.entries(request.params ?? {}).map(([key, value]) => [key, String(value)]),
    ),
    axes: (request.axes ?? []).map((axis) => ({
      param: axis.param,
      values: axis.values.join(", "),
    })),
  };
}

// ── 批量 ────────────────────────────────────────────────────

/** 多行 / 逗号分隔的标的原文 → 去重后的列表 + 逐条错误 */
export function parseSymbolList(raw: string): { symbols: string[]; errors: string[] } {
  const parts = raw
    .split(/[,，\s]+/)
    .map((part) => part.trim())
    .filter((part) => part !== "");
  const errors: string[] = [];
  const symbols: string[] = [];
  for (const part of parts) {
    const issue = validateSymbol(part);
    if (issue) {
      errors.push(issue);
      continue;
    }
    if (symbols.includes(part)) {
      errors.push(`${part} 重复了`);
      continue;
    }
    symbols.push(part);
  }
  if (symbols.length > MAX_SYMBOLS) errors.push(`标的数最多 ${MAX_SYMBOLS} 个（当前 ${symbols.length} 个）`);
  return { symbols, errors };
}

export function validateBatch(
  state: BatchFormState,
  picks: BatchPick[],
): { errors: Record<string, string>; total: number } {
  const errors: Record<string, string> = {};
  const { symbols, errors: symbolErrors } = parseSymbolList(state.symbols);
  if (symbolErrors.length > 0) errors.symbols = symbolErrors.join("；");
  if (symbols.length === 0 && symbolErrors.length === 0) errors.symbols = "请填至少一个标的";
  if (state.start && state.end && state.start > state.end) errors.end = "终点早于起点";

  const chosen = picks.filter((pick) => state.picks.includes(pick.key));
  if (chosen.length === 0) errors.picks = "请至少选一条策略";
  if (chosen.length > MAX_BATCH_STRATEGIES) {
    errors.picks = `策略最多 ${MAX_BATCH_STRATEGIES} 条`;
  }

  const total = symbols.length * chosen.length;
  if (Object.keys(errors).length === 0 && total > MAX_CELLS) {
    errors.picks = `${symbols.length} 个标的 × ${chosen.length} 条策略 = ${total} 格，超过单次上限 ${MAX_CELLS} 格`;
  }
  return { errors, total: Object.keys(errors).length === 0 ? total : 0 };
}

/** 批量可选的策略：内置两条 + 我的策略若干 */
export interface BatchPick {
  key: string;
  label: string;
  strategy: Strategy;
  strategyId?: string;
}

export function buildBatchRequest(state: BatchFormState, picks: BatchPick[]): BatchRequest {
  const { symbols } = parseSymbolList(state.symbols);
  return {
    symbols,
    strategies: picks
      .filter((pick) => state.picks.includes(pick.key))
      .map((pick) => ({
        strategy: pick.strategy,
        ...(pick.strategy === "user" && pick.strategyId ? { strategy_id: pick.strategyId } : {}),
      })),
    ...(state.start ? { start: state.start } : {}),
    ...(state.end ? { end: state.end } : {}),
    // 批量同样不收 `both`（与网格同一理由）
    pit_mode: "pit",
  };
}

export function batchFormFromRequest(request: StoredOptimizeRequest): BatchFormState {
  return {
    symbols: (request.symbols ?? []).join("\n"),
    picks: (request.strategies ?? []).map((item) =>
      item.strategy === "user" && item.strategy_id ? `user:${item.strategy_id}` : item.strategy,
    ),
    start: request.start ?? "",
    end: request.end ?? "",
  };
}
