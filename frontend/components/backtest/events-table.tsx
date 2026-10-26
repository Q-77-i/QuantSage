"use client";

import { useState } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { Cell, Row, TableShell } from "@/components/backtest/table";
import { EventDetailSheet, type DetailTarget } from "@/components/events/event-detail-sheet";
// 方向标签**一处定义**（M10d 起移到 lib/event-detail.ts）：详情抽屉与本表共用同一份配色，
// 各写一份迟早一处改了一处没改
import { DIRECTION } from "@/lib/event-detail";
import { count, eventStamp, num, shortHash } from "@/lib/format";
import type { EventCoverage, MarketEvent } from "@/lib/types";

/**
 * 事件语料表：窗口内该标的的事件（服务端默认最多 500 条，超出时标注截断）。
 *
 * `event_time` 与 `available_at` **并列成列**——两者之差就是本项目的护城河，比任何
 * 文字解释都直观。来源三元组是 PRD §5 的硬性要求：行尾显示 `original_source`，
 * 展开见 `source` 与 `content_hash`（后者截前 12 位，全串对人不产生信息）。
 */
export function EventsTable({
  events,
  coverage,
  window,
  limit,
  truncated,
}: {
  events: MarketEvent[];
  /** 本次回测依据的语料覆盖区间（来自报告 `meta`）：值是查出来的，不写死「约 3 个月」 */
  coverage?: EventCoverage | null;
  /** `/events` 实际生效的查询窗口（个股页用）；回测页没有它，退回 `coverage` */
  window?: { start: string | null; end: string | null } | null;
  limit?: number;
  /** 窗口内还有更早事件未返回（S1）：必须让读者看见，不静默截断 */
  truncated?: boolean;
}) {
  const windowSpan = window?.start && window?.end ? `${window.start} → ${window.end}` : null;
  const span = coverage?.start && coverage?.end ? `${coverage.start} → ${coverage.end}` : null;
  const scope = windowSpan ? `窗口 ${windowSpan}` : span ? `语料覆盖 ${span}` : null;
  const cut = truncated && limit ? ` · 仅显示最近 ${count(limit)} 条（已截断）` : "";
  // 详情抽屉（M10d）：状态放在表里，**两个页面（回测页与个股页）共用这一份接线**
  const [target, setTarget] = useState<DetailTarget | null>(null);
  return (
    <Section
      title="事件语料"
      hint={
        events.length
          ? `${count(events.length)} 条${scope ? ` · ${scope}` : ""}${cut}`
          : windowSpan
            ? `窗口 ${windowSpan} 内没有该标的的事件`
            : "该区间内没有事件"
      }
    >
      {events.length === 0 ? (
        <p className="mt-3 text-sm text-ink-2">
          {windowSpan
            ? `窗口 ${windowSpan} 内没有该标的的事件。换标的或放宽区间再看看。`
            : span
              ? `本地事件语料覆盖 ${span}，此区间内没有该标的的事件。换标的或放宽区间再看看。`
              : "此区间内没有落库的事件。换标的或放宽区间再看看。"}
        </p>
      ) : (
        <>
          <TableShell
            className="max-h-[420px] overflow-y-auto"
            head={["事发", "标题", "方向", "评分", "首次可用", "来源", "详情"]}
          >
            {events.map((event) => (
              // 键不能只用 event_id：平台对「按月复发的同题事件」复用 id（实测 84 天里 18 例），
              // 同一窗口内出现两次时 React 会丢行，故带上首次可用时刻
              <Row key={`${event.event_id}@${event.available_at ?? ""}`}>
                <Cell numeric>{eventStamp(event.event_time)}</Cell>
                <Cell className="max-w-[420px] truncate">
                  <span title={event.title}>{event.title}</span>
                </Cell>
                <Cell className={DIRECTION[event.direction_norm ?? ""]?.className ?? "text-ink-3"}>
                  {DIRECTION[event.direction_norm ?? ""]?.label ?? "—"}
                </Cell>
                <Cell numeric>{num(event.score, { digits: 1 })}</Cell>
                <Cell numeric>{eventStamp(event.available_at)}</Cell>
                <SourceCell event={event} />
                <Cell>
                  {/* 带 `day`：同题事件跨日复用 id，不带的话详情会取到最新那条 */}
                  <button
                    type="button"
                    data-detail-open={event.event_id}
                    onClick={() =>
                      setTarget({
                        eventId: event.event_id,
                        day: (event.event_time ?? "").slice(0, 10) || null,
                      })
                    }
                    className="rounded-[var(--radius)] px-1.5 text-ink-3 hover:bg-muted hover:text-foreground"
                  >
                    详情
                  </button>
                </Cell>
              </Row>
            ))}
          </TableShell>

          <p className="mt-2 text-xs text-ink-3">
            可见性以「首次可用是否早于当日 15:00」判定（PIT 模式）；非 PIT 改用事发时间，
            等于允许使用尚未公开的信息。
          </p>
        </>
      )}
      <EventDetailSheet target={target} onClose={() => setTarget(null)} />
    </Section>
  );
}

/**
 * 行尾来源单元格：合着看 `original_source`，展开看 `source` 与 `content_hash`。
 *
 * **导出给 M10c 的个股页复用**——那里的事件时间线与这里展示的是同一件事，
 * 展开的形状（`▸` 转角、三元组顺序）不该各写一份。
 */
export function SourceCell({ event }: { event: MarketEvent }) {
  return (
    <td className="px-2 align-middle">
      <details className="group">
        <summary className="flex cursor-pointer list-none items-center gap-1 whitespace-nowrap text-ink-2 [&::-webkit-details-marker]:hidden">
          <span className="text-ink-3 transition-transform group-open:rotate-90">▸</span>
          {event.original_source ?? "—"}
        </summary>
        <dl className="mt-1 space-y-0.5 text-xs whitespace-nowrap text-ink-3">
          <div>
            <dt className="inline">source　</dt>
            <dd className="num inline">{event.source ?? "—"}</dd>
          </div>
          <div>
            <dt className="inline">hash　　</dt>
            <dd className="num inline">{shortHash(event.content_hash)}</dd>
          </div>
          {event.quality_status && (
            <div>
              <dt className="inline">质量　　</dt>
              <dd className="inline">{event.quality_status}</dd>
            </div>
          )}
        </dl>
      </details>
    </td>
  );
}
