"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";

import { Section } from "@/components/backtest/chart-frame";
import { BatchTable } from "@/components/optimize/batch-table";
import { DsrCard, paramText } from "@/components/optimize/dsr-card";
import { GridHeatmap, GridLine, hasAnySharpe } from "@/components/optimize/grid-heatmap";
import { OptimizeForm } from "@/components/optimize/optimize-form";
import { RunsList } from "@/components/optimize/runs-list";
import { SharpeDistribution } from "@/components/optimize/sharpe-distribution";
import { batchPicks, useDescriptors, useUserStrategies } from "@/components/optimize/use-catalog";
import { useOptimize } from "@/components/optimize/use-optimize";
import { api, describeError } from "@/lib/api";
import { builtinDescriptors, parseAxisValues, parseSymbolList } from "@/lib/optimize-form";
import type { BatchColumn } from "@/lib/optimize-matrix";
import {
  batchFormFromRequest,
  buildBatchRequest,
  buildGridRequest,
  gridFormFromRequest,
  validateBatch,
  validateGrid,
} from "@/lib/optimize-form";
import type { BatchFormState, GridFormState } from "@/lib/optimize-form";
import type { BacktestRequest, OptimizeAxis, OptimizeCell, OptimizeSummary } from "@/lib/types";

/**
 * 网格与批量（M5b）。
 *
 * 页面结构按 dataviz 的两条硬规矩组织：
 *   · **一行 filters 管住下面所有的图**——表单在最上，没有「图自带的过滤器」；
 *   · 结果区按「一个结论 → 它的证据」排：DSR 卡（那个数）→ 分布（它在哪）→
 *     热力图（往哪调）→ 没跑成的格（哪些缺）。
 *
 * 两条状态进地址栏：`?mode=grid|batch` 与 `?run=<id>`（重开）。去重用「最近一次已同步的号」
 * 而不是「当前载入的 id」——页面自己会把地址换成新的 `?run=`，按后者去重会让那次改写
 * 又被当成一次新深链（M4c 踩过）。
 *
 * **轴与标的一律从表单派生**（重开时表单已被回填成当时的样子），不另存一份：
 * 两处各存一份的话，热力图的行列与表格的读数迟早对不上。
 */
export default function OptimizePage() {
  return (
    <Suspense fallback={<Bootstrap />}>
      <OptimizeView />
    </Suspense>
  );
}

