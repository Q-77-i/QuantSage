/**
 * 研报页的**纯函数**：块分发、指标卡、归因表、复盘卡、证据行、分享与导出。
 *
 * 分工（同 `lib/paper.ts` / `lib/factor-report.ts`）：
 *   * 服务端给**事实**（中文文案、数字、口径说明都在正文里）；
 *   * 这里把事实翻成**视图模型**（哪一块显示什么、什么单位、什么语气）；
 *   * 组件只管画。
 *
 * 两条刻意的克制：
 *   1. **块清单不写死**——正文的 `blocks[]` 是契约，M8 的深度研报接同一结构；未知块兜底渲染
 *      `text`，这页不必为它改一行（`blockView` 返回 `"unknown"` 时组件照常出文本）。
 *   2. **单位不猜**——`metrics.*` 是比例走 `pct()`，复盘里的 `alpha_pp` 是百分点走 `pp()`；
 *      两者混用会差 100 倍且不报错（`lib/format.ts` 的头注就是这么写的）。
 */

import { EMPTY, amount, count, eventStamp, num, pct, pp, shortHash } from "./format";
import { reasonText } from "./paper";
import type {
  ReportBlock,
  ReportBlockKind,
  ReportBody,
  ReportEvidence,
  ReportMetrics,
  ReportReview,
  ReportReviewItem,
} from "./types";

// ── 块分发 ────────────────────────────────────────────────────────────────

export type BlockView =
  | "overview"
  | "performance"
  | "attribution"
  | "review"
  | "narrative"
  | "unknown";

/** 已知块 → 专用渲染器；其余一律 `unknown`（兜底渲染 `text` + note）。 */
export function blockView(block: ReportBlock): BlockView {
  switch (block.id) {
    case "overview":
      return "overview";
    case "performance":
      return "performance";
    case "attribution":
      return "attribution";
    case "review":
      return "review";
    case "narrative":
      return "narrative";
    default:
      return "unknown";
  }
}

/** claim 分级徽章：事实（中性描边）/ 推断（warn 描边）。PRD 要求它可见。 */
export function kindBadge(kind: ReportBlockKind): { label: string; tone: "fact" | "inference" } {
  return kind === "fact"
    ? { label: "事实", tone: "fact" }
    : { label: "推断", tone: "inference" };
}

// ── 指标卡 ────────────────────────────────────────────────────────────────

export interface MetricCell {
  key: string;
  label: string;
  value: string;
  hint: string;
  /** 首行三张大卡：累计 / 超额 / 回撤 */
  primary: boolean;
  tone: "plain" | "up" | "down";
}

function signedTone(value: number | null): MetricCell["tone"] {
  if (value === null || value === 0) return "plain";
  return value > 0 ? "up" : "down";
}

/** 指标 → 卡片。带号与单位**按语义写死**（回撤是正值幅度，不能统加 `+`）。 */
export function metricCells(metrics: ReportMetrics): MetricCell[] {
  return [
    {
      key: "total_return",
      label: "累计收益",
      value: pct(metrics.total_return, { signed: true }),
      hint: "账户净值口径",
      primary: true,
      tone: signedTone(metrics.total_return),
    },
    {
      key: "excess_return",
      label: "超额收益",
      value: pct(metrics.excess_return, { signed: true }),
      hint: "对全市场等权（同期）",
      primary: true,
      tone: signedTone(metrics.excess_return),
    },
    {
      key: "max_drawdown",
      label: "最大回撤",
      value: pct(metrics.max_drawdown),
      hint: "峰值回撤幅度（正值）",
      primary: true,
      tone: "plain",
    },
    {
      key: "annual_return",
      label: "年化收益",
      value: pct(metrics.annual_return, { signed: true }),
      hint: "按 252 个交易日折算",
      primary: false,
      tone: signedTone(metrics.annual_return),
    },
    {
      key: "benchmark_return",
      label: "全市场等权",
      value: pct(metrics.benchmark_return, { signed: true }),
      hint: "同窗口基准收益",
      primary: false,
      tone: signedTone(metrics.benchmark_return),
    },
    {
      key: "volatility",
      label: "年化波动率",
      value: pct(metrics.volatility),
      hint: "日收益样本标准差 × √252",
      primary: false,
      tone: "plain",
    },
    {
      key: "sharpe",
      label: "夏普",
      value: num(metrics.sharpe),
      hint: "rf=0；样本不足或净值恒定时为 —",
      primary: false,
      tone: "plain",
    },
    {
      key: "win_rate",
      label: "胜率",
      value: pct(metrics.win_rate),
      hint: "只算已平仓回合",
      primary: false,
      tone: "plain",
    },
    {
      key: "trade_count",
      label: "已平仓回合",
      value: count(metrics.trade_count),
      hint: "一买一卖算一次",
      primary: false,
      tone: "plain",
    },
    {
      key: "final_equity",
      label: "期末权益",
      value: amount(metrics.final_equity),
      hint: "现金 + 持仓市值",
      primary: false,
      tone: "plain",
    },
  ];
}

