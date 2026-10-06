/**
 * 图表缩放的数学部分——抽成纯函数，因为**触控板手势本身测不了**。
 *
 * 两图共用同一套「序号」口径：`equity_curve` 的日期与 K 线 bar 逐点对齐
 * （后端 `zip(strict=True)`），所以一个 index 区间对两图含义相同。但缩放**各自独立**
 * ——口径见 SPEC §7「图表交互口径」。
 */

/** 可视区间，序号口径（闭区间，`to - from` 即跨度） */
export interface IndexRange {
  from: number;
  to: number;
}

/** 可视跨度下限。再少既无意义，也算不出蜡烛宽度。 */
export const MIN_VISIBLE_BARS = 8;

/** 判定「是否全览」的容差（单位：根）。LWC 的 logical range 在两端会带小数留白，
 *  没有容差会让按钮在刚载入时就闪出来。 */
const FULL_VIEW_TOLERANCE = 1;

/** 捏合灵敏度。库内把缩放系数写死为 `deltaY / 100`，而 macOS 触控板捏合的 deltaY
 *  通常只有 ±1~10，落到图上就是每帧 1%~10%——手感很钝。这里改成指数映射并放大系数，
 *  **手动走查时按手感调这一个常数即可**。 */
const PINCH_SENSITIVITY = 0.015;

export function isFullView(range: IndexRange, total: number): boolean {
  if (total <= 1) return true;
  return range.to - range.from >= total - 1 - FULL_VIEW_TOLERANCE;
}

/**
 * 捏合增量 → 缩放因子。
 *
 * 指数映射保证「放大多少再缩小多少回到原处」（`exp(a)·exp(-a) = 1`），
 * 且正负天然对称。deltaY > 0（下滑）放大跨度 = 缩小，deltaY < 0（捏开）放大。
 */
export function pinchFactor(deltaY: number): number {
  return Math.exp(deltaY * PINCH_SENSITIVITY);
}

/**
 * 以 `anchor` 为锚点收放可视区间。
 *
 * 锚点在区间里的**相对位置保持不变**，于是捏合看起来是「朝指针所在的那根 bar 收放」，
 * 而不是从中间对称地胀缩。结果夹在 `[0, total-1]` 与最小跨度内；放到最大即回到全览。
 */
export function zoomRange(
  range: IndexRange,
  anchor: number,
  factor: number,
  total: number,
): IndexRange {
  const full: IndexRange = { from: 0, to: Math.max(0, total - 1) };
  if (total <= 1) return full;

  const maxSpan = total - 1;
  const minSpan = Math.min(MIN_VISIBLE_BARS - 1, maxSpan);
  const span = clamp(range.to - range.from, minSpan, maxSpan);
  const nextSpan = clamp(span * factor, minSpan, maxSpan);

  if (nextSpan >= maxSpan) return full;

  const ratio = span === 0 ? 0.5 : clamp((anchor - range.from) / span, 0, 1);
  const from = clamp(anchor - ratio * nextSpan, 0, maxSpan - nextSpan);
  return { from, to: from + nextSpan };
}

/**
 * 水平滚轮平移：整段挪动 `delta` 根，**跨度不变**。
 *
 * 到头即停（不循环、不反弹）；全览时无地可挪，返回原样。
 */
export function panRange(range: IndexRange, delta: number, total: number): IndexRange {
  const full: IndexRange = { from: 0, to: Math.max(0, total - 1) };
  if (total <= 1) return full;

  const maxSpan = total - 1;
  const minSpan = Math.min(MIN_VISIBLE_BARS - 1, maxSpan);
  const span = clamp(range.to - range.from, minSpan, maxSpan);
  const from = clamp(range.from + delta, 0, maxSpan - span);
  return { from, to: from + span };
}

/** 像素增量 → 平移根数。按「可视跨度 / 画布宽度」换算，于是缩放前后手感一致。 */
export function panByPixels(deltaX: number, span: number, width: number): number {
  if (width <= 0) return 0;
  return (deltaX / width) * span;
}

/** ECharts 的 `dataZoom` 用百分比表示可视区间，这里与序号口径互转。 */
export function percentToRange(start: number, end: number, total: number): IndexRange {
  const maxSpan = Math.max(1, total - 1);
  return { from: (start / 100) * maxSpan, to: (end / 100) * maxSpan };
}

export function rangeToPercent(range: IndexRange, total: number): { start: number; end: number } {
  const maxSpan = Math.max(1, total - 1);
  return { start: (range.from / maxSpan) * 100, end: (range.to / maxSpan) * 100 };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
