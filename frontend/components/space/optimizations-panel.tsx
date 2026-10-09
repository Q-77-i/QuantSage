"use client";

import { Section } from "@/components/backtest/chart-frame";
import { RunsList } from "@/components/optimize/runs-list";

/**
 * 个人空间的「我的优化」页签（M5b）。
 *
 * 与「我的回测」并列摆着：两者都是「这个账号跑过什么」，差别只在记录的粒度——
 * 回测是一次一份完整报告，优化是一次一批摘要（每格只有指标，没有曲线与逐笔）。
 * 要看清某一格，从 `/optimize?run=` 点进去再点那一格重跑。
 */
export function OptimizationsPanel() {
  return (
    <Section title="我的优化" hint="网格 / 批量的汇总；点一条重开（不重跑）">
      <RunsList limit={20} emptyText="还没有跑过网格或批量——去「优化」页跑一次。" />
    </Section>
  );
}