// ── 归因表 ────────────────────────────────────────────────────────────────

export interface TableView {
  key: string;
  title: string;
  hint: string;
  headers: string[];
  rows: string[][];
  /** 无行时的说明（**不留空表格**：空表与「本次没有」是两句话） */
  empty: string;
}

/** 归因三表。行业口径的注脚由页面常驻展示（与后端 `blocks` 里的说明同源）。 */
export function attributionTables(body: ReportBody): TableView[] {
  const { symbols, direction, industry } = body.attribution;
  return [
    {
      key: "symbols",
      title: "标的级",
      hint: "贡献 = （已实现 + 未平仓浮盈）/ 初始资金",
      headers: ["标的", "回合", "已平仓", "胜", "已实现", "未平仓浮盈", "贡献", "未估值"],
      rows: symbols.map((row) => [
        row.symbol,
        count(row.trips),
        count(row.closed),
        count(row.wins),
        amount(row.realized_pnl, { signed: true }),
        amount(row.unrealized_pnl, { signed: true }),
        pp(row.contribution_pp, { signed: true }),
        row.unmarked ? count(row.unmarked) : EMPTY,
      ]),
      empty: "本次没有可归因的回合",
    },
    {
      key: "direction",
      title: "驱动事件方向",
      hint: "按买入决策的驱动事件方向分组",
      headers: ["方向", "回合", "已平仓", "胜", "盈亏", "未估值"],
      rows: direction.map((row) => [
        row.label,
        count(row.trips),
        count(row.closed),
        count(row.wins),
        amount(row.pnl, { signed: true }),
        row.unmarked ? count(row.unmarked) : EMPTY,
      ]),
      empty: "本次买入没有事件来源（纯价量策略属正常）",
    },
    {
      key: "industry",
      title: "驱动事件行业",
      hint: "口径 = 事件自身的 industries",
      headers: ["行业", "回合", "已平仓", "胜", "盈亏", "未估值"],
      rows: industry.map((row) => [
        row.label,
        count(row.trips),
        count(row.closed),
        count(row.wins),
        amount(row.pnl, { signed: true }),
        row.unmarked ? count(row.unmarked) : EMPTY,
      ]),
      empty: "本次买入没有事件来源（纯价量策略属正常）",
    },
  ];
}

// ── 复盘 ──────────────────────────────────────────────────────────────────

export interface ReviewCardView {
  key: string;
  symbol: string;
  tone: "settled" | "open";
  statusLabel: string;
  window: string;
  pnl: string;
  pnlTone: MetricCell["tone"];
  returnText: string;
  benchmarkText: string;
  alphaText: string;
  entryReason: string;
  exitReason: string;
  reflectionText: string | null;
  reflectionByline: string | null;
  reflectionNote: string | null;
  evidenceKey: string | null;
}

export interface UnfilledRow {
  key: string;
  symbol: string;
  tradeDate: string;
  sideLabel: string;
  statusLabel: string;
  reason: string | null;
}

function reviewCard(item: ReportReviewItem, dataEnd: string): ReviewCardView {
  const settled = item.settled;
  return {
    key: item.decision_id,
    symbol: item.symbol,
    tone: settled ? "settled" : "open",
    statusLabel: settled ? "已到期" : `未到期（数据止于 ${dataEnd}）`,
    window: `${item.entry_date} → ${item.exit_date ?? "未平仓"}（${item.window_days} 个交易日）`,
    pnl: amount(item.pnl, { signed: true }),
    pnlTone: signedTone(item.pnl),
    returnText: pct(item.return_pct, { signed: true }),
    benchmarkText: pct(item.benchmark_pct, { signed: true }),
    alphaText: pp(item.alpha_pp, { signed: true }),
    // 理由走 `lib/paper.ts::reasonText` 的可读化（`event_driven:news:123 score:88` → 中文），
    // 与 `/paper` 的决策卡同一套——同一串理由在两处不该长得不一样
    entryReason: item.entry_reason ? reasonText(item.entry_reason) : EMPTY,
    exitReason: item.exit_reason ? reasonText(item.exit_reason) : EMPTY,
    reflectionText: item.reflection?.text ?? null,
    reflectionByline: item.reflection?.text
      ? `模型 ${item.reflection.model}${item.reflection.prompt_version ? ` · ${item.reflection.prompt_version}` : ""}`
      : null,
    reflectionNote: item.reflection?.note ?? null,
    evidenceKey: item.evidence_key,
  };
}

