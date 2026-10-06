/**
 * 自选股的展示口径（纯函数，可单测）。
 *
 * 后端给的是**扁平列表**：分组只是每行上的 `group_name` 字符串列，没有分组实体表
 * （见 SPEC §2），所以「有哪些组、谁排在前、组内怎么排」全是展示口径，落在这里。
 */

import type { WatchlistItem, WatchlistGroup } from "./types";

/** 与后端 `core/db.py` 的常量同名同值：删组时的回落目标，也是展示时的置顶组。 */
export const DEFAULT_GROUP = "默认分组";

/** 股票代码 = 六位数字。后端也会拒（422），这里先挡一道，省一次往返。 */
export function validateSymbol(raw: string): string | null {
  const value = raw.trim();
  if (!value) return "请输入股票代码";
  if (!/^\d{6}$/.test(value)) return "股票代码应是 6 位数字";
  return null;
}

/**
 * 扁平列表 → 分组视图。
 *
 * 排序口径：**「默认分组」恒在首位**（它是回落目标，位置固定才一眼找得到，否则会按拼音
 * 散在中间），其余组按组内最早加入时间升序；组内也按加入时间升序——「先加的在上」是
 * 这个列表唯一有意义的顺序，按涨幅排会让每次刷新都换位置。
 */
export function groupItems(items: WatchlistItem[]): WatchlistGroup[] {
  const buckets = new Map<string, WatchlistItem[]>();
  for (const item of items) {
    const bucket = buckets.get(item.group_name);
    if (bucket) bucket.push(item);
    else buckets.set(item.group_name, [item]);
  }

  const groups = [...buckets.entries()].map(([name, rows]) => ({
    name,
    items: [...rows].sort((a, b) => a.added_at.localeCompare(b.added_at)),
  }));

  return groups.sort((a, b) => {
    if (a.name === DEFAULT_GROUP) return -1;
    if (b.name === DEFAULT_GROUP) return 1;
    return a.items[0].added_at.localeCompare(b.items[0].added_at);
  });
}

/** 现有分组名（移动标的时的候选），顺序与展示一致。 */
export function groupNames(items: WatchlistItem[]): string[] {
  return groupItems(items).map((group) => group.name);
}
