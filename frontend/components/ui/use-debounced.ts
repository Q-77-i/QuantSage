"use client";

import { useEffect, useState } from "react";

/**
 * 搜索词防抖（M10c 建，E2c 挪到中立位置）。
 *
 * 原本住在 `components/stocks/use-stocks.ts`——admin 面板要用它时，从 stocks 的模块里取
 * 就有了「admin 依赖 stocks」的怪味道，故上提到这里（同 `components/ui/use-echart.ts` 的来历）。
 *
 * 仓里另外两处防抖**刻意保留各自一份**：工作台的检查是 400ms、自选股的探测是 300ms，
 * 三处的**语义不同**（搜索是「别每敲一个字就打一次后端」，检查是「停手再验」），
 * 合并成一个带参数的钩子只会让各自的理由模糊掉。这里共用的是「搜索」这一种。
 */
export const SEARCH_DEBOUNCE_MS = 300;

export function useDebounced(value: string, delay = SEARCH_DEBOUNCE_MS): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
