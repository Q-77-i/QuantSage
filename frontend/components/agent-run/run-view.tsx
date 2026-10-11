"use client";

import type { ReactNode } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { cn } from "@/lib/utils";
import {
  budgetMeters,
  buildStages,
  buildWaterfall,
  statusLabel,
  statusTone,
  waterfallSpan,
  type RunBudget,
  type RunNodeEvent,
  type RunStatus,
  type RunSubject,
  type StageState,
} from "@/lib/agent-run";

/**
 * 运行视图（展示层，纯 props）：阶段带 + **并行瀑布** + 预算计量条。
 *
 * 三条设计口径（见 `P2-M8c-design-brief.md`）：
 *   * 阶段带是**横向流水线**而不是圆环——这件事有方向、每段都有状态，环反而信息量更低；
 *   * 瀑布条**一律同色**（`series1`）：角色身份已由行标签承载，再上五个色相是冗余；
 *   * **不借 `--up/--down`**（涨跌 / 买卖专用色）：状态走 brand / warn / destructive。
 */

const STAGE_DOT: Record<StageState, string> = {
  pending: "border-border bg-transparent text-ink-3",
  running: "border-brand bg-brand/15 text-brand animate-pulse",
  done: "border-brand bg-brand text-white",
  degraded: "border-warn/60 bg-warn/10 text-warn",
  blocked: "border-warn/60 bg-transparent text-warn",
};

const STAGE_NOTE: Record<StageState, string> = {
  pending: "未开始",
  running: "进行中",
  done: "",
  degraded: "降级",
  blocked: "待审批",
};

export function StageStrip({ stages }: { stages: ReturnType<typeof buildStages> }) {
  return (
    <ol className="flex flex-wrap items-stretch gap-1.5" aria-label="执行阶段">
      {stages.map((stage) => (
        <li
          key={stage.node}
          data-stage={stage.node}
          data-state={stage.state}
          className="flex min-w-[6.5rem] flex-1 flex-col gap-1 rounded-[var(--radius)] border border-border px-2 py-1.5"
        >
          <span className="flex items-center gap-1.5 text-xs">
            <span
              className={cn(
                "inline-block size-2 shrink-0 rounded-full border",
                STAGE_DOT[stage.state],
              )}
            />
            <span className="truncate text-ink-2">{stage.label}</span>
            {stage.runs > 1 && <span className="num text-ink-3">×{stage.runs}</span>}
          </span>
          <span className="num text-xs text-ink-3">
            {stage.seconds !== null ? `${stage.seconds.toFixed(1)}s` : STAGE_NOTE[stage.state]}
          </span>
        </li>
      ))}
    </ol>
  );
}

/**
 * 并行瀑布：一行一次节点执行，**条长 = 真实耗时**。
 *
 * 刻意用 DOM 而不是 ECharts：十几行条形、语义是时间轴、要带行标签，DOM 的可访问性、
 * 打印（M7d 的 PDF 导出）与窄屏表现都更好；图表库在这里只换来一层缩放开销。
 */
export function Waterfall({ events }: { events: RunNodeEvent[] }) {
  const rows = buildWaterfall(events);
  const span = waterfallSpan(rows);
  if (!rows.length) {
    return <p className="text-sm text-ink-3">还没有可画的节点。</p>;
  }
  return (
    <div className="flex flex-col gap-1" data-rows={rows.length}>
      {rows.map((row) => (
        <div key={`${row.node}-${row.index}`} className="flex items-center gap-2 text-xs">
          <span className="w-20 shrink-0 truncate text-ink-2" title={row.label}>
            {row.label}
            {row.index > 1 ? ` ${row.index}` : ""}
          </span>
          {/* 宿主常驻 + 条按百分比定位：窄屏也不换行（时间轴语义不能竖过来） */}
          <span className="relative h-3 flex-1 overflow-hidden rounded-[var(--radius)] bg-muted">
            <span
              data-bar={row.node}
              className={cn(
                "absolute inset-y-0 rounded-[var(--radius)]",
                row.status === "ok" ? "bg-[var(--chart-series1)]" : "bg-warn",
              )}
              style={{
                left: `${(row.startAt / span) * 100}%`,
                width: `${Math.max(0.6, (row.seconds / span) * 100)}%`,
              }}
            />
          </span>
          <span className="num w-12 shrink-0 text-right text-ink-3">{row.seconds.toFixed(1)}s</span>
        </div>
      ))}
      <p className="mt-1 text-xs text-ink-3">
        横轴 0 → {span.toFixed(1)}s。<strong className="font-medium">条长就是真实耗时</strong>
        ：取证五条并行跑，检索那条最长（本地精排是串行的，不把它画短）。
      </p>
    </div>
  );
}

