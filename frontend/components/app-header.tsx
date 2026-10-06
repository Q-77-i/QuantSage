import Link from "next/link";

import { ThemeToggle } from "@/components/theme-toggle";

/** 全站页头：品牌 + 两页导航 + 主题切换。导航在桌面必须单行（设计规范）。 */
export function AppHeader() {
  return (
    <header className="border-b border-border">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-4">
        <Link href="/" className="font-heading text-lg tracking-tight">
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

        <div className="ml-auto">
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
