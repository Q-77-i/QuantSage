"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { builtinDescriptors, userDescriptors } from "@/lib/optimize-form";
import type { BatchPick, ParamDescriptor } from "@/lib/optimize-form";
import { STRATEGIES } from "@/lib/backtest-form";
import type { Strategy, StrategySummary } from "@/lib/types";

/**
 * 可选策略目录：两条内置 + 我的策略。
 *
 * 与工作台不同，这里**不下载源码**——网格要的是参数名与值域，那些从
 * `POST /strategies/check` 的 `meta.params` 拿（纯函数、不落库）。
 * 列表接口本来就不带 code（列表不为每行拖一份源码），所以用户策略是两次请求：
 * 列表 → 选中时再取那一条拿 schema。缓存按 id 记，来回切换不会反复打。
 */
export function useUserStrategies(): { items: StrategySummary[]; error: string | null } {
  const [items, setItems] = useState<StrategySummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .strategies()
      .then((rows) => {
        if (alive) setItems(rows);
      })
      // 取不到就只是「没有我的策略」——网格跑内置策略照样能用，不该拦在这一步
      .catch(() => {
        if (alive) setError("读取我的策略失败");
      });
    return () => {
      alive = false;
    };
  }, []);
  return { items, error };
}

/** 批量模式的复选框列表：内置两条 + 我的策略 */
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

const cache = new Map<string, ParamDescriptor[]>();

/**
 * 当前选中策略的参数表（含值域）。内置查本地表，用户策略取一次源码再静态解析。
 *
 * `loading` 期间**不要**拿空表去校验：那会把用户已经填好的轴判成「不接受参数」，
 * 一闪而过的红字比不显示还糟。
 */
export function useDescriptors(
  strategy: Strategy,
  strategyId: string | null,
): { loading: boolean; descriptors: ParamDescriptor[]; error: string | null } {
  const key = strategy === "user" ? (strategyId ?? "") : strategy;
  const initial = strategy === "user" ? cache.get(key) : builtinDescriptors(strategy);
  const [state, setState] = useState<{ loading: boolean; descriptors: ParamDescriptor[]; error: string | null }>(
    () => ({ loading: initial === undefined, descriptors: initial ?? [], error: null }),
  );

  useEffect(() => {
    if (strategy !== "user") {
      setState({ loading: false, descriptors: builtinDescriptors(strategy), error: null });
      return;
    }
    if (!strategyId) {
      setState({ loading: false, descriptors: [], error: null });
      return;
    }
    const cached = cache.get(strategyId);
    if (cached) {
      setState({ loading: false, descriptors: cached, error: null });
      return;
    }

    let alive = true;
    setState({ loading: true, descriptors: [], error: null });
    api
      .strategy(strategyId)
      .then((detail) => api.checkStrategy(detail.code))
      .then((check) => {
        if (!alive) return;
        if (!check.meta) {
          // `PARAMS` 解析不出来：这不是「没有参数」，是这条策略现在跑不了
          setState({ loading: false, descriptors: [], error: "这条策略的参数声明解析不了，先去工作台检查" });
          return;
        }
        const descriptors = userDescriptors(check.meta.params);
        cache.set(strategyId, descriptors);
        setState({ loading: false, descriptors, error: null });
      })
      .catch(() => {
        if (alive) setState({ loading: false, descriptors: [], error: "读取策略参数失败" });
      });
    return () => {
      alive = false;
    };
  }, [strategy, strategyId]);

  return state;
}