export function BudgetMeters({ budget }: { budget: RunBudget }) {
  const meters = budgetMeters(budget);
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {meters.map((meter) => (
        <div key={meter.key} className="flex flex-col gap-1" data-meter={meter.key}>
          <span className="flex items-baseline justify-between text-xs">
            <span className="text-ink-2">{meter.label}</span>
            <span className={cn("num", meter.over ? "text-destructive" : "text-ink-3")}>
              {meter.text}
              {meter.over ? " · 已超限" : ""}
            </span>
          </span>
          <span className="h-1 w-full overflow-hidden rounded-full bg-muted">
            <span
              className={cn("block h-full rounded-full", meter.over ? "bg-destructive" : "bg-brand")}
              style={{ width: `${meter.ratio * 100}%` }}
            />
          </span>
        </div>
      ))}
    </div>
  );
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const tone = statusTone(status);
  return (
    <span
      data-run-status={status}
      className={cn(
        "inline-flex items-center rounded-[var(--radius)] border px-1.5 py-0.5 text-xs",
        tone === "brand" && "border-brand/50 bg-brand/10 text-brand",
        tone === "warn" && "border-warn/50 bg-warn/10 text-warn",
        tone === "danger" && "border-destructive/40 text-destructive",
        tone === "muted" && "border-border text-ink-2",
      )}
    >
      {statusLabel(status)}
    </span>
  );
}

export function RunView({
  question,
  subject,
  status,
  events,
  budget,
  gaps,
  aside,
}: {
  question: string;
  subject: RunSubject;
  status: RunStatus;
  events: RunNodeEvent[];
  budget: RunBudget;
  gaps: string[];
  aside?: ReactNode;
}) {
  const stages = buildStages(events, status);
  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="font-heading text-lg font-semibold">{question}</h1>
        <p className="text-xs text-ink-3">
          标的 <span className="num">{subject.symbols?.join("、") || "（未指定）"}</span> · 数据截止{" "}
          <span className="num">{subject.as_of?.slice(0, 10) || "—"}</span> ·{" "}
          <RunStatusBadge status={status} />
        </p>
      </header>

      {subject.notes?.length ? (
        <p className="text-xs text-ink-2">解析标注：{subject.notes.join("；")}</p>
      ) : null}

      <Section title="执行阶段">
        <StageStrip stages={stages} />
      </Section>

      <Section title="并行瀑布">
        <Waterfall events={events} />
      </Section>

      <Section title="预算">
        <BudgetMeters budget={budget} />
      </Section>

      {gaps.length ? (
        <Section title="如实标注：本次的缺口">
          <ul className="flex flex-col gap-1 text-xs text-warn">
            {gaps.map((gap) => (
              <li key={gap}>· {gap}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      {aside}
    </div>
  );
}

/** 事件时间线（默认收起）：需要深挖的人才展开。 */
export function EventTimeline({ events }: { events: RunNodeEvent[] }) {
  if (!events.length) return null;
  return (
    <details className="rounded-[var(--radius)] border border-border">
      <summary className="cursor-pointer list-none px-2.5 py-1.5 text-xs text-ink-2 hover:bg-muted [&::-webkit-details-marker]:hidden">
        事件时间线（{events.length} 条）
      </summary>
      <ol className="border-t border-border px-2.5 py-2 text-xs">
        {events.map((event, index) => (
          <li key={`${event.phase}-${event.node}-${index}`} className="num flex gap-2 py-0.5">
            <span className="w-12 shrink-0 text-ink-3">{(event.at ?? 0).toFixed(1)}s</span>
            <span className="w-24 shrink-0">{event.node ?? event.phase}</span>
            <span className="text-ink-3">
              {event.phase}
              {event.seconds != null ? ` · ${event.seconds.toFixed(2)}s` : ""}
              {event.status && event.status !== "ok" ? ` · ${event.status}` : ""}
            </span>
          </li>
        ))}
      </ol>
    </details>
  );
}
