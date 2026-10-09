/**
 * 查询串构造：空值（`undefined` / `null` / 空串）一律不写进 URL。
 *
 * 独立成模块而不是挂在 `api.ts` 里：`factor-report.ts` 也要用它拼查询串，
 * 而 `api.ts` 又要 import `factor-report` 的类型——放一起就是循环依赖。
 *
 * 返回**带前导 `?`** 的串（没有参数时是空串），调用方直接拼在路径后面。
 */
export function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
