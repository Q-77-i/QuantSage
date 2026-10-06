"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { RunsPanel } from "@/components/space/runs-panel";
import { StrategiesPanel } from "@/components/space/strategies-panel";
import { ThreadsPanel } from "@/components/space/threads-panel";
import { WatchlistPanel } from "@/components/space/watchlist-panel";

/**
 * 个人空间（M1c）：我的自选 / 我的回测 / 会话历史 / 我的策略。
 *
 * 页签做成一页，是因为这四块都是「这个账号有什么」——分开摆会让人以为它们是四个功能。
 * 页签状态放地址栏（`?tab=`）：刷新与分享都能回到同一格，返回键也能退回上一格。
 *
 * 整个页签体包在 Suspense 里：`useSearchParams` 在生产构建下要求边界，而这一页的内容
 * 本来就是客户端取数（fallback 用骨架，与挂载后的第一帧同形，不会闪）。
 */
const TABS = [
  { key: "watchlist", label: "我的自选" },
  { key: "runs", label: "我的回测" },
  { key: "threads", label: "会话历史" },
  { key: "strategies", label: "我的策略" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const DEFAULT_TAB: TabKey = "watchlist";

function isTabKey(value: string | null): value is TabKey {
  return TABS.some((tab) => tab.key === value);
}

export default function SpacePage() {
  return (
    <Suspense fallback={<Bootstrap />}>
      <SpaceTabs />
    </Suspense>
  );
}

function SpaceTabs() {
  const params = useSearchParams();
  const router = useRouter();
  const raw = params.get("tab");
  const active: TabKey = isTabKey(raw) ? raw : DEFAULT_TAB;

  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <h1 className="font-heading text-xl font-semibold">个人空间</h1>

      <nav className="mt-4 flex gap-1 border-b border-border">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => router.replace(`/space?tab=${tab.key}`)}
            aria-current={active === tab.key ? "page" : undefined}
            className={`-mb-px rounded-t-[var(--radius)] border-b-2 px-3 py-1.5 text-sm ${
              active === tab.key
                ? "border-brand font-medium text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <div className="mt-6">
        {active === "watchlist" && <WatchlistPanel />}
        {active === "runs" && <RunsPanel />}
        {active === "threads" && <ThreadsPanel />}
        {active === "strategies" && <StrategiesPanel />}
      </div>
    </main>
  );
}

function Bootstrap() {
  return (
    <main className="mx-auto max-w-[1400px] px-4 py-6">
      <div className="h-6 w-24 animate-pulse rounded-[var(--radius)] bg-muted" />
    </main>
  );
}
