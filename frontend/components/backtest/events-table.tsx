"use client";

import { Section } from "@/components/backtest/chart-frame";
import { Cell, Row, TableShell } from "@/components/backtest/table";
import { count, eventStamp, num, shortHash } from "@/lib/format";
import type { MarketEvent } from "@/lib/types";

/** 方向标签。`direction` 原值中英混用，后端已归一到 `direction_norm`（T2）。 */
const DIRECTION: Record<string, { label: string; className: string }> = {
  bullish: { label: "利多", className: "text-up" },
  bearish: { label: "利空", className: "text-down" },
  neutral: { label: "中性", className: "text-ink-3" },
};

/**
 * 事件语料表：回测窗口内该标的的全部事件。
 *
 * `event_time` 与 `available_at` **并列成列**——两者之差就是本项目的护城河，比任何
 * 文字解释都直观。来源三元组是 PRD §5 的硬性要求：行尾显示 `original_source`，
 * 展开见 `source` 与 `content_hash`（后者截前 12 位，全串对人不产生信息）。
 */
export function EventsTable({ events }: { events: MarketEvent[] }) {
  return (
    <Section
      title="事件语料"
      hint={
        events.length
          ? `回测窗口内 ${count(events.length)} 条`
          : "回测窗口内该标的没有事件"
      }
    >
      {events.length === 0 ? (
        <p className="mt-3 text-sm text-ink-2">
          事件窗口约 3 个月，此区间内没有落库的事件。换标的或放宽区间再看看。
        </p>
      ) : (
        <>
          <TableShell
            className="max-h-[420px] overflow-y-auto"
            head={["事发", "标题", "方向", "评分", "首次可用", "来源"]}
          >
            {events.map((event) => (
              <Row key={event.event_id}>
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
              </Row>
            ))}
          </TableShell>

          <p className="mt-2 text-xs text-ink-3">
            可见性以「首次可用是否早于当日 15:00」判定（PIT 模式）；非 PIT 改用事发时间，
            等于允许使用尚未公开的信息。
          </p>
        </>
      )}
    </Section>
  );
}

/** 行尾来源单元格：合着看 `original_source`，展开看 `source` 与 `content_hash`。 */
function SourceCell({ event }: { event: MarketEvent }) {
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
