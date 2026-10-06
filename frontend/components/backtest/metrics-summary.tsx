"use client";

import { symbolName, strategyLabel } from "@/lib/backtest-form";
import { amount, count, num, pct } from "@/lib/format";
import type { BacktestReport, Metrics } from "@/lib/types";

/**
 * 指标区：标签 + 大号数字的裸布局，不套卡片盒。
 *
 * **带号与否按指标语义写死**，不看数值正负——最大回撤后端返回的是正值幅度，
 * 统一「正数加 +」会把它显示成 `+6.7%`。收益与超额带号，回撤与胜率不带。
 */
export function MetricsSummary({
  report,
  running,
}: {
  report: BacktestReport;
  running: boolean;
}) {
  const { meta, metrics } = report;
  const cells = metricCells(metrics);

  return (
    <section className="border-t border-border pt-4">
      <dl className="grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-4 lg:grid-cols-7">
        {cells.map((cell) => (
          <div key={cell.label}>
            <dt className="text-xs text-ink-3">{cell.label}</dt>
            <dd className="num mt-0.5 text-lg">{cell.value}</dd>
          </div>
        ))}
      </dl>

      {/* 样本量提示常驻：不可折叠、不塞 tooltip。loading 与无提示时同样占位，
          免得数据到达才把下面的图表顶下去一截 */}
      <p className="mt-3 min-h-[1.25rem] text-xs text-destructive">
        {meta.warnings.join("；")}
      </p>

      <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-ink-2">
        <span>{symbolName(meta.symbol)}</span>
        <Sep />
        <span>{strategyLabel(meta.strategy)}</span>
        <Sep />
        <span>{meta.mode === "pit" ? "PIT" : "非 PIT"}</span>
        <Sep />
        <span className="num">
          {meta.start} → {meta.end}
        </span>
        <Sep />
        <span className="num">{count(meta.bars)} 根 bar</span>
        <Sep />
        {/* `cutoff_field` 描述的是事件游标用的字段，与策略读不读事件无关——
            双均线下它照样返回 available_at，照抄会让人以为这次回测过滤过事件 */}
        {meta.strategy === "event_driven" ? (
          <span>
            可见性判据 <span className="num">{meta.cutoff_field}</span>
          </span>
        ) : (
          <span>不读事件语料</span>
        )}
        <Sep />
        <span>{meta.costs}</span>
        {Object.entries(meta.params).length > 0 && (
          <>
            <Sep />
            <span className="num">
              {Object.entries(meta.params)
                .map(([key, value]) => `${key}=${value}`)
                .join(" · ")}
            </span>
          </>
        )}
        {running && <span className="text-ink-3">运行中…</span>}
      </p>
    </section>
  );
}

function Sep() {
  return <span className="text-ink-3">·</span>;
}

function metricCells(metrics: Metrics): { label: string; value: string }[] {
  return [
    { label: "总收益", value: pct(metrics.total_return, { signed: true }) },
    { label: "年化", value: pct(metrics.annual_return, { signed: true }) },
    { label: "最大回撤", value: pct(metrics.max_drawdown) },
    { label: "夏普", value: num(metrics.sharpe, { signed: true }) },
    { label: "胜率", value: pct(metrics.win_rate) },
    { label: "交易次数", value: count(metrics.trade_count) },
    { label: "期末权益", value: amount(metrics.final_equity) },
  ];
}
