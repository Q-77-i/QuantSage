"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { ThemeToggle } from "@/components/theme-toggle";
import { api } from "@/lib/api";
import type { DataFreshness } from "@/lib/types";

/**
 * 全站页头：品牌 + 四项导航 + 当前用户 + 主题切换。导航在桌面必须单行（设计规范）。
 *
 * 只在受保护路由组内渲染，所以 `user` 必然非空（守卫已挡在前面）。
 */
export function AppHeader() {
  const { user, logout } = useAuth();
  const router = useRouter();
  const freshness = useFreshness();

  async function handleLogout() {
    await logout();
    router.replace("/login");
  }

  return (
    <header className="border-b border-border">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-4">
        <Link href="/" className="font-heading text-lg font-semibold tracking-tight">
          知策 <span className="text-muted-foreground">QuantSage</span>
        </Link>

        <nav className="flex items-center gap-1 text-sm whitespace-nowrap">
          <Link
            href="/"
            className="rounded-[var(--radius)] px-2.5 py-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            对话
          </Link>
          <Link
            href="/backtest"
            className="rounded-[var(--radius)] px-2.5 py-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            回测
          </Link>
          <Link
            href="/strategies"
            className="rounded-[var(--radius)] px-2.5 py-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            策略
          </Link>
          <Link
            href="/space"
            className="rounded-[var(--radius)] px-2.5 py-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            个人空间
          </Link>
        </nav>

        <div className="ml-auto flex items-center gap-3">
          {freshness?.latest_trade_date ? (
            <span
              className="num hidden text-xs text-ink-3 md:inline"
              title={freshnessTooltip(freshness)}
            >
              数据截至 {freshness.latest_trade_date}
            </span>
          ) : null}
          <span className="hidden text-xs text-ink-3 sm:inline" title="当前登录账号">
            {user?.email}
          </span>
          <button
            type="button"
            onClick={() => void handleLogout()}
            className="rounded-[var(--radius)] px-2.5 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            退出
          </button>
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

/**
 * 本地数据的最新时点。取不到就不显示——宁可没有这枚标签，也不能显示一个假日期。
 * 值来自后端查询（`/api/v1/market/freshness`），M2 的日增量 ETL 接上后会自动前移。
 */
function useFreshness(): DataFreshness | null {
  const [freshness, setFreshness] = useState<DataFreshness | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .freshness()
      .then((data) => {
        if (alive) setFreshness(data);
      })
      .catch(() => undefined); // 数据未落盘（503）时静默：页头少一枚标签而已
    return () => {
      alive = false;
    };
  }, []);

  return freshness;
}

function freshnessTooltip(freshness: DataFreshness): string {
  const event = freshness.latest_event_available_at?.slice(0, 10);
  return [
    "本地行情快照",
    event ? `事件语料截至 ${event}` : null,
    "日增量 ETL 到位后随每次拉取自动前移（M2）",
  ]
    .filter(Boolean)
    .join(" · ");
}
