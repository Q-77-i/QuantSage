/**
 * 数值与时间的显示格式化。**单位换算与带号规则的唯一定义处。**
 *
 * 后端报告里的数字是两种单位混着的，走错一条路径就会错 100 倍，而且不报错：
 *   · `metrics.*` 与 `delta.final_equity_pct` 是**比例**（0.124 表示 12.4%）→ 用 `pct()`
 *   · `delta.*_pp` 后端已经 ×100，是**百分点**（0.68 表示 0.68 个百分点）→ 用 `pp()`
 * 函数名与后端字段后缀对齐：`pp(delta.total_return_pp)` 一眼能看出没配错。
 *
 * 带号与否**按指标语义指定，绝不按数值正负判断**——`max_drawdown` 后端返回的是正值
 * 幅度，若统一「正数加 +」会把回撤显示成 `+8.1%`。
 *
 * 一律显式 `Intl.NumberFormat`，不用裸 `toLocaleString()`：客户端组件仍会被 SSR，
 * Node 与浏览器的默认 locale 不保证一致，产出不同就会 hydration mismatch。
 */

/** 缺失值占位：不是 0，也不是空串。「无法计算」与「等于 0」是两句不同的话。 */
export const EMPTY = "—";

export interface FormatOptions {
  /** 是否带正负号。零值不带号（`signDisplay: "exceptZero"`）。 */
  signed?: boolean;
  /** 小数位上限（尾随零不补）。 */
  digits?: number;
}

function build(options: Intl.NumberFormatOptions, value: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY;
  return new Intl.NumberFormat("zh-CN", options).format(value);
}

function signDisplay(signed: boolean | undefined): Intl.NumberFormatOptions["signDisplay"] {
  return signed ? "exceptZero" : "auto";
}

/** 比例 → 百分数。`0.124` → `12.4%`。用于 `metrics.*` 与 `delta.final_equity_pct`。 */
export function pct(value: number | null, { signed, digits = 2 }: FormatOptions = {}): string {
  return build(
    { style: "percent", maximumFractionDigits: digits, signDisplay: signDisplay(signed) },
    value,
  );
}

/** 百分点 → 百分数。`0.68` → `0.68pp`。用于 `delta.*_pp`，**不要**喂比例进去。 */
export function pp(value: number | null, { signed, digits = 2 }: FormatOptions = {}): string {
  const text = build(
    { maximumFractionDigits: digits, signDisplay: signDisplay(signed) },
    value,
  );
  return text === EMPTY ? EMPTY : `${text}pp`;
}

/** 金额。`997329.155` → `997,329`。 */
export function amount(value: number | null, { signed, digits = 0 }: FormatOptions = {}): string {
  return build(
    { maximumFractionDigits: digits, signDisplay: signDisplay(signed) },
    value,
  );
}

/**
 * **已是百分数**的值 → 带 `%` 号。`-0.102` → `-0.1%`。用于 `change_pct` 这类源侧就是
 * 百分数的字段（M10 起）。**第三条路径，必须与另外两条分得清**：
 *
 * | 输入是什么 | 用它 | 例 |
 * |---|---|---|
 * | 比例（0.124 表示 12.4%） | `pct()` | `metrics.total_return` |
 * | 已是百分数，且字段名就叫「百分点」 | `pp()` | `delta.total_return_pp` |
 * | 已是百分数，语义是涨跌幅 / 收益率 | `rate()` | `change_pct` |
 *
 * 三条都吃同一个 `build`，差别只在后缀；分成三个名字是为了让调用处**一眼看出没配错**——
 * 100 倍的错不会报错，只会安静地显示一个错的数。
 */
export function rate(value: number | null, { signed, digits = 2 }: FormatOptions = {}): string {
  const text = build(
    { maximumFractionDigits: digits, signDisplay: signDisplay(signed) },
    value,
  );
  return text === EMPTY ? EMPTY : `${text}%`;
}

/**
 * 元 → **亿元**（A 股口径的成交额单位）。`1450089493893` → `14,500.9`。
 * 小数位是**上限**：`7603661152` → `76`（不是 `76.0`）——尾随零不补，那一位没有信息。
 *
 * 只换单位、不带「亿」字：中文里的「亿」在排版上常要单独变小字，那属于组件的事。
 * 缺失值照旧走 `EMPTY`，**不把 null 当 0 元**。
 */
export function yi(value: number | null, digits = 1): string {
  return build({ maximumFractionDigits: digits }, value === null ? null : value / 1e8);
}

/** 无单位的数值（夏普一类）。 */
export function num(value: number | null, { signed, digits = 2 }: FormatOptions = {}): string {
  return build(
    { maximumFractionDigits: digits, signDisplay: signDisplay(signed) },
    value,
  );
}

/** 计数。 */
export function count(value: number | null): string {
  return build({ maximumFractionDigits: 0 }, value);
}

// ── 时间 ────────────────────────────────────────────────────────────────────

/** A 股口径：数据源的 `event_time` / `available_at` 都按北京时间理解。 */
const EVENT_STAMP = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/**
 * 事件时间戳 → `07-15 10:12`。
 *
 * 显式钉 `Asia/Shanghai`：后端交给我们的偏移取决于 DuckDB 会话时区（落盘是 TIMESTAMPTZ），
 * 不能指望它恒为 `+08:00`，也不能用浏览器的本地时区——两端显示同一条事件必须得到同一个钟点。
 */
export function eventStamp(iso: string | null): string {
  if (!iso) return EMPTY;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso; // 认不出就原样显示，不吞掉线索
  const parts: Record<string, string> = {};
  for (const part of EVENT_STAMP.formatToParts(parsed)) parts[part.type] = part.value;
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

const DAY_STAMP = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/**
 * 时间戳 → `2026-07-15 10:12`（列表用，带年份）。
 *
 * 与 `eventStamp` 同一套时区口径，只差年份：会话历史与回测记录是跨月摆放的，
 * 少了年份就得靠上下文猜。
 */
export function dayStamp(iso: string | null): string {
  if (!iso) return EMPTY;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  const parts: Record<string, string> = {};
  for (const part of DAY_STAMP.formatToParts(parsed)) parts[part.type] = part.value;
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/**
 * **只到日**：`2026-09-30` → `09-30`。日期带与按日分组头用它（`dayStamp` 一定带时分）。
 *
 * **走纯字符串切片，不过 `Date`**：服务端按日分组是在 SQL 里做完的（`event_time::DATE`），
 * 交过来就是北京时间的日子；再拿 `Date` 解析一遍等于把同一个换算做第二次，两边必然漂。
 */
export function dayOnly(day: string | null): string {
  if (!day) return EMPTY;
  return day.length >= 10 ? day.slice(5, 10) : day;
}

const WEEKDAY = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", weekday: "short" });

/** `2026-09-30` → `周三`。日期带上帮读者定位——「调休上班的周六」一眼看得出来。 */
export function weekday(day: string | null): string {
  if (!day) return EMPTY;
  const parsed = new Date(`${day.slice(0, 10)}T00:00:00+08:00`);
  if (Number.isNaN(parsed.getTime())) return EMPTY;
  return WEEKDAY.format(parsed);
}

/** 截断长哈希一类。`abc…` 前 `n` 位（`content_hash` 展示用）。 */
export function shortHash(value: string | null, n = 12): string {
  if (!value) return EMPTY;
  return value.length > n ? value.slice(0, n) : value;
}
