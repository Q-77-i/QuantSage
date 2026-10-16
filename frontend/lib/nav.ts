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
  // 第 10 项（2026-10-13 用户点头）。**M10c 时我写的「不进页头」被推翻了**，理由是它现在
  // 是两条路由的一级浏览面，而快捷入口行只在首页上、从 `/stocks/600519` 回不去。
  // 代价如实记：窄屏下导航自己横向滚（M5b 定的缓解手段，加第 5、6 项时都用它兜过）。
  { key: "stocks", href: "/stocks", label: "标的浏览" },
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
 * 只有 admin 看得见的一项（E2b）。**刻意不放进 `NAV_ITEMS`**：首页快捷入口「9 张 = 3×3」
 * 的不变量挂在 `NAV_ITEMS` 上（`nav.test.ts` 钉着），混进去会让那套对应关系失真。
 *
 * 隐藏入口**不是门**——非 admin 直接敲 `/admin` 会看到「需要管理员权限」（服务端 403）。
 */
export const ADMIN_NAV_ITEM: NavItem = { key: "admin", href: "/admin", label: "运维" };

/** 全部可达的导航项（含 admin 那一项）。**高亮判据**用它，展示只取子集：
 *  两处清单若分开写，admin 站在 `/admin` 上会一项都不亮。 */
const ALL_NAV_ITEMS: readonly NavItem[] = [...NAV_ITEMS, ADMIN_NAV_ITEM];

/** 页头该显示哪些项：普通人 10 项，admin 多一项。纯函数，带用例（顺序即展示顺序）。 */
export function navItemsFor(role: string | null | undefined): readonly NavItem[] {
  return role === "admin" ? ALL_NAV_ITEMS : NAV_ITEMS;
}

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
  for (const item of ALL_NAV_ITEMS) {
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


/**
 * 首页快捷入口。**顺序与 `NAV_ITEMS` 完全对应**（去掉「首页」自己——你就站在这儿），
 * 故正好 9 张 = 3×3。`nav.test.ts` 里有一条用例把这件事钉住。
 *
 * 与页头同序不是为了好看：两处顺序不一致时，用户建立的「第几项是什么」的位置记忆会互相打架。
 * 文案则两处不同——页头要**功能名**（短、能横扫），这里要「去哪儿能干什么」。
 */
export interface QuickEntry {
  href: string;
  label: string;
  hint: string;
}

export const QUICK_ENTRIES: readonly QuickEntry[] = [
  { href: "/stocks", label: "标的浏览", hint: "全市场列表与个股页：K 线、事件、加自选" },
  { href: "/chat", label: "对话", hint: "就一个标的或一条新闻追问，带 PIT 语义的检索" },
  { href: "/backtest", label: "回测", hint: "跑一段历史，看 PIT / 非 PIT 两口径差多少" },
  { href: "/strategies", label: "策略", hint: "写策略，运行前先静态拦下未来函数" },
  { href: "/optimize", label: "优化", hint: "参数网格扫描、批量回测与过拟合检验" },
  { href: "/factor", label: "因子", hint: "检验新闻信号与价格动量有没有截面预测力" },
  { href: "/paper", label: "模拟盘", hint: "纸上交易，每笔决策先过审批闸门再成交" },
  { href: "/research", label: "深度研究", hint: "多 Agent 并行取证 → 多空辩论 → 出研报" },
  { href: "/space", label: "个人空间", hint: "自选股、会话、回测与研报记录" },
] as const;
