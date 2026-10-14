"use client";

import { STRATEGIES } from "@/lib/backtest-form";
import type { BatchPick } from "@/lib/optimize-form";
import type { Strategy, StrategySummary } from "@/lib/types";

/**
 * 批量模式的复选框列表：内置两条 + 我的策略。
 *
 * 列表与参数 schema 的取数（`useUserStrategies` / `useDescriptors`）M4c-3 起搬去了
 * `components/strategies/use-catalog.ts`——回测页也要用，留在优化页目录里会让回测页
 * import 一个「别人家的」模块。本模块只剩**优化页特有**的这件。
 */
export function batchPicks(userStrategies: StrategySummary[]): BatchPick[] {
  return [
    ...STRATEGIES.map((item) => ({
      key: item.value,
      label: item.label,
      strategy: item.value as Strategy,
    })),
    ...userStrategies.map((item) => ({
      key: `user:${item.id}`,
      label: item.name,
      strategy: "user" as Strategy,
      strategyId: item.id,
    })),
  ];
}
