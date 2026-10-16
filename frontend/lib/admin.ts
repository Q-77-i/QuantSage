/**
 * 运维页（E2c）的排序键与文案。
 *
 * 键名与**服务端的白名单同源**（`app/api/admin.py` 的 `Literal[...]`）：多一个少一个都会 422，
 * 而 `ORDER BY` 拼的是标识符、参数占位符在那儿没用，所以服务端只能白名单查表——
 * 这份清单就是它的前端镜像。
 */

export const USAGE_SORT_CHOICES = [
  { key: "id", label: "默认（登记顺序）" },
  { key: "email", label: "账号" },
  { key: "active_tasks", label: "在飞任务" },
  { key: "agent_calls_today", label: "今日 Agent 调用" },
  { key: "rejected_today", label: "今日被拒" },
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
