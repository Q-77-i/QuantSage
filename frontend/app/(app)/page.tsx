"use client";

import Link from "next/link";
import { useState } from "react";

import { OverviewPanel } from "@/components/home/overview-panel";
import { SectorPanel } from "@/components/home/sector-panel";
import { useHome } from "@/components/home/use-home";
import { asOf } from "@/lib/market-home";

/** 快捷入口。文案是「去哪儿能干什么」，不是功能名——页头已经有功能名了。 */
const ENTRIES = [
  { href: "/chat", label: "对话", hint: "就一个标的或一条新闻追问，带 PIT 语义的检索" },
  { href: "/backtest", label: "回测", hint: "跑一段历史，看 PIT / 非 PIT 两口径差多少" },
  { href: "/research", label: "深度研究", hint: "多 Agent 并行取证 → 多空辩论 → 出研报" },
  { href: "/paper", label: "模拟盘", hint: "纸上交易，每笔决策先过审批闸门再成交" },
  { href: "/strategies", label: "策略", hint: "写策略，运行前先静态拦下未来函数" },
  { href: "/space", label: "个人空间", hint: "自选股、会话、回测与研报记录" },
] as const;

/**
 * 研究首页（M10a）——**登录后的落地页**。
 *
 * M10a 起 `/` 是这一页，对话挪到了 `/chat`（判据与搬迁清单见 `lib/nav.ts` 与 SPEC §11.1）。
 *
 * 这一页回答的是**「今天的市场是什么样」**，不是研究工具：四个读数读完就该知道
 * 「今天值不值得深挖」，然后从快捷入口走去别的页。所以它**只读不写**，没有任何表单。
 *
 * 两个「截至」刻意并列（行情 / 事件语料）：行情是年度整片重下载、不日更，事件却按日增量，
 * 两者天然不同步。挑一个显示会让「今天的事件配着上周的行情」看起来像同一天的事。
 */
export default function HomePage() {
  const [minSymbols, setMinSymbols] = useState(10);
  const { overview, sectors, loading, error, reload } = useHome(minSymbols);
  const stamps = asOf(overview);

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <div>
          <h1 className="font-heading text-xl font-semibold">研究首页</h1>
          <p className="mt-1 text-sm text-ink-3">
            今天的市场什么样。读完这几个数，再决定往哪挖。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
          <Stamp label="行情截至" value={stamps?.bars ?? null} kind="bars" />
          <Stamp label="事件截至" value={stamps?.events ?? null} kind="events" />
        </div>
      </header>

      {error ? (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center gap-3 rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={reload}
            className="rounded-[var(--radius)] border border-destructive/40 px-2 py-0.5 text-xs"
          >
            重试
          </button>
        </div>
      ) : loading && !overview ? (
        <div className="mt-4 h-28 animate-pulse rounded-[var(--radius)] border border-border bg-muted/60" />
      ) : (
        <OverviewPanel overview={overview} />
      )}

      <div className="mt-8 flex flex-col gap-6">
        <SectorPanel
          sectors={sectors}
          loading={loading}
          error={null}
          minSymbols={minSymbols}
          onMinSymbols={setMinSymbols}
        />
      </div>

      <nav className="mt-8 grid gap-3 border-t border-border pt-4 sm:grid-cols-2 lg:grid-cols-3">
        {ENTRIES.map((entry) => (
          <Link
            key={entry.href}
            href={entry.href}
            data-entry={entry.label}
            className="rounded-[var(--radius)] border border-border px-3 py-2.5 hover:bg-muted"
          >
            <span className="font-heading text-sm font-semibold">{entry.label}</span>
            <span className="mt-0.5 block text-xs text-ink-3">{entry.hint}</span>
          </Link>
        ))}
      </nav>
    </main>
  );
}

/**
 * 「截至」戳。取不到就**不显示**（宁可没有这枚标签，也不显示一个假日期，同页头口径）。
 */
function Stamp({ label, value, kind }: { label: string; value: string | null; kind: string }) {
  if (!value) return null;
  return (
    <span
      data-stamp={kind}
      className="num rounded-[var(--radius)] border border-border px-2 py-0.5"
      title={
        kind === "bars"
          ? "行情是年度整片重下载、不日更，故通常停在事件之后"
          : "事件语料按日增量，截至最近一次 ETL"
      }
    >
      {label} {value}
    </span>
  );
}
