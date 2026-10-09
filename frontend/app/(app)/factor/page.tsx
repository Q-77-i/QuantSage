"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { FactorForm, queryInputOf } from "@/components/factor/factor-form";
import type { FactorFormState } from "@/components/factor/factor-form";
import { GroupPanel } from "@/components/factor/group-panel";
import { ICChart } from "@/components/factor/ic-chart";
import { MetricCards } from "@/components/factor/metric-cards";
import { NotesList } from "@/components/factor/notes-list";
import { SpreadPanel } from "@/components/factor/spread-panel";
import { useFactor } from "@/components/factor/use-factor";
import { factorHeadline } from "@/lib/factor-report";

export default function FactorPage() {
  // `useSearchParams` 在生产构建下要求 Suspense 边界（同 optimize / space 页）
  return (
    <Suspense fallback={<Bootstrap />}>
      <FactorView />
    </Suspense>
  );
}

function FactorView() {
  const params = useSearchParams();
  const router = useRouter();
  const source: FactorFormState["source"] = params.get("source") === "price" ? "price" : "event";
  const direction: FactorFormState["direction"] =
    params.get("direction") === "momentum" ? "momentum" : "reversal";

  const [form, setForm] = useState<FactorFormState>({
    source,
    direction,
    start: "",
    end: "",
    costs: true,
  });
  const { report, loading, error, run } = useFactor();

  const runWith = useCallback(
    (value: FactorFormState) => void run(queryInputOf(value)),
    [run],
  );

  // 进页面就出报告（默认口径 0.6–0.9s，同步端点），换因子源也自动重取——
  // 深链 `?source=price` 因此可直接分享。窗口与费用改动仍要点「运行」。
  useEffect(() => {
    setForm((current) => ({ ...current, source, direction }));
    void run({
      source,
      direction: source === "price" ? direction : undefined,
      costs: true,
    });
  }, [source, direction, run]);

  const changeSource = useCallback(
    (next: FactorFormState["source"]) => {
      // 因子源进地址栏（与 /optimize 的 `?mode=` 同款）：刷新与分享都带得住
      router.replace(next === "price" ? "/factor?source=price" : "/factor", { scroll: false });
    },
    [router],
  );

  const head = useMemo(() => (report ? factorHeadline(report) : null), [report]);

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <h1 className="font-heading text-xl font-semibold">因子分析</h1>
      <p className="mt-1 text-sm text-ink-3">
        把「新闻信号 / 价格动量有没有截面预测力」跑成一份可复算的报告：RankIC、分层、多空价差。
        <strong className="font-medium text-ink-2">这是一次检验，不是战绩</strong>
        ——读数落在噪声区间时会如实标出来。
      </p>

      <FactorForm
        value={form}
        onChange={setForm}
        onRun={() => runWith(form)}
        running={loading}
        onSourceChange={changeSource}
      />

      {error ? (
        <p role="alert" className="mt-4 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      {report && head ? (
        <div className="mt-6 flex flex-col gap-6">
          <div>
            <p className="text-xs text-ink-3">
              窗口 {report.window.start} → {report.window.end}（{report.window.signal_days} 个信号日，
              行情末端 {report.window.bars_end}）
            </p>
            <MetricCards head={head} params={report.params} />
          </div>

          <Section
            title="逐日 RankIC"
            hint="红 = 正、蓝 = 负；零线是「无预测力」的参照"
          >
            <ICChart report={report} />
          </Section>

          <Section
            title="分层净值（五等分）"
            hint={`按因子值分 5 组、组内等权，次日开盘建仓、再次日开盘平仓（${report.params.horizon === "open_t1_to_open_t2" ? "t+1 → t+2" : report.params.horizon}）`}
          >
            <GroupPanel report={report} />
          </Section>

          <Section title="多空价差（Q5 − Q1）" hint="与分层同口径；单列一张图，不与组合曲线共轴">
            <SpreadPanel report={report} />
          </Section>

          <Section title="如实标注" hint="服务端随报告一起给的口径与边界，逐条照登">
            <NotesList notes={report.notes} />
          </Section>
        </div>
      ) : loading ? (
        <Bootstrap />
      ) : (
        <p className="mt-8 rounded-[var(--radius)] border border-border px-4 py-10 text-center text-sm text-ink-2">
          选好口径后点「运行」。
        </p>
      )}
    </main>
  );
}

function Bootstrap() {
  return <div className="mt-8 h-40 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />;
}
