"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import { importanceOf, latestDay, PAGE_SIZE, typesOf } from "@/lib/event-feed";
import { offsetOf } from "@/lib/paging";
import type { EventCalendar, EventFeed } from "@/lib/types";

/**
 * 事件日历 / 快讯流的状态（M10b）。
 *
 * **与 `useHome` 分开写**：那个是「一次并行取两条、参数变就重取」，这里是三种驱动方式
 * ——日历只在挂载时取一次、快讯流随「日期 + 筛选」重取、翻页是**追加**而不是替换。
 * 塞进一个 hook 会把它「一起就绪才算加载完」的语义搅坏。
 *
 * 筛选参数以**档位键**（纯字符串）进出：数组每次渲染都是新身份，当依赖会无限重取。
 */
export function useEvents(typeKey: string, importanceKey: string) {
  const [calendar, setCalendar] = useState<EventCalendar | null>(null);
  const [day, setDay] = useState<string | null>(null);
  const [feed, setFeed] = useState<EventFeed | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const types = typesOf(typeKey);
  const minImportance = importanceOf(importanceKey);

  /** 拉某一页（**替换**而不是追加——列表是「第 N 页」不是流水账）。 */
  const load = useCallback(
    async (target: string, targetPage: number) => {
      const ticket = ++seq.current;
      setLoading(true);
      setError(null);
      try {
        const next = await api.eventFeed({
          date: target,
          types: types ?? undefined,
          minImportance: minImportance ?? undefined,
          limit: PAGE_SIZE,
          offset: offsetOf(targetPage, PAGE_SIZE),
        });
        if (ticket !== seq.current) return;
        setFeed(next);
      } catch (cause) {
        if (ticket !== seq.current) return;
        setError(describeError(cause, "取事件流失败"));
        setFeed(null);
      } finally {
        if (ticket === seq.current) setLoading(false);
      }
    },
    [types, minImportance],
  );

  // 日历只取一次：它跟着语料走，与筛选无关
  useEffect(() => {
    let alive = true;
    api
      .eventCalendar()
      .then((data) => {
        if (!alive) return;
        setCalendar(data);
        setDay((current) => current ?? latestDay(data));
      })
      .catch((cause) => {
        if (alive) setError(describeError(cause, "取事件日历失败"));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 换筛选回第一页——否则「第 5 页的公告筛选」多半是空白
  useEffect(() => {
    setPage(1);
  }, [typeKey, importanceKey]);

  // 日期、页码或筛选变了就重取（`day` 还没定下来时不动）
  useEffect(() => {
    if (day) void load(day, page);
  }, [day, page, load]);

  // 卸下时作废在途回调
  useEffect(() => () => void ++seq.current, []);

  return {
    calendar,
    day,
    page,
    feed,
    loading,
    error,
    // 换日期同时回第一页（换一天还停在第 5 页多半是空白）
    select: (next: string) => {
      setDay(next);
      setPage(1);
    },
    goPage: setPage,
  };
}
