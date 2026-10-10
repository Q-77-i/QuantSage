"use client";

import { statusTone } from "@/lib/paper";
import type { StatusTone } from "@/lib/paper";
import type { PaperDecisionStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 决策六态的徽章。
 *
 * **文案来自服务端**（`status_label`，组件不自己拼）；这里只管颜色与形状。
 *
 * 两条配色纪律，都是被既有页面逼出来的：
 *
 * 1. **不借红绿**。`--up` / `--down` 在本项目是「涨跌 / 买卖」的专用色（A 股口径，
 *    K 线 markers 与成交明细都在用）。让「已驳回」用红、「已成交」用绿，同一张表里
 *    就会出现「红 = 买入」与「红 = 驳回」两义。
 * 2. **`--warn` 一族分两态，靠实心/描边区分**：实心（带 10% 底色）= 等你动手，
 *    描边 = 你已决定、等市场。两态同族不同形，既分出层级又不引入新色相。
 *
 * 底色一律走 `10%` 透明而不是实心填充：实心要配一个「上面的字」的颜色，而 `--warn`
 * 在深色主题下是亮色（白字会掉到 2:1 以下，M4c 记过这条）。文字色直接用 token 本身，
 * 对比度就是既有那组实测值（浅 5.02 / 深 4.68，都过 AA）。
 */

const TONE_CLASS: Record<StatusTone, string> = {
  action: "border-warn/50 bg-warn/10 text-warn",
  waiting: "border-warn/40 text-warn",
  normal: "border-border text-foreground",
  void: "border-border text-ink-3",
  alert: "border-destructive/40 text-destructive",
};

export function StatusBadge({
  status,
  label,
  className,
}: {
  status: PaperDecisionStatus;
  label: string;
  className?: string;
}) {
  return (
    <span
      data-status={status}
      className={cn(
        "inline-flex items-center rounded-[var(--radius)] border px-1.5 py-0.5 text-xs whitespace-nowrap",
        TONE_CLASS[statusTone(status)],
        className,
      )}
    >
      {label}
    </span>
  );
}
