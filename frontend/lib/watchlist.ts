/**
 * 自选股的展示口径（纯函数，可单测）。
 *
 * 后端给的是**扁平列表**：分组只是每行上的 `group_name` 字符串列，没有分组实体表
 * （见 SPEC §2），所以「有哪些组、谁排在前、组内怎么排」全是展示口径，落在这里。
 */

import type { WatchlistItem, WatchlistGroup } from "./types";

/** 与后端 `core/db.py` 的常量同名同值：删组时的回落目标，也是展示时的置顶组。 */
export const DEFAULT_GROUP = "默认分组";

/**
 * 输入的是「按名称搜」还是「按代码查」（M5a）。
 *
 * 分流按**输入形状**而不是猜：含任何非数字字符就走名称搜索（代码永远是纯数字）。
 * 纯函数且单独可测——它是这个表单两条路径的分岔口，判错的后果是「输中文时静默无反应」。
 */
export function isNameQuery(raw: string): boolean {
  const value = raw.trim();
  return value !== "" && /\D/.test(value);
}

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

/**
 * 代码体检的状态（`GET /market/{symbol}/probe` 的界面侧）。
 *
 * 每个变体都带 `code`：**结果属于哪个代码**。输入改一位就是另一个问题了，陈旧响应
 * 贴到新输入上会给出一个看似合理、其实答非所问的提示——带上 code 让这种错配在
 * 纯函数里就没法发生，不必让组件再维护一套序号。
 */
export type Probe =
  | { status: "idle" }
  | { status: "checking"; code: string }
  | { status: "found"; code: string; date: string | null; close: number | null }
  | { status: "missing"; code: string }
  | { status: "unknown"; code: string };

/** 加自选表单此刻该说什么。`missing` 是唯一会禁用「加自选」的一档。 */
export type AddHint =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "duplicate"; group: string }
  | { kind: "missing" }
  | { kind: "found"; date: string | null; close: number | null }
  | { kind: "unknown" };

/**
 * 输入 → 提示。
 *
 * 三处口径：
 *   * **没输满六位一律不吭声**——正在打字时弹出「应是 6 位数字」只是噪音，校验仍留给提交那一下；
 *   * **判重在前，体检在后**：已在自选时列表本身就是答案，体检结果再准也无关（列表还在加载
 *     `null` 时不判重，照常体检——宁可多显示一次「有数据」，也不能凭空说「已在自选」）；
 *   * **`unknown` 与 `missing` 分开**：行情层不可用时每个代码都查不到，当成 `missing`
 *     会让表单对所有输入禁用（行情依赖把自选股拖死）——那一档不禁用。
 */
export function addFormHint(
  raw: string,
  items: WatchlistItem[] | null,
  probe: Probe,
): AddHint {
  const code = raw.trim();
  if (validateSymbol(code) !== null) return { kind: "idle" };

  const tracked = items?.find((row) => row.symbol === code);
  if (tracked) return { kind: "duplicate", group: tracked.group_name };

  switch (probe.status) {
    case "found":
      return probe.code === code
        ? { kind: "found", date: probe.date, close: probe.close }
        : { kind: "checking" };
    case "missing":
      return probe.code === code ? { kind: "missing" } : { kind: "checking" };
    case "unknown":
      return probe.code === code ? { kind: "unknown" } : { kind: "checking" };
    case "checking":
      return { kind: "checking" };
    default:
      return { kind: "idle" };
  }
}
