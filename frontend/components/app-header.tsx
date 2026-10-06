"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { ThemeToggle } from "@/components/theme-toggle";

/**
 * 全站页头：品牌 + 两页导航 + 当前用户 + 主题切换。导航在桌面必须单行（设计规范）。
 *
 * 只在受保护路由组内渲染，所以 `user` 必然非空（守卫已挡在前面）。
 */
export function AppHeader() {
  const { user, logout } = useAuth();
  const router = useRouter();

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

        <nav className="flex items-center gap-1 text-sm">
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
        </nav>

        <div className="ml-auto flex items-center gap-3">
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
