"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import { appendFeed, importanceOf, latestDay, PAGE_SIZE, typesOf } from "@/lib/event-feed";
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
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const types = typesOf(typeKey);
  const minImportance = importanceOf(importanceKey);

  /** 拉一页。`offset` 为 null 表示「重取第一页」。 */
  const load = useCallback(
    async (target: string, offset: number | null) => {
      const ticket = ++seq.current;
      if (offset === null) setLoading(true);
      else setMore(true);
      setError(null);
      try {
        const next = await api.eventFeed({
          date: target,
          types: types ?? undefined,
          minImportance: minImportance ?? undefined,
          limit: PAGE_SIZE,
          offset: offset ?? 0,
        });
        if (ticket !== seq.current) return;
        setFeed((current) => (offset === null ? appendFeed(null, next) : appendFeed(current, next)));
      } catch (cause) {
        if (ticket !== seq.current) return;
        setError(describeError(cause, "取事件流失败"));
        if (offset === null) setFeed(null);
      } finally {
        if (ticket === seq.current) {
          setLoading(false);
          setMore(false);
        }
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

  // 日期或筛选变了就重取第一页（`day` 还没定下来时不动）
  useEffect(() => {
    if (day) void load(day, null);
  }, [day, load]);

  // 卸下时作废在途回调
  useEffect(() => () => void ++seq.current, []);

  return {
    calendar,
    day,
    feed,
    loading,
    more,
    error,
    select: setDay,
    loadMore: () => {
      if (day && feed) void load(day, feed.offset);
    },
  };
}
