"use client";

import { useEffect, useRef, useState } from "react";

import { api, describeError } from "@/lib/api";
import { detailGroups, stampRows, DIRECTION, type DetailRow } from "@/lib/event-detail";
import { EMPTY, eventStamp, num } from "@/lib/format";
import { typeLabel } from "@/lib/event-feed";
import type { EventDetail } from "@/lib/types";

/** 抽屉要开的那一条。`day` 可省：服务端会取最新那条并把实际日期回显在详情里 */
export interface DetailTarget {
  eventId: string;
  day?: string | null;
}

/**
 * 事件详情抽屉（M10d）：右侧滑出的一条事件**完整证据链**。
 *
 * 三处刻意：
 *  1. **自己取数**（只吃 `eventId` + 可选 `day`）——快讯流与个股页的事件表因此共用同一个
 *     组件，不必各自把行数据搬过来；
 *  2. **`aria-modal` + Esc + 点遮罩关闭**：这是本项目第一个浮层，先按最小可用的键盘可达性做
 *     （打开时聚焦面板、Esc 关闭）；**不做焦点陷阱**——内容里没有表单与危险动作，
 *     为一个只读浮层引一套焦点管理不划算，如实记在 §11.4；
 *  3. **不编造**：任何空字段都由纯函数给 `EMPTY`（—），组件不做二次判断。
 */
export function EventDetailSheet({
  target,
  onClose,
}: {
  target: DetailTarget | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<EventDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!target) {
      setDetail(null);
      setError(null);
      return;
    }
    let alive = true;
    setDetail(null);
    setError(null);
    api
      .eventDetail(target.eventId, target.day)
      .then((body) => alive && setDetail(body))
      .catch((exc: unknown) => alive && setError(describeError(exc, "读取事件详情失败")));
    return () => {
      alive = false;
    };
  }, [target]);

  useEffect(() => {
    if (!target) return;
    panel.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [target, onClose]);

  if (!target) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end" data-detail="sheet">
      {/* 遮罩：点它关闭。`aria-hidden` —— 它没有任何语义，只是点击区 */}
      <button
        type="button"
        aria-label="关闭详情"
        data-detail="backdrop"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-black/30"
      />
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label="事件详情"
        className="relative h-full w-full max-w-[560px] overflow-y-auto border-l border-border bg-card p-4 shadow-xl outline-none"
      >
        <div className="flex items-start justify-between gap-3">
          <span className="text-sm font-medium">事件详情</span>
          <button
            type="button"
            data-detail="close"
            onClick={onClose}
            className="rounded-[var(--radius)] px-2 py-0.5 text-xs text-ink-3 hover:bg-muted hover:text-foreground"
          >
            关闭
          </button>
        </div>

        {error ? (
          <p className="mt-4 text-sm text-destructive">{error}</p>
        ) : detail === null ? (
          <p className="mt-4 text-sm text-ink-3">读取中…</p>
        ) : (
          <article className="mt-3">
            <header data-detail="head" className="border-b border-border/60 pb-3">
              <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
                <span className="rounded-[2px] border border-border px-1">
                  {typeLabel(detail.event_type)}
                </span>
                {detail.importance_score !== null ? (
                  <span className="num" title="平台给的重要度（0–100）">
                    {num(detail.importance_score, { digits: 0 })}
                  </span>
                ) : null}
                {detail.direction_norm ? (
                  <span className={DIRECTION[detail.direction_norm]?.className ?? "text-ink-3"}>
                    {DIRECTION[detail.direction_norm]?.label ?? detail.direction_norm}
                  </span>
                ) : null}
                <span className="num">{detail.date ? `落在 ${detail.date}` : EMPTY}</span>
              </div>
              <h2 className="mt-2 text-base leading-relaxed font-medium">{detail.title ?? detail.event_id}</h2>
              <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-3">
                {stampRows(detail).map((row) => (
                  <div key={row.label} className="flex gap-1">
                    <dt>{row.label}</dt>
                    <dd className="num">{row.value}</dd>
                  </div>
                ))}
              </dl>
            </header>

            {/* 正文：快讯流那层不带 summary（D1），全文只在这里出 */}
            <section data-detail="body" className="border-b border-border/60 py-3">
              <p className="text-sm leading-relaxed whitespace-pre-line">
                {detail.summary ?? "（这条没有正文摘要）"}
              </p>
            </section>

            {detailGroups(detail).map((group) => (
              <section key={group.key} data-detail={group.key} className="border-b border-border/60 py-3 last:border-0">
                <h3 className="text-xs font-medium text-ink-2">{group.title}</h3>
                <dl className="mt-1.5 space-y-1">
                  {group.rows.map((row, index) => (
                    <DetailLine key={`${row.label}-${index}`} row={row} />
                  ))}
                </dl>
              </section>
            ))}

            <p className="num pt-1 text-[11px] text-ink-3">
              事发至可得：{eventStamp(detail.event_time)} → {eventStamp(detail.available_at)}
            </p>
          </article>
        )}
      </div>
    </div>
  );
}

function DetailLine({ row }: { row: DetailRow }) {
  return (
    <div className={row.wide ? "text-xs" : "flex gap-3 text-xs"}>
      <dt className="shrink-0 text-ink-3">{row.label}</dt>
      <dd className={row.wide ? "mt-0.5 break-words text-ink-2" : "break-words text-ink-2"}>
        {row.href ? (
          <a
            href={row.href}
            target="_blank"
            rel="noreferrer"
            data-detail="source-link"
            className="break-all text-brand underline underline-offset-2"
          >
            {row.value} ↗
          </a>
        ) : (
          row.value
        )}
      </dd>
    </div>
  );
}
