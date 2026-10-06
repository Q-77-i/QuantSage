"use client";

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** 分节外壳：标题 + 右侧说明 + 内容。数据区一律用 1px 分隔线分组，不套卡片盒。 */
export function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="border-t border-border pt-4">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="font-heading text-base">{title}</h2>
        {hint ? <p className="text-xs text-ink-3">{hint}</p> : null}
      </header>
      {children}
    </section>
  );
}

/**
 * 图表容器：固定高度的盒子 + 加载/空/错三态。
 *
 * 高度必须**常驻且显式**（不用百分比、不靠内容撑）：两个图表库都在 canvas 上绘制，
 * 容器尺寸为 0 时建图只会得到一张空图；而骨架屏与真图共用一个盒子，数据到达时才不会
 * 把下面的内容顶下去。骨架与最终布局同形，不用居中转圈。
 */
export function ChartFrame({
  title,
  hint,
  loading,
  error,
  empty,
  children,
}: {
  title: string;
  hint?: ReactNode;
  loading?: boolean;
  error?: string | null;
  empty?: string | null;
  children?: ReactNode;
}) {
  return (
    <Section title={title} hint={hint}>
      <div className="relative mt-3 h-[300px] overflow-hidden rounded-[var(--radius)] border border-border bg-chart-surface md:h-[340px]">
        {loading ? (
          <div className="absolute inset-0 animate-pulse bg-muted/60" />
        ) : error ? (
          <Notice text={error} tone="error" />
        ) : empty ? (
          <Notice text={empty} />
        ) : (
          children
        )}
      </div>
    </Section>
  );
}

function Notice({ text, tone }: { text: string; tone?: "error" }) {
  return (
    <p
      className={cn(
        "absolute inset-0 flex items-center justify-center px-6 text-center text-sm",
        tone === "error" ? "text-destructive" : "text-ink-2",
      )}
    >
      {text}
    </p>
  );
}
