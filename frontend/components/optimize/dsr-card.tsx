"use client";

import { Section } from "@/components/backtest/chart-frame";
import { dsrPercent, overfitInputs } from "@/lib/optimize-matrix";
import type { OptimizeSummary } from "@/lib/types";

/** 常用的显著性线（5%）。画成刻度而不是配色分档——理由见下 */
const THRESHOLD = 0.95;

/**
 * Deflated Sharpe 卡：**一个数字 + 一根计量条**，不是图（dataviz：单个比率 → Meter）。
 *
 * 计量条**刻意不上颜色**：本页的两支语义色已被热力图的 diverging 色阶占满
 * （红 = 正夏普、蓝 = 负夏普），而全站的绿是「跌」——再借一个状态色只会被读错。
 * 意义的承载交给三样东西：数字本身、0.95 的刻度线、以及下面那句人话。
 *
 * 无定义时**不显示 0、也不显示「—」**：DSR 算不出来时 0 是一个具体且错误的断言。
 * 显示服务端给的原因文案（`reason_text`，与 `rejects` 的「码 + 文案同行」同姿态）。
 */
export function DsrCard({ summary, running }: { summary: OptimizeSummary | null; running: boolean }) {
  if (!summary) return null;
  const { overfit } = summary;
  const percent = dsrPercent(overfit.dsr);
  const inputs = overfitInputs(summary);
  const best = summary.best_index === null ? null : summary.cells[summary.best_index];

  return (
    <Section
      title="过拟合检验"
      hint={running ? "运行中…" : `试验 ${overfit.n_trials} 组 · 有效 ${overfit.n_valid} 组`}
    >
      {percent === null ? (
        <div className="mt-3 rounded-[var(--radius)] border border-border bg-muted/40 px-3 py-3">
          {running ? (
            // 跑完才知道——宁可空着，也不能拿「N 组」的进度去凑一个看起来像结论的数
            <p className="text-sm text-ink-2">跑完整个网格才有 DSR（它要用到全网格的夏普分布）。</p>
          ) : (
            <>
              <p className="text-sm text-ink-2">{overfit.reason_text ?? "DSR 不适用"}</p>
              <p className="mt-1 text-xs text-ink-3">原因以服务端给出的为准——本页不自己编一句。</p>
            </>
          )}
        </div>
      ) : (
        <div className="mt-3">
          <p className="text-xs text-ink-3">Deflated Sharpe（去偏后的夏普）</p>
          {/* 英雄数字：与全站同一个 sans，**不用 tabular-nums**（大字下等宽会显松） */}
          <p className="font-heading text-5xl leading-none font-semibold">
            {(percent / 100).toFixed(4)}
          </p>
          {/* 中文句子里的内联标记要**紧贴**前后字：JSX 会把换行折成一个空格，
              于是「不是运气 的概率」就多出一个空格（截图里看出来的） */}
          <p className="mt-1.5 text-sm text-ink-2">
            观测到的夏普在 {overfit.n_trials} 组参数里
            <span className="font-medium text-foreground">不是运气</span>的概率；常用显著线{" "}
            {THRESHOLD}
          </p>

          <div
            className="relative mt-3 h-2 w-full overflow-hidden rounded-full bg-muted"
            role="img"
            aria-label={`DSR ${percent.toFixed(1)}%，常用显著线 ${THRESHOLD * 100}%`}
          >
            {/* 填充走主文字色而不是某支语义色：本页的红/蓝已被热力图的 diverging 占满，
                绿又被全站的「跌」占着，再借一支只会被读错（理由详见组件 docstring） */}
            <div className="h-full bg-foreground" style={{ width: `${percent}%` }} />
            {/* 刻度线：0.95 是约定，不是数据的一部分，故画成一条细线而不是另一段填充 */}
            <span
              className="absolute top-0 h-full w-px bg-ink-3"
              style={{ left: `${THRESHOLD * 100}%` }}
            />
          </div>

          {best ? (
            <p className="mt-2 text-xs text-ink-3">
              被选中者：{paramText(best.params)}（年化夏普 {fmt(annual(best.metrics?.sharpe ?? null))}）
            </p>
          ) : null}
        </div>
      )}

      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-1.5 sm:grid-cols-4">
        {inputs.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-2 border-b border-border/60 pb-1">
            <dt className="text-xs text-ink-3">{row.label}</dt>
            <dd className="num text-sm">{row.value}</dd>
          </div>
        ))}
      </dl>

      {/* 口径自述：**照抄服务端**（M5a 的基准口径小字同一姿态）——前端不各写一份文案 */}
      <p className="mt-3 text-xs text-ink-3">{overfit.note}</p>
    </Section>
  );
}

function annual(perPeriod: number | null): number | null {
  return perPeriod === null ? null : perPeriod * Math.sqrt(252);
}

function fmt(value: number | null): string {
  return value === null ? "—" : value.toFixed(3);
}

/** 参数对象 → `fast=8, slow=15`。**键序固定**，免得两处的同一组参数长得不一样 */
export function paramText(params: Record<string, number>): string {
  return Object.keys(params)
    .sort()
    .map((key) => `${key}=${params[key]}`)
    .join(", ");
}
