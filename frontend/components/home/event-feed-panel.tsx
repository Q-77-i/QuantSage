"use client";

import { useState } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { Row } from "@/components/research/panels";
import { Input, Select } from "@/components/ui/form-controls";
import { Pager } from "@/components/ui/pager";
import {
  daySummary,
  feedItemView,
  IMPORTANCE_CHOICES,
  PAGE_SIZE,
  stripDays,
  TYPE_CHOICES,
} from "@/lib/event-feed";
import { pageOf } from "@/lib/paging";
import { EMPTY, eventStamp } from "@/lib/format";
import { evidenceRows } from "@/lib/research";
import type { EventCalendar, EventFeed } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 事件日历 / 快讯流（M10b）。
 *
 * **两个「日历」与「流」是一件事的两半**：日期带回答「哪几天热闹」（实测单日 322–6,512 条，
 * 差别二十倍），下面的流回答「那天到底发生了什么」。
 *
 * 三条口径长在界面上，不是藏在文档里：
 *   · **类型默认「快讯（除公告）」**——平台的 `importance_score` 不跨类型可比，实测
 *     2026-09-30 按分数降序的**前 200 名全是公告**（验资报告一类常规披露拿满分）。
 *     不摘公告就没有「流」，只有一张公告列表；「公告」单列一档，随时切得回来。
 *   · **两个时间戳并列 + 滞后徽章**——`available_at − event_time` 是这个项目的护城河在
 *     界面上的落点（实测有 8 天后才可得的：9-30 的美国 PCE 数据，10-08 才进语料）。
 *   · **来源三元组每条都能展开**（PRD §5 硬要求），行渲染直接复用 M7 证据面板的 `Row`。
 */
export function EventFeedPanel({
  calendar,
  feed,
  day,
  page,
  loading,
  error,
  typeKey,
  importanceKey,
  onType,
  onImportance,
  onSelect,
  onGo,
}: {
  calendar: EventCalendar | null;
  feed: EventFeed | null;
  day: string | null;
  page: number;
  loading: boolean;
  error: string | null;
  typeKey: string;
  importanceKey: string;
  onType: (key: string) => void;
  onImportance: (key: string) => void;
  onSelect: (day: string) => void;
  onGo: (page: number) => void;
}) {
  const strip = stripDays(calendar, day);
  const items = feed?.items ?? [];

  return (
    <Section title="事件日历 / 快讯流" hint={daySummary(calendar, day)}>
      {/* 日期带：每天一根柱，柱高按窗口内最大值归一 */}
      <div className="mt-3 overflow-x-auto" data-strip="calendar">
        <div className="flex min-w-max items-end gap-1 pb-1">
          {strip.map((entry) => (
            <button
              key={entry.date}
              type="button"
              data-day={entry.date}
              data-selected={entry.selected ? "true" : "false"}
              aria-pressed={entry.selected}
              title={`${entry.date} ${entry.weekday} · ${entry.total} 条（重要度 ≥ ${calendar?.high_score ?? EMPTY} 的 ${entry.high} 条）`}
              onClick={() => onSelect(entry.date)}
              className={cn(
                "flex w-9 flex-col items-center gap-1 rounded-[var(--radius)] px-0.5 pt-1 pb-0.5",
                entry.selected ? "bg-brand/12" : "hover:bg-muted",
              )}
            >
              <span className="flex h-12 w-full items-end">
                <span
                  className={cn("w-full rounded-t-[2px]", entry.selected ? "bg-brand" : "bg-ink-3/40")}
                  style={{ height: `${entry.heightPct}%` }}
                />
              </span>
              <span className={cn("text-[10px]", entry.selected ? "text-brand" : "text-ink-3")}>
                {entry.label}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* 一行筛选管住下面的流（dataviz：filters in one row above the data） */}
      <div className="mt-2 flex flex-wrap items-center gap-3 text-sm" data-panel="feed-controls">
        <Select
          aria-label="事件类型"
          className="w-36"
          value={typeKey}
          onChange={(event) => onType(event.target.value)}
        >
          {TYPE_CHOICES.map((choice) => (
            <option key={choice.key} value={choice.key}>
              {choice.label}
            </option>
          ))}
        </Select>
        <Select
          aria-label="重要度下限"
          className="w-36"
          value={importanceKey}
          onChange={(event) => onImportance(event.target.value)}
        >
          {IMPORTANCE_CHOICES.map((choice) => (
            <option key={choice.key} value={choice.key}>
              {choice.label}
            </option>
          ))}
        </Select>
        {/* 原生 date：全站 11 处都是它，不引第三方日期库（既定取舍） */}
        <Input
          aria-label="跳到某一天"
          type="date"
          className="w-36"
          value={day ?? ""}
          min={calendar?.start ?? undefined}
          max={calendar?.end ?? undefined}
          onChange={(event) => event.target.value && onSelect(event.target.value)}
        />
      </div>

      {error ? (
        <p
          role="alert"
          className="mt-3 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </p>
      ) : loading && !feed ? (
        <div className="mt-3 h-32 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
      ) : items.length === 0 ? (
        <p className="mt-3 rounded-[var(--radius)] border border-border px-4 py-10 text-center text-sm text-ink-2">
          这一天在这个筛选下没有事件。
        </p>
      ) : (
        <>
          <ol className="mt-2" data-feed="items">
            {items.map((item) => (
              <FeedItem key={feedItemView(item).key} item={item} />
            ))}
          </ol>
          <Pager
            total={feed?.total ?? null}
            limit={feed?.limit ?? PAGE_SIZE}
            page={feed ? pageOf(feed.offset, feed.limit) : page}
            onGo={onGo}
          />
        </>
      )}
    </Section>
  );
}

function FeedItem({ item }: { item: EventFeed["items"][number] }) {
  const [open, setOpen] = useState(false);
  const view = feedItemView(item);
  return (
    <li
      data-event={item.event_id}
      data-type={item.event_type ?? "unknown"}
      className="border-b border-border/60 py-2.5 last:border-0"
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 shrink-0 rounded-[2px] border border-border px-1 text-[10px] text-ink-3">
          {view.typeText}
        </span>
        {view.score ? (
          <span className="num mt-0.5 shrink-0 text-[10px] text-ink-3" title="平台给的重要度（0–100）">
            {view.score}
          </span>
        ) : null}
        <span className="flex-1 text-sm">{view.title}</span>
      </div>

      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-3">
        {/* 事发与可得**并列**：两者之差就是这个项目的护城河 */}
        <span className="num" data-stamp="event">
          事发 {eventStamp(item.event_time)}
        </span>
        <span className="num" data-stamp="available">
          可得 {eventStamp(item.available_at)}
        </span>
        <span className="num" data-lag>
          {view.lag}
        </span>
        {view.symbols ? <span>{view.symbols}</span> : null}
        {view.industries ? <span>{view.industries}</span> : null}
        <button
          type="button"
          aria-expanded={open}
          data-toggle="sources"
          onClick={() => setOpen((value) => !value)}
          className="ml-auto rounded-[var(--radius)] px-1.5 hover:bg-muted hover:text-foreground"
        >
          {open ? "收起来源" : "来源"}
        </button>
      </div>

      {open ? (
        <dl className="mt-1.5 rounded-[var(--radius)] border border-border px-2.5 py-1.5 text-xs [&>div]:border-b [&>div]:border-border/60 [&>div:last-child]:border-0">
          {evidenceRows(item).map((row) => (
            <Row key={row.label} row={row} />
          ))}
        </dl>
      ) : null}
    </li>
  );
}
