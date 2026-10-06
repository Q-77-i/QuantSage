"use client";

import { useRef, useState } from "react";

import { BacktestForm } from "@/components/backtest/backtest-form";
import { CandlestickChart } from "@/components/backtest/candlestick-chart";
import { ChartFrame, Section } from "@/components/backtest/chart-frame";
import { EquityChart } from "@/components/backtest/equity-chart";
import { EventsTable } from "@/components/backtest/events-table";
import { MetricsSummary } from "@/components/backtest/metrics-summary";
import { PitComparisonSection } from "@/components/backtest/pit-comparison";
import { TradesTable } from "@/components/backtest/trades-table";
import { useBacktest } from "@/components/backtest/use-backtest";
import { Button } from "@/components/ui/button";
import { buildRequest, defaultForm, hasErrors, validateForm } from "@/lib/backtest-form";
import type { FormState } from "@/lib/backtest-form";
import type { ChartHandle } from "@/lib/chart-handle";

/**
 * 回测页（T6c）。
 *
 * 分区按叙事顺序堆叠，不做页签：默认表单已经选了「双模式对比」，把最能讲故事的
 * PIT 对比藏在页签后面与这个默认自相矛盾。表单状态在 `lib/backtest-form.ts`（纯函数、
 * 有单测），取数在 `components/backtest/use-backtest.ts`，这里只负责排版与编排。
 *
 * 图表缩放（T6d）：两图**各自独立**，故各存一份缩放态，各自在标题右侧露出「重置缩放」。
 * 图表内部只在布尔翻转时回调，所以这里的 setState 不会随拖动逐帧触发。
 */
export default function BacktestPage() {
  const [form, setForm] = useState<FormState>(defaultForm);
  const { loading, report, bars, events, error, run } = useBacktest();
  const errors = validateForm(form);

  const equityRef = useRef<ChartHandle>(null);
  const klineRef = useRef<ChartHandle>(null);
  const [zoomed, setZoomed] = useState({ equity: false, kline: false });

  function handleRun() {
    if (hasErrors(errors)) return;
    run(buildRequest(form));
  }

  return (
    <>
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

            <ChartFrame
              title="净值曲线"
              hint={
                zoomed.equity ? (
                  <ResetZoom onClick={() => equityRef.current?.resetZoom()} />
                ) : null
              }
            >
              <EquityChart
                ref={equityRef}
                points={report.equity_curve}
                onZoomChange={(next) =>
                  setZoomed((prev) => (prev.equity === next ? prev : { ...prev, equity: next }))
                }
              />
            </ChartFrame>

            <ChartFrame
              title="K 线"
              empty={bars.length ? null : "该区间没有行情数据。"}
              hint={
                zoomed.kline ? (
                  <ResetZoom onClick={() => klineRef.current?.resetZoom()} />
                ) : null
              }
            >
              <CandlestickChart
                ref={klineRef}
                bars={bars}
                trades={report.trades}
                openPosition={report.open_position}
                onZoomChange={(next) =>
                  setZoomed((prev) => (prev.kline === next ? prev : { ...prev, kline: next }))
                }
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

/** 「重置缩放」只在对应图表已缩放时出现，所以不做成常驻控件。 */
function ResetZoom({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={onClick}>
      重置缩放
    </Button>
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
