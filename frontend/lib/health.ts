/**
 * 数据体检面板的纯函数层（E3a）——分级到配色/文案的映射**只有这一处**。
 *
 * 为什么单独一层：级别到颜色的映射是**容易写错又不容易发现**的那类逻辑（error 与 warn
 * 反了不会有任何类型错误），抽成纯函数 + 用例钉死，面板只负责画。
 *
 * 配色**照抄 M4 静态检查面板**的既有约定（`components/strategies/findings-panel.tsx:50`
 * 的 `severity === "error" ? "text-destructive" : "text-warn"`），不另立一套。
 *
 * ⚠ **红灯不在这里判**：`check_status` 由服务端给（`data_health.status_from_counts`），
 * 前端只做「状态 → 怎么说」的翻译。两处各判一次就会有两套口径，改一处漏一处。
 */

import type { HealthCheckBrief, HealthRun, HealthStatus, StaleSource } from "@/lib/types";

export const LEVEL_ORDER = ["error", "warn", "info"] as const;
export type CheckLevel = (typeof LEVEL_ORDER)[number];

/** 级别 → 文字色。`info` 走中性色（它是「记录」，不是好消息也不是坏消息）。 */
const LEVEL_TONE: Record<CheckLevel, string> = {
  error: "text-destructive",
  warn: "text-warn",
  info: "text-ink-3",
};

/** 级别 → 记号。与体检脚本 stdout 的 `✘/⚠/✔` 同族（那边多一个「通过」态）。 */
const LEVEL_MARKER: Record<CheckLevel, string> = {
  error: "✕",
  warn: "!",
  info: "·",
};

export function levelTone(level: CheckLevel): string {
  return LEVEL_TONE[level] ?? "text-ink-3";
}

export function levelMarker(level: CheckLevel): string {
  return LEVEL_MARKER[level] ?? "·";
}

/** 按 error → warn → info 分组，**空组丢掉**（面板上一排空标题是纯噪音）。 */
export function groupChecks(
  checks: readonly HealthCheckBrief[],
): { level: CheckLevel; items: HealthCheckBrief[] }[] {
  return LEVEL_ORDER.map((level) => ({
    level,
    items: checks.filter((check) => check.level === level),
  })).filter((group) => group.items.length > 0);
}

export interface StatusText {
  text: string;
  tone: string;
}

/**
 * 状态 → 一句话 + 文字色。
 *
 * 绿**借用 `--down`**：本仓唯一的绿（它语义上是行情「跌」，这里是「无告警」）——只共用色值、
 * 不共用判断，`data-up`/`data-down` 那两个是价格的语义，别混。
 */
export function statusText(status: HealthStatus | null | undefined): StatusText {
  if (status === "red") return { text: "有 error", tone: "text-destructive" };
  if (status === "yellow") return { text: "有告警", tone: "text-warn" };
  if (status === "green") return { text: "无告警", tone: "text-down" };
  return { text: "还没跑过", tone: "text-ink-3" };
}

/** 台账一行的读数摘要。被写锁跳过的那轮**没有计数可报**，如实说跳过。 */
export function runSummary(run: Pick<HealthRun, "skipped" | "error" | "warn" | "info">): string {
  if (run.skipped) return `跳过（${run.skipped}）`;
  return `error ${run.error ?? 0} · warn ${run.warn ?? 0} · info ${run.info ?? 0}`;
}

/** 耗时。体检整轮 5.5s 量级，秒足够；缺读数给「—」而不是 0s。 */
export function durationText(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * 源侧边界怎么说——**归因就是这一句的全部意义**。
 *
 * 「本地落后」与「源侧没发」在页头上长得一样（都只是「数据截至 X」），这条把后者单独说出来；
 * `stalled_trading_days` 为 null 是「无从判断」（没探过源侧/读数不可解析），**不是 0**。
 */
export function staleSourceText(stale: StaleSource | null | undefined): string {
  if (!stale || stale.stalled_trading_days === null || !stale.source_last) {
    return "源侧边界无从判断（还没探过源侧）";
  }
  if (stale.stalled_trading_days === 0) return `源侧已推进到 ${stale.source_last}`;
  return `源侧止于 ${stale.source_last}（${stale.stalled_trading_days} 个交易日未推进）`;
}

/** 下一次体检的时刻：只取 `HH:MM`。调度器没起（总闸关着）时说清是关的，不显示一个假时刻。 */
export function nextRunText(scheduler: {
  enabled: boolean;
  hour: number;
  minute: number;
  next_run_at: string | null;
}): string {
  const at = `${String(scheduler.hour).padStart(2, "0")}:${String(scheduler.minute).padStart(2, "0")}`;
  if (!scheduler.enabled) return `定时体检未开启（配置里是 ${at}）`;
  return scheduler.next_run_at ? `下次 ${at}` : `已开启（${at}），本次进程内暂无排期`;
}