/** 复盘三段：已到期（有教训）/ 未到期（没有结果可总结）/ 定了没交易（只列状态）。 */
export function reviewCards(
  review: ReportReview | null,
  dataEnd: string,
): { settled: ReviewCardView[]; open: ReviewCardView[]; unfilled: UnfilledRow[] } {
  if (!review) return { settled: [], open: [], unfilled: [] };
  return {
    settled: review.settled.map((item) => reviewCard(item, dataEnd)),
    open: review.open.map((item) => reviewCard(item, dataEnd)),
    unfilled: review.unfilled.map((item) => ({
      key: item.decision_id,
      symbol: item.symbol,
      tradeDate: item.trade_date,
      sideLabel: item.side === "buy" ? "买入" : "卖出",
      // 状态文案**原样吃服务端的**（同 M6b：六态中文只有一处真源）
      statusLabel: item.status_label,
      reason: item.reject_reason || null,
    })),
  };
}

// ── 证据 ──────────────────────────────────────────────────────────────────

/** 方向 → 中文。措辞与 `components/backtest/events-table.tsx` 的 DIRECTION 一致（同一份语料两处展示）。 */
const DIRECTION_LABELS: Record<string, string> = {
  bullish: "利多",
  bearish: "利空",
  neutral: "中性",
};

export interface EvidenceRow {
  label: string;
  value: string;
  href?: string;
  mono?: boolean;
}

/** 一条证据 → 展示行。**事发与可得并列**（PIT 语义最直观的展示位，与 `/paper` 的来源三元组同序）。 */
export function evidenceRows(item: ReportEvidence): EvidenceRow[] {
  const rows: EvidenceRow[] = [];
  rows.push({ label: "事发", value: eventStamp(item.event_time) });
  rows.push({ label: "可得", value: eventStamp(item.available_at) });
  if (item.summary) rows.push({ label: "摘要", value: item.summary });
  rows.push({ label: "来源", value: item.source || EMPTY });
  rows.push({ label: "原始来源", value: item.original_source || EMPTY });
  rows.push({ label: "内容哈希", value: shortHash(item.content_hash), mono: true });
  if (item.direction_norm) {
    rows.push({ label: "方向", value: DIRECTION_LABELS[item.direction_norm] ?? item.direction_norm });
  }
  if (item.industries.length) rows.push({ label: "行业", value: item.industries.join("、") });
  if (item.source_url) rows.push({ label: "原文", value: item.source_url, href: item.source_url });
  return rows;
}

/** 证据的两种告警（**都如实说**，不静默展示成正常事件）。 */
export function evidenceNotice(item: ReportEvidence): string | null {
  if (!item.found) return "本地语料查无此行（按决策当时的快照如实展示）";
  if (item.revised) {
    return `该事件已被平台修订（快照 ${shortHash(item.content_hash)} → 现在 ${shortHash(item.corpus_hash)}）`;
  }
  return null;
}

/** 证据键 → 证据项（块上的 `evidence[]` 是键，正文里的 `evidence[]` 是项）。 */
export function evidenceIndex(items: ReportEvidence[]): Map<string, ReportEvidence> {
  return new Map(items.map((item) => [`${item.event_id}|${item.day}`, item]));
}

export function evidenceForBlock(
  block: ReportBlock,
  index: Map<string, ReportEvidence>,
): ReportEvidence[] {
  return (block.evidence ?? [])
    .map((key) => index.get(key))
    .filter((item): item is ReportEvidence => item !== undefined);
}

// ── 概览块的数字标签 ──────────────────────────────────────────────────────

/** 键路径 → 中文标签。只收正文里真会出现的几个；认不出的回落到路径末段（不编名字）。 */
const NUMBER_LABELS: Record<string, string> = {
  "account.initial_cash": "初始资金",
  "metrics.final_equity": "期末权益",
};

export function numberLabel(path: string): string {
  return NUMBER_LABELS[path] ?? path.split(".").pop() ?? path;
}

// ── 分享与导出 ────────────────────────────────────────────────────────────

/** 分享 URL：**由前端按当前 origin 拼**（后端只给 `share_path`，部署换域名不用改配置）。 */
export function shareUrl(origin: string, sharePath: string | null): string | null {
  if (!sharePath) return null;
  return `${origin.replace(/\/$/, "")}${sharePath}`;
}

/** Markdown 下载地址：公开页走 token 端点，登录页走受保护端点（**两处都能导出**）。 */
export function markdownHref(apiBase: string, reportId: string, token: string | null): string {
  return token
    ? `${apiBase}/api/v1/public/reports/${encodeURIComponent(token)}/markdown`
    : `${apiBase}/api/v1/reports/${encodeURIComponent(reportId)}/markdown`;
}

/** 下载文件名：账户名 + 报告指纹短码（同名多份也不互相覆盖）。 */
export function downloadName(accountName: string, reportHash: string): string {
  const safe = accountName.replace(/[\\/:*?"<>|\s]+/g, "-").slice(0, 40) || "report";
  return `${safe}-${shortHash(reportHash, 8)}.md`;
}

/** 页头指纹行：报告与数据快照各取前 12 位（报告是冻结产物，要能自证「看的是哪一份」）。 */
export function fingerprintLine(reportHash: string, snapshotDigest: string | undefined): string {
  return `报告 ${shortHash(reportHash)} ｜ 数据快照 ${shortHash(snapshotDigest ?? null)}`;
}
