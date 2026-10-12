"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import type { MarketOverview, MarketSectors } from "@/lib/types";

/**
 * 研究首页的数据：两条公开端点**并行取**，一起就绪才算加载完（同一屏的两半）。
 *
 * 在途作废用**序号**（同 `use-factor` / `use-optimize`）：改样本下限时上一发可能还没回来，
 * 晚到的旧结果不能覆盖新的。
 *
 * `minSymbols` 变了就重取——它是服务端参数（小样本的均值会霸榜），不是前端过滤：
 * 前端过滤会把「被滤掉的那些」也算进覆盖率的分母里，两个数就对不上了。
 */
export function useHome(minSymbols: number) {
  const [overview, setOverview] = useState<MarketOverview | null>(null);
  const [sectors, setSectors] = useState<MarketSectors | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async (min: number) => {
    const ticket = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const [nextOverview, nextSectors] = await Promise.all([
        api.marketOverview(),
        api.marketSectors({ minSymbols: min }),
      ]);
      if (ticket !== seq.current) return;
      setOverview(nextOverview);
      setSectors(nextSectors);
    } catch (cause) {
      if (ticket !== seq.current) return;
      setError(describeError(cause, "取大盘数据失败"));
      setOverview(null);
      setSectors(null);
    } finally {
      if (ticket === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(minSymbols);
  }, [load, minSymbols]);

  // 卸下时作废在途回调，避免对已卸载组件 setState
  useEffect(() => () => void ++seq.current, []);

  return { overview, sectors, loading, error, reload: () => void load(minSymbols) };
}
