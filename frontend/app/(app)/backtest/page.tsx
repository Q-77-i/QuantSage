"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

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
import { api } from "@/lib/api";
import {
  buildRequest,
  defaultForm,
  formFromRequest,
  hasErrors,
  validateForm,
} from "@/lib/backtest-form";
import type { FormState } from "@/lib/backtest-form";
import type { ChartHandle } from "@/lib/chart-handle";
import type { BacktestRunDetail, StoredBacktestRequest } from "@/lib/types";

/**
 * 回测页（T6c）。
 *
 * 分区按叙事顺序堆叠，不做页签：默认表单已经选了「双模式对比」，把最能讲故事的
 * PIT 对比藏在页签后面与这个默认自相矛盾。表单状态在 `lib/backtest-form.ts`（纯函数、
 * 有单测），取数在 `components/backtest/use-backtest.ts`，这里只负责排版与编排。
 *
 * 图表缩放（T6d）：两图**各自独立**，故各存一份缩放态，各自在标题右侧露出「重置缩放」。
 * 图表内部只在布尔翻转时回调，所以这里的 setState 不会随拖动逐帧触发。
 *
 * 重开（M1c）：地址栏带 `?run=<id>` 时载入那次存下来的报告并把表单回填成当时的配置；
 * 跑完一次也把地址换成 `?run=`，刷新或把链接发给别人看到的是同一份报告。
 *
 * 用户策略（M4c）：本页**只跑内置策略**，但可能重开到用户策略那次运行——报告照常渲染、
 * 表单只回填与策略无关的几项，另出一条提示条（策略名 / 代码是否已改 / 去工作台重跑）。
 */
export default function BacktestPage() {
  const [form, setForm] = useState<FormState>(defaultForm);
  const { loading, report, bars, events, error, runId, run, loadRun } = useBacktest();
  const errors = validateForm(form);
  const router = useRouter();

  const equityRef = useRef<ChartHandle>(null);
  const klineRef = useRef<ChartHandle>(null);
  const [zoomed, setZoomed] = useState({ equity: false, kline: false });
  /** 重开的是用户策略那次时的提示条（M4c）；内置策略为 null */
  const [userNotice, setUserNotice] = useState<UserRunNotice | null>(null);

  // 地址栏跟着当前这份报告走。`replace` 而不是 `push`：连跑几次不该在历史里堆一串
  useEffect(() => {
    if (runId) router.replace(`/backtest?run=${runId}`, { scroll: false });
  }, [runId, router]);

  function handleRun() {
    if (hasErrors(errors)) return;
    setUserNotice(null); // 新跑的是内置策略，上一次的用户策略提示到此为止
    run(buildRequest(form));
  }

  /**
   * 重开一条历史回测：回填表单；若是**用户策略**那次，另出一条提示条（M4c）。
   *
   * 回测页只跑内置策略（SPEC §5 M4c 已拍板），所以用户策略的记录不回填策略位——
   * 提示条给出三件事就够了：当次用的哪条策略、代码是否已改（两个 hash 比对）、去哪重跑。
   */
  function handleLoaded(request: StoredBacktestRequest, detail: BacktestRunDetail) {
    setForm(formFromRequest(request, form));
    if (request.strategy !== "user") {
      setUserNotice(null);
      return;
    }
    setUserNotice({ name: detail.report.meta.strategy_name, codeChanged: null, gone: false });
    if (!detail.strategy_id) return;
    api
      .strategy(detail.strategy_id)
      .then((strategy) =>
        setUserNotice((prev) =>
          prev ? { ...prev, codeChanged: strategy.code_sha256 !== detail.code_sha256 } : prev,
        ),
      )
      // 404 = 策略已被删除：不是错误，如实说「已删除」即可
      .catch(() => setUserNotice((prev) => (prev ? { ...prev, gone: true } : prev)));
  }

  return (
    <>
      <Suspense fallback={null}>
        <RunDeepLink loadedId={runId} onLoad={loadRun} onRequest={handleLoaded} />
      </Suspense>

      <main className="mx-auto max-w-[1400px] px-4 py-6">
        <h1 className="font-heading text-xl font-semibold">回测</h1>

        {userNotice ? <UserRunBanner notice={userNotice} /> : null}

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

            <EventsTable events={events} coverage={report.meta.event_coverage} />
          </div>
        )}
      </main>
    </>
  );
}

/**
 * `?run=<id>`：从「我的回测」点进来时载入那次报告，并把表单回填成当时的配置。
 *
 * 用 `loadedId` 而不是「只认挂载那一次」来去重：跑完一次页面会把地址换成新的
 * `?run=`，若按「挂载时消费」的写法，那次改写又会被当成一次新的深链再拉一遍。
 * 比对当前已载入的 id，则自产自销的 URL 变更天然被跳过。
 */
function RunDeepLink({
  loadedId,
  onLoad,
  onRequest,
}: {
  loadedId: string | null;
  onLoad: (id: string) => Promise<BacktestRunDetail | null>;
  onRequest: (request: StoredBacktestRequest, detail: BacktestRunDetail) => void;
}) {
  const params = useSearchParams();
  const target = params.get("run");

  useEffect(() => {
    if (!target || target === loadedId) return;
    void onLoad(target).then((detail) => {
      if (detail) onRequest(detail.request, detail);
    });
  }, [target, loadedId, onLoad, onRequest]);

  return null;
}

/** 重开一条用户策略回测时的提示（M4c）；`codeChanged` 为 null = 还在比对 */
interface UserRunNotice {
  name: string | null;
  codeChanged: boolean | null;
  gone: boolean;
}

/**
 * 用户策略记录的提示条。
 *
 * 三句话各有出处：**当次用的策略名**来自报告 meta；**代码是否已改**靠当次运行的
 * `code_sha256` 与策略**当前**的 hash 比对得出（服务端只给事实，判断在这边）；
 * 「去工作台重跑」是因为本页只跑内置策略——用户策略的入口只有工作台一个。
 */
function UserRunBanner({ notice }: { notice: UserRunNotice }) {
  const codeState = notice.gone
    ? "策略已删除"
    : notice.codeChanged === null
      ? "正在核对代码"
      : notice.codeChanged
        ? "已非当次运行的代码"
        : "代码与当次一致";
  return (
    <p className="mt-3 flex flex-wrap items-center gap-x-2 rounded-[var(--radius)] border border-border bg-surface px-3 py-2 text-sm text-ink-2">
      <span>
        当次运行使用用户策略
        <span className="mx-1 font-medium text-ink-1">{notice.name ?? "（未命名）"}</span>
        · {codeState}
      </span>
      <Link href="/strategies" className="text-brand hover:underline">
        去工作台重跑
      </Link>
    </p>
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
