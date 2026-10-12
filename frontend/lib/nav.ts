/**
 * 页头导航的**纯函数**：项目清单与「当前在哪一项」的判据。
 *
 * 为什么要单独一层：**「当前项」的判据是容易写错又不容易发现的那类逻辑**——
 * `/` 用前缀匹配会让所有页面都点亮「对话」；`/backtest` 用相等匹配则带着
 * `?run=<id>` 深链进来就不亮。抽成纯函数 + 用例钉死，顺便让页头只负责画。
 */

export interface NavItem {
  key: string;
  href: string;
  label: string;
}

/**
 * 页头导航（顺序即展示顺序）。新增一项只改这里。
 *
 * ⚠ **M10a 起 `/` 是研究首页，对话挪到 `/chat`**——改动落在这一张表里，别处不用动。
 * 页面搬迁时唯一要一并检查的是：还有谁把 `/` 当成「对话」写死了（`ChatPage` 的
 * `ThreadUrlSync` 已改）。`register` 与 `safeNextPath` 的默认值仍指向 `/`，
 * 那是**对的**——注册后落到研究首页正是这次改动的意图。
 */
export const NAV_ITEMS: readonly NavItem[] = [
  { key: "home", href: "/", label: "首页" },
  { key: "chat", href: "/chat", label: "对话" },
  { key: "backtest", href: "/backtest", label: "回测" },
  { key: "strategies", href: "/strategies", label: "策略" },
  { key: "optimize", href: "/optimize", label: "优化" },
  { key: "factor", href: "/factor", label: "因子" },
  { key: "paper", href: "/paper", label: "模拟盘" },
  { key: "research", href: "/research", label: "深度研究" },
  { key: "space", href: "/space", label: "个人空间" },
] as const;

/**
 * 不在导航里、但**属于**某一项的路径（点进来时高亮它所属的模块）。
 *
 * ⚠ 这里曾有一条 `"/research": "/paper"`：M7 时研报页是模拟盘账户的产物，归过去说得通。
 * M8 之后 `/research` 已经是**深度研究的一级入口**（有自己的导航项），那条别名就过期了
 * ——它让深度研究的页面点亮「模拟盘」（用户当场发现）。**新增一级功能区时回来清一遍别名**。
 */
const ALIASES: Record<string, string> = {};

/** 路径 → 当前项（没有匹配到就返回 `null`，例如公开分享页不该点亮任何一项）。 */
export function activeHref(pathname: string | null): string | null {
  if (!pathname) return null;
  const path = pathname.replace(/\/+$/, "") || "/";
  for (const item of NAV_ITEMS) {
    if (item.href === "/") {
      // 根路径只有**精确**相等才算当前项——前缀匹配会让全站点亮「首页」
      if (path === "/") return "/";
      continue;
    }
    if (path === item.href || path.startsWith(`${item.href}/`)) return item.href;
  }
  for (const [prefix, href] of Object.entries(ALIASES)) {
    if (path === prefix || path.startsWith(`${prefix}/`)) return href;
  }
  return null;
}

/** 某一项是不是当前项（页头据此加 `aria-current` 与高亮样式）。 */
export function isActive(pathname: string | null, href: string): boolean {
  return activeHref(pathname) === href;
}
