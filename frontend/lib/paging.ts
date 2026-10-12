/**
 * 分页的**纯函数**（M10c 起两处共用：标的列表与快讯流）。
 *
 * 抽出来的理由与 `query.ts` 独立成模块同一条：**同一件事只该有一个定义**。两处各写一遍
 * 「总页数怎么算」「页码怎么从 offset 反推」，迟早一处改了另一处没改——而分页错了不会报错，
 * 只会安静地少一页或多一页。
 *
 * 三条口径：
 *   · **总页数至少 1**（`第 1 / 0 页` 没法读）；
 *   · **当前页由服务端回的 `offset` 反推**，前端不另记一份（两边会漂）；
 *   · **页码 ↔ offset 必须往返一致**（有用例钉着）。
 */

/** 总页数；一条都没有时也是 1 页。 */
export function totalPages(total: number, limit: number): number {
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(limit) || limit <= 0) return 1;
  return Math.ceil(total / limit);
}

/** 当前是第几页（1 起），由服务端的 `offset` 反推。 */
export function pageOf(offset: number, limit: number): number {
  if (!Number.isFinite(limit) || limit <= 0) return 1;
  const safe = Number.isFinite(offset) ? Math.max(0, offset) : 0;
  return Math.floor(safe / limit) + 1;
}

/** 第 `page` 页的起点。非整数向下取整、小于 1 按第 1 页算（`?page=abc` 不该炸）。 */
export function offsetOf(page: number, limit: number): number {
  const safe = Number.isFinite(page) ? Math.floor(page) : 1;
  return Math.max(0, Math.max(1, safe) - 1) * limit;
}

/**
 * 把页码夹进 `[1, 总页数]`。手改地址栏到 `?page=9999` 时落到最后一页，不是空白页。
 *
 * ⚠ **`total` 未知（还没加载）时传 `null`，此时只保下界、不猜上界**——猜了会把 `?page=7`
 * 这种深链直接夹成第 1 页，链接等于失效。
 */
export function clampPage(page: number, total: number | null, limit: number): number {
  const safe = Number.isFinite(page) ? Math.floor(page) : 1;
  const lower = Math.max(1, safe);
  return total === null ? lower : Math.min(lower, totalPages(total, limit));
}

/** 这一页显示的是第几条到第几条（1 起、闭区间）。空页返回 `null`。 */
export function rangeText(offset: number, count: number, total: number): string | null {
  if (count <= 0) return null;
  return `${offset + 1}–${Math.min(offset + count, total)}`;
}