function OptimizeView() {
  const params = useSearchParams();
  const router = useRouter();
  const mode = params.get("mode") === "batch" ? "batch" : "grid";

  const [grid, setGrid] = useState<GridFormState>(defaultGrid);
  const [batch, setBatch] = useState<BatchFormState>(defaultBatch);
  const { items: userStrategies } = useUserStrategies();
  const descriptors = useDescriptors(grid.strategy, grid.strategyId);
  const picks = useMemo(() => batchPicks(userStrategies), [userStrategies]);

  const { running, started, cells, done, error, runId, run, stop, loadRun } = useOptimize();
  const [picking, setPicking] = useState<number | null>(null);
  const [pickError, setPickError] = useState<string | null>(null);
  const [reopened, setReopened] = useState<OptimizeSummary | null>(null);
  const [syncedRunId, setSyncedRunId] = useState<string | null>(null);

  const gridErrors = descriptors.loading ? {} : validateGrid(grid, descriptors.descriptors).errors;
  const batchErrors = validateBatch(batch, picks).errors;

  // 轴从表单派生（重开时表单已回填）——**不另存一份**
  const axes: OptimizeAxis[] = useMemo(
    () =>
      grid.axes
        .map((axis) => ({ param: axis.param, values: parseAxisValues(axis.values).values }))
        .filter((axis) => axis.param !== "" && axis.values.length >= 2),
    [grid.axes],
  );
  const batchColumns: BatchColumn[] = useMemo(
    () =>
      picks
        .filter((pick) => batch.picks.includes(pick.key))
        .map((pick) => ({ key: pick.key, label: pick.label })),
    [picks, batch.picks],
  );
  const batchSymbols = useMemo(() => parseSymbolList(batch.symbols).symbols, [batch.symbols]);

  useEffect(() => {
    if (runId && runId !== syncedRunId) {
      setSyncedRunId(runId);
      router.replace(`/optimize?mode=${mode}&run=${runId}`, { scroll: false });
    }
  }, [runId, syncedRunId, mode, router]);

  // 跑着的时候离开会丢掉整次（后端「断线即断」，SPEC §6 M5b）——拦一道
  useEffect(() => {
    if (!running) return;
    const guard = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [running]);

  // 重开：与「最近一次已同步的号」比对去重（自产自销的地址改写天然被跳过）
  const requested = params.get("run");
  useEffect(() => {
    if (!requested || requested === syncedRunId) return;
    void loadRun(requested).then((detail) => {
      if (!detail) return;
      setReopened(detail.summary);
      setSyncedRunId(detail.id);
      if (detail.summary.kind === "grid") setGrid(gridFormFromRequest(detail.request));
      else setBatch(batchFormFromRequest(detail.request));
    });
  }, [requested, syncedRunId, loadRun]);

  function submit() {
    setReopened(null);
    setPickError(null);
    if (mode === "grid") run("grid", buildGridRequest(grid));
    else run("batch", buildBatchRequest(batch, picks));
  }

  /**
   * 点一格重跑：走**既有的**单次回测端点，落一条正常回测记录后跳回测页。
   *
   * 不复用网格里的那份报告，是因为 `summary` 里**只有指标、没有净值曲线与逐笔**
   * （SPEC §6 M5b 定的：一百格 × 上千点净值曲线会把汇总撑成 MB 级）。单格 70–500ms，无感。
   */
  const pick = useCallback(
    (cellIndex: number) => {
      const cell = (reopened?.cells ?? cells)[cellIndex];
      if (!cell || !cell.ok) return;
      setPicking(cellIndex);
      setPickError(null);
      void (async () => {
        try {
          const { run_id } = await api.backtest(backtestBodyOf(cell));
          router.push(`/backtest?run=${run_id}`);
        } catch (cause) {
          setPickError(describeError(cause, "重跑这一格失败：后端未启动或网络不通。"));
        } finally {
          setPicking(null);
        }
      })();
    },
    [cells, reopened, router],
  );

  const live = useMemo(
    () => (started ? liveSummary(started, cells, done) : null),
    [started, cells, done],
  );
  const summary = reopened ?? live;
  // 运行中数组带空洞（逐格填充）——遍历前一律先 `filter(Boolean)`，解引用会整页白屏
  const arrivedCells = (summary?.cells ?? []).filter(Boolean);
  const arrived = arrivedCells.length;
  const failed = arrivedCells.filter((cell) => !cell.ok);
  const showResults = arrivedCells.length > 0;

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h1 className="font-heading text-xl font-semibold">网格与批量</h1>
        <p className="text-xs text-ink-3">
          进度逐格推送；断线即断、不续传，刷新等于重跑
        </p>
      </div>

      <nav className="mt-4 flex gap-1 border-b border-border">
        {(
          [
            { key: "grid", label: "参数网格" },
            { key: "batch", label: "批量回测" },
          ] as const
        ).map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => router.replace(`/optimize?mode=${tab.key}`, { scroll: false })}
            aria-current={mode === tab.key ? "page" : undefined}
            className={`-mb-px rounded-t-[var(--radius)] border-b-2 px-3 py-1.5 text-sm ${
              mode === tab.key
                ? "border-brand font-medium text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <div className="mt-4">
        <OptimizeForm
          mode={mode}
          grid={grid}
          batch={batch}
          picks={picks}
          userStrategies={userStrategies}
          descriptors={descriptors.descriptors}
          loading={descriptors.loading}
          errors={mode === "grid" ? gridErrors : batchErrors}
          running={running}
          onGridChange={setGrid}
          onBatchChange={setBatch}
          onSubmit={submit}
          onStop={stop}
        />
      </div>

      {descriptors.error ? <p className="mt-3 text-xs text-destructive">{descriptors.error}</p> : null}
      {error ? (
        <p
          role="alert"
          className="mt-4 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </p>
      ) : null}
      {pickError ? <p className="mt-3 text-xs text-destructive">{pickError}</p> : null}

      {running && started ? <Progress arrived={arrived} total={started.total} /> : null}

      {showResults && summary ? (
        <div className="mt-6 space-y-6">
          <DsrCard summary={summary} running={running && done === null} />

          {hasAnySharpe(summary) ? (
            summary.kind === "grid" ? (
              <>
                <Section title="全网格夏普分布" hint="红点是被选中的那一格">
                  <SharpeDistribution summary={summary} />
                </Section>
                <Section
                  title="参数网格"
                  hint={`${summary.cells_ok} / ${summary.cells_total} 格有结果`}
                >
                  {axes.length === 2 ? (
                    <GridHeatmap summary={summary} axes={axes} onPick={pick} picking={picking} />
                  ) : axes.length === 1 ? (
                    <GridLine summary={summary} axes={axes} />
                  ) : null}
                </Section>
              </>
            ) : (
              <Section title="批量结果" hint={`${summary.cells_ok} / ${summary.cells_total} 格有结果`}>
                <BatchTable
                  summary={summary}
                  symbols={batchSymbols}
                  columns={batchColumns}
                  onPick={pick}
                  picking={picking}
                />
              </Section>
            )
          ) : (
            <Section title="结果">
              <p className="mt-2 text-sm text-ink-2">本次没有任何一格跑出夏普值，见下面的失败清单。</p>
            </Section>
          )}

          {failed.length > 0 ? (
            <Section title="没跑成的格" hint={`${failed.length} 格`}>
              <ul className="mt-2 space-y-1 text-sm">
                {failed.map((cell) => (
                  <li key={cell.index} className="flex flex-wrap items-baseline gap-x-2 text-ink-2">
                    <span className="num text-xs text-ink-3">
                      {cell.symbol} {paramText(cell.params)}
                    </span>
                    <span className="num rounded-[var(--radius)] bg-muted px-1.5 py-0.5 text-xs text-ink-2">
                      {cell.error?.kind}
                    </span>
                    <span>{cell.error?.message}</span>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}
        </div>
      ) : !running ? (
        <Notice />
      ) : null}

      <Section title="最近的优化" hint="点一条重开（不重跑）">
        <RunsList currentId={runId} limit={8} emptyText="还没有跑过网格或批量。" />
      </Section>
    </main>
  );
}

function Progress({ arrived, total }: { arrived: number; total: number }) {
  const percent = total > 0 ? (arrived / total) * 100 : 0;
  return (
    <div className="mt-4">
      <p className="text-xs text-ink-3">
        已完成 {arrived} / {total} 格
      </p>
      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className="h-full bg-brand transition-[width] duration-200" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function Notice() {
  return (
    <div className="mt-6 flex h-[160px] items-center justify-center rounded-[var(--radius)] border border-border">
      <p className="text-sm text-ink-2">选好策略与参数轴，点「运行」。</p>
    </div>
  );
}

function Bootstrap() {
  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <div className="h-6 w-32 animate-pulse rounded-[var(--radius)] bg-muted" />
    </main>
  );
}

// ── 纯函数 ─────────────────────────────────────────────────

function defaultGrid(): GridFormState {
  const descriptors = builtinDescriptors("ma_cross");
  return {
    strategy: "ma_cross",
    strategyId: null,
    symbol: "600519",
    start: "",
    end: "",
    baseParams: Object.fromEntries(
      descriptors.map((field) => [field.key, field.fallback === null ? "" : String(field.fallback)]),
    ),
    axes: [{ param: "fast", values: "3, 5, 8" }],
  };
}

function defaultBatch(): BatchFormState {
  return { symbols: "600519\n000001", picks: ["ma_cross"], start: "", end: "" };
}

/** 单格 → 回测请求体（**用该格实际跑的区间**，与热力图上那个数一一对应） */
function backtestBodyOf(cell: OptimizeCell): BacktestRequest {
  return {
    strategy: cell.strategy as BacktestRequest["strategy"],
    ...(cell.strategy_id ? { strategy_id: cell.strategy_id } : {}),
    symbol: cell.symbol,
    ...(cell.window?.start ? { start: cell.window.start } : {}),
    ...(cell.window?.end ? { end: cell.window.end } : {}),
    pit_mode: "pit",
    params: cell.params,
  };
}

/**
 * 运行中的那份 `summary`（重开时用存下来的那份，不走这里）。
 *
 * `cells` **原样传**，不 `filter` 掉尚未到达的槽位——`best_index` 是服务端给的整批下标，
 * 压缩数组会让它指到别的格上去。收尾帧到达时所有槽位都已填满（失败格也是一格）。
 */
function liveSummary(
  started: NonNullable<ReturnType<typeof useOptimize>["started"]>,
  cells: OptimizeCell[],
  done: ReturnType<typeof useOptimize>["done"],
): OptimizeSummary {
  return {
    kind: started.kind,
    cells,
    cells_total: started.total,
    cells_ok: cells.filter((cell) => cell?.ok).length,
    best_index: done?.best_index ?? null,
    // 收尾前 `overfit` 还没有：给一个「什么都没算」的块，卡上会显示「运行中…」而不是编个数
    overfit: done?.overfit ?? {
      dsr: null,
      reason: null,
      reason_text: null,
      note: "",
      n_trials: started.total,
      n_valid: 0,
      best_index: null,
      sr: null,
      sr0: null,
      sr_variance: null,
      skew: null,
      kurt: null,
      observations: null,
    },
    window: started.window?.start
      ? { start: started.window.start, end: started.window.end, bars: 0 }
      : null,
    costs: "",
    pit_mode: started.pit_mode,
    adjust: "qfq",
    duration_s: done?.duration_s ?? 0,
  };
}
