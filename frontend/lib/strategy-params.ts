/**
 * 策略参数的统一形状：内置与用户策略在同一张表上渲染与校验。
 *
 * 两边的来源不同、语义相同：
 *   * 内置 → 前端常量 `PARAMS`（`lib/backtest-form.ts`，与后端注册表两个策略对齐）；
 *   * 用户策略 → **源码里 `PARAMS` 字面量的静态解析**（`POST /strategies/check` 的
 *     `meta.params`，纯函数、不落库、不执行代码）。
 *
 * 优化页 M5b 起就在用这一层；M4c-3 把回测页也接上——**两页各转一次必然漂移**
 * （值域提示、标签、默认值三处都会各写一份）。
 */

import { PARAMS } from "./backtest-form";
import type { BuiltinStrategy, ParamSpec } from "./types";

export interface ParamDescriptor {
  key: string;
  label: string;
  /** M4c-3 加：用户策略可以有 `bool`（内置的全是数字），回测页按它决定渲染输入框还是开关 */
  type: "number" | "bool";
  min: number | null;
  max: number | null;
  /**
   * 初值：数字参数给数值、布尔参数给开关；schema 没给数字默认值时是 null。
   *
   * 布尔默认值**必须带出来**（M4c-3）：前端总是显式发参数，若开关默认成 false 而作者
   * 写的是 `default: True`，跑出来的东西与策略作者的本意不一致，且没人看得出来。
   */
  fallback: number | boolean | null;
}

export function builtinDescriptors(strategy: BuiltinStrategy): ParamDescriptor[] {
  return PARAMS[strategy].map((field) => ({
    key: field.key,
    label: field.label,
    type: "number",
    min: field.min,
    max: field.max ?? null,
    fallback: field.fallback,
  }));
}

/** 用户策略的 schema（`StrategyMeta.params`）→ 同一形状 */
export function userDescriptors(params: Record<string, ParamSpec>): ParamDescriptor[] {
  return Object.entries(params).map(([key, spec]) => ({
    key,
    label: spec.label || key,
    type: spec.type === "bool" ? "bool" : "number",
    min: spec.min,
    max: spec.max,
    fallback: spec.default,
  }));
}
