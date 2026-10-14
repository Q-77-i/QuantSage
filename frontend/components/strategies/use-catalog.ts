"use client";

/**
 * 策略目录与参数 schema（M5b 起优化页在用，M4c-3 起回测页也用）。
 *
 * 与工作台不同，这里**不下载源码给用户看**——回测页与优化页要的是参数名与值域，
 * 那些从 `POST /strategies/check` 的 `meta.params` 拿（纯函数、不落库、不执行代码）。
 * 列表接口本来就不带 code（列表不为每行拖一份源码），所以用户策略是两次请求：
 * 列表 → 选中时再取那一条拿 schema。缓存按 id 记，来回切换不会反复打。
 *
 * 放在 `components/strategies/` 而不是某一页的目录里：两个消费方（`/optimize`、`/backtest`）
 * 都要它，挂在任一边都会让另一边 import 一个「别人家的」模块。
 */

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { builtinDescriptors, userDescriptors } from "@/lib/strategy-params";
import type { ParamDescriptor } from "@/lib/strategy-params";
import type { Strategy, StrategySummary } from "@/lib/types";

/** 我的策略（摘要，不带源码）。取不到就只是「没有我的策略」——内置策略照样能用，不拦在这一步 */
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
      .catch(() => {
        if (alive) setError("读取我的策略失败");
      });
    return () => {
      alive = false;
    };
  }, []);
  return { items, error };
}

const cache = new Map<string, ParamDescriptor[]>();

/**
 * 当前选中策略的参数表（含值域）。内置查本地表，用户策略取一次源码再静态解析。
 *
 * `loading` 期间**不要**拿空表去校验：那会把用户已经填好的参数判成「不接受参数」，
 * 一闪而过的红字比不显示还糟。
 */
export function useDescriptors(
  strategy: Strategy,
  strategyId: string | null,
): { loading: boolean; descriptors: ParamDescriptor[]; error: string | null } {
  const key = strategy === "user" ? (strategyId ?? "") : strategy;
  const initial = strategy === "user" ? cache.get(key) : builtinDescriptors(strategy);
  const [state, setState] = useState<{
    loading: boolean;
    descriptors: ParamDescriptor[];
    error: string | null;
  }>(() => ({ loading: initial === undefined, descriptors: initial ?? [], error: null }));

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
          setState({
            loading: false,
            descriptors: [],
            error: "这条策略的参数声明解析不了，先去工作台检查",
          });
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

/** 测试与「换了一条策略」时清缓存用（缓存键是策略 id，源码改了要让它失效） */
export function clearDescriptorCache(): void {
  cache.clear();
}
