"use client";

import { useState } from "react";

import { AppHeader } from "@/components/app-header";
import { BacktestForm } from "@/components/backtest/backtest-form";
import { CandlestickChart } from "@/components/backtest/candlestick-chart";
import { ChartFrame, Section } from "@/components/backtest/chart-frame";
import { EquityChart } from "@/components/backtest/equity-chart";
import { EventsTable } from "@/components/backtest/events-table";
import { MetricsSummary } from "@/components/backtest/metrics-summary";
import { PitComparisonSection } from "@/components/backtest/pit-comparison";
import { TradesTable } from "@/components/backtest/trades-table";
import { useBacktest } from "@/components/backtest/use-backtest";
import { buildRequest, defaultForm, hasErrors, validateForm } from "@/lib/backtest-form";
import type { FormState } from "@/lib/backtest-form";

/**
 * 回测页（T6c）。
 *
 * 分区按叙事顺序堆叠，不做页签：默认表单已经选了「双模式对比」，把最能讲故事的
 * PIT 对比藏在页签后面与这个默认自相矛盾。表单状态在 `lib/backtest-form.ts`（纯函数、
 * 有单测），取数在 `components/backtest/use-backtest.ts`，这里只负责排版与编排。
 */
export default function BacktestPage() {
  const [form, setForm] = useState<FormState>(defaultForm);
  const { loading, report, bars, events, error, run } = useBacktest();
  const errors = validateForm(form);

  function handleRun() {
    if (hasErrors(errors)) return;
    run(buildRequest(form));
  }

  return (
    <>
      <AppHeader />
      <main className="mx-auto max-w-[1400px] px-4 py-6">
        <h1 className="font-heading text-xl">回测</h1>

        <div className="mt-4">
          <BacktestForm
            value={form}
            errors={errors}
            running={loading}
            onChange={setForm}
            onSubmit={handleRun}
          />
        </div>

        {error && (
          <p
            role="alert"
            className="mt-4 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {error}
          </p>
        )}

        {report === null ? (
          loading ? (
            <FirstRunSkeleton />
          ) : (
            <Notice />
          )
        ) : (
          // 重跑时保留上一次的结果，只把「运行中」标在指标区的回显行上——
          // 把整页清空再填回去，观感上像是页面被重置了
          <div className="mt-6 space-y-6">
            <MetricsSummary report={report} running={loading} />

            <ChartFrame title="净值曲线">
              <EquityChart points={report.equity_curve} />
            </ChartFrame>

            <ChartFrame
              title="K 线"
              empty={bars.length ? null : "该区间没有行情数据。"}
            >
              <CandlestickChart
                bars={bars}
                trades={report.trades}
                openPosition={report.open_position}
              />
            </ChartFrame>

            <PitComparisonSection report={report} />

            <Section title="交易明细" hint={`已平仓 ${report.trades.length} 笔`}>
              <TradesTable trades={report.trades} openPosition={report.open_position} />
            </Section>

            <EventsTable events={events} />
          </div>
        )}
      </main>
    </>
  );
}

/** 首次运行的骨架屏：与最终布局同形，不用居中转圈。 */
function FirstRunSkeleton() {
  return (
    <div className="mt-6 space-y-6">
      <div className="grid grid-cols-2 gap-x-8 gap-y-3 border-t border-border pt-4 sm:grid-cols-4 lg:grid-cols-7">
        {Array.from({ length: 7 }, (_, index) => (
          <div key={index}>
            <div className="h-3 w-10 animate-pulse rounded-[var(--radius)] bg-muted" />
            <div className="mt-1.5 h-5 w-20 animate-pulse rounded-[var(--radius)] bg-muted" />
          </div>
        ))}
      </div>
      {[0, 1].map((index) => (
        <div
          key={index}
          className="h-[300px] animate-pulse rounded-[var(--radius)] border border-border bg-muted/60 md:h-[340px]"
        />
      ))}
    </div>
  );
}

function Notice() {
  return (
    <div className="mt-6 flex h-[200px] items-center justify-center rounded-[var(--radius)] border border-border">
      <p className="text-sm text-ink-2">选好参数，点「运行」。</p>
    </div>
  );
}
