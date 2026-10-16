/**
 * 运维页（E2c）的排序键与文案。
 *
 * 键名与**服务端的白名单同源**（`app/api/admin.py` 的 `Literal[...]`）：多一个少一个都会 422，
 * 而 `ORDER BY` 拼的是标识符、参数占位符在那儿没用，所以服务端只能白名单查表——
 * 这份清单就是它的前端镜像。
 */

/** 时间窗三档（用户 2026-10-16 定：固定档，不做自定义起止日期）。`days` 直接进查询串。 */
export const USAGE_RANGE_CHOICES = [
  { days: 1, label: "今日" },
  { days: 7, label: "近 7 天" },
  { days: 30, label: "近 30 天" },
] as const;

export type UsageRangeDays = (typeof USAGE_RANGE_CHOICES)[number]["days"];

/** 档位文案。列头与说明行都要用它——**口径变了名字就得跟着变**。 */
export function rangeLabel(days: number): string {
  return USAGE_RANGE_CHOICES.find((choice) => choice.days === days)?.label ?? "今日";
}

export const USAGE_SORT_CHOICES = [
  { key: "id", label: "默认（登记顺序）" },
  { key: "email", label: "账号" },
  { key: "active_tasks", label: "在飞任务" },
  { key: "agent_calls", label: "Agent 调用" },
  { key: "rejected", label: "被拒" },
] as const;

export type UsageSortKey = (typeof USAGE_SORT_CHOICES)[number]["key"];

export type SortOrder = "asc" | "desc";

/** 方向按钮上的文案：说的是**当前**是哪种（与标的浏览同一个交互）。 */
export function orderLabel(order: SortOrder): string {
  return order === "desc" ? "降序" : "升序";
}

/** 点一下换向。 */
export function toggleOrder(order: SortOrder): SortOrder {
  return order === "desc" ? "asc" : "desc";
}
