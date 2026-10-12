"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import { offsetOf } from "@/lib/paging";
import { PAGE_SIZE } from "@/lib/stocks";
import type { CrossSection } from "@/lib/types";

/** 搜索防抖。与自选股面板同值（300ms）——同一种交互用同一个手感。 */
export const SEARCH_DEBOUNCE_MS = 300;

/**
 * 全市场 / 某行业列表的状态（M10c）。
 *
 * **一次只取一页**（`offset = offsetOf(page)`），翻页是**替换**不是追加——列表是
 * 「第 N 页」而不是流水账，保留上一页的行会让「第 3 页」看着像 150 行。
 *
 * `page` 由调用方给（它同时是地址栏里的 `?page=`），这里只把它换算成 offset——**请求前不夹
 * 上界**，首屏还不知道总数，夹了会把 `?page=7` 这种深链夹没；上界由 `Pager` 在渲染层夹。
 */
export function useStocksList(params: {
  industry: string | null;
  sort: string;
  order: "desc" | "asc";
  q: string;
  page: number;
}) {
  const { industry, sort, order, q } = params;
  const [section, setSection] = useState<CrossSection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(
    async (target: number) => {
      const ticket = ++seq.current;
      setLoading(true);
      setError(null);
      try {
        const next = await api.crossSection({
          industry: industry ?? undefined,
          sort,
          order,
          q: q || undefined,
          limit: PAGE_SIZE,
          offset: offsetOf(target, PAGE_SIZE),
        });
        if (ticket !== seq.current) return;
        setSection(next);
      } catch (cause) {
        if (ticket !== seq.current) return;
        setError(describeError(cause, "取标的列表失败"));
        setSection(null);
      } finally {
        if (ticket === seq.current) setLoading(false);
      }
    },
    [industry, sort, order, q],
  );

  useEffect(() => {
    // 页码在请求前**不夹上界**（首屏还不知道总数，夹了会把 `?page=7` 这种深链夹没）；
    // 上界由 `Pager` 在渲染层夹，`clampPage` 的注释里有完整理由。
    void load(params.page);
  }, [load, params.page]);

  useEffect(() => () => void ++seq.current, []);

  return { section, loading, error, reload: () => void load(params.page) };
}

/** 搜索词防抖：边输边查会每敲一个字打一次后端，而截面的 `q` 是服务端过滤。 */
export function useDebounced(value: string, delay = SEARCH_DEBOUNCE_MS): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
