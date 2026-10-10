"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { ThemeToggle } from "@/components/theme-toggle";
import { api } from "@/lib/api";
import { isActive, NAV_ITEMS } from "@/lib/nav";
import type { DataFreshness } from "@/lib/types";

/**
 * 全站页头：品牌 + 导航 + 当前用户 + 主题切换。导航在桌面必须单行（设计规范）。
 *
 * **当前项要高亮**（用户反馈：切换模块后分不清自己在哪）：品牌色文字 + 品牌浅底 + 中等字重，
 * 与中性的 hover 面（`bg-muted`）明确区分；判据在 `lib/nav.ts`（纯函数，带用例）。
 *
 * 只在受保护路由组内渲染，所以 `user` 必然非空（守卫已挡在前面）。
 */
export function AppHeader() {
  const { user, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const freshness = useFreshness();

  async function handleLogout() {
    await logout();
    router.replace("/login");
  }

  return (
    <header className="border-b border-border">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-4">
        <Link href="/" className="shrink-0 font-heading text-lg font-semibold tracking-tight">
          知策 <span className="text-muted-foreground">QuantSage</span>
        </Link>

        {/* 导航：桌面单行（设计规范）。窄屏放不下时**自己横向滚**——
            加第 5 项「优化」后 390px 下页头会横溢出 140px（第 6 项「因子」沿用同一条自滚策略）（界面验证逮到），
            而挤走的若是品牌或用户区，损失比让导航滚一下大得多 */}
        <nav className="flex min-w-0 items-center gap-1 overflow-x-auto text-sm whitespace-nowrap">
          {NAV_ITEMS.map((item) => {
            const current = isActive(pathname, item.href);
            return (
              <Link
                key={item.key}
                href={item.href}
                // `aria-current="page"` 是无障碍里的「你在这儿」；`data-active` 给界面验证用
                aria-current={current ? "page" : undefined}
                data-nav={item.key}
                data-active={current ? "true" : "false"}
                className={
                  current
                    ? "rounded-[var(--radius)] bg-brand/12 px-2.5 py-1 font-medium text-brand"
                    : "rounded-[var(--radius)] px-2.5 py-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                }
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex shrink-0 items-center gap-3">
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
