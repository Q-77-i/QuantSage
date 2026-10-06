# QuantSage 前端

Next.js 15（App Router）+ TypeScript + Tailwind + shadcn/ui。项目总览、架构与完整启动步骤见[根 README](../README.md)。

```bash
pnpm install
pnpm dev          # http://127.0.0.1:3001（3000 被 Langfuse 自托管 UI 占用）
pnpm test         # Vitest，只测纯函数
pnpm typecheck    # tsc --noEmit
pnpm lint         # eslint
pnpm build        # 构建验证前请先停掉 dev server，两者共用 .next 目录
```

后端地址由 `NEXT_PUBLIC_API_BASE` 指定，默认 `http://127.0.0.1:8000`。

## 抓取 README 截图

`pnpm screenshots` 用 Playwright 驱动真实交互，产出 `docs/images/` 下三张图（对话页 / 回测页 / PIT 对比区）。首次需要下载浏览器：

```bash
pnpm exec playwright install chromium
```

前置：后端 8000 与前端 3001 已启动。**用生产模式截**（`pnpm build && pnpm start`）——`next dev` 会在页面上浮一个开发模式角标。脚本会自己清理它在对话页新建的那条会话，可反复重跑。
