/**
 * 抓取 README 用截图：对话页、回测页与 PIT 对比区块。
 *
 * 前置：后端 8000、前端 3001 均已启动（见根 README「快速开始」）。
 * 用法：cd frontend && node scripts/capture-screenshots.mjs
 *
 * 两页都需要真实交互才有内容——对话页要发一条提问等流式结束，
 * 回测页要提交表单等图表渲染，故用 Playwright 驱动而非静态截图。
 *
 * 自 M1c 起对话相关端点与页面均需登录，故脚本自带一个一次性账号；
 * 自 M10a 起 `/` 是研究首页，对话页在 `/chat`。
 */

import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const OUT_DIR = resolve(ROOT, "docs/images");
const BASE = process.env.APP_BASE ?? "http://127.0.0.1:3001";

/** 后端在跑真实模型，对话与回测都慢，超时给足。 */
const SLOW = 180_000;
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";
const EMAIL = "shot-ui@example.com";
const PASSWORD = "shot-ui-pass";

/** 截对话页会真的新建一条会话；跑完删掉自己造的那条，脚本才可反复重跑。 */
async function threadIds(context) {
  const res = await context.request.get(`${API}/api/v1/chat/threads?limit=100`);
  if (!res.ok()) throw new Error(`会话列表返回 ${res.status()}`);
  return new Set((await res.json()).map((t) => t.thread_id));
}

async function deleteThreadsNotIn(context, before) {
  const after = await threadIds(context);
  const created = [...after].filter((id) => !before.has(id));
  for (const id of created) {
    const res = await context.request.delete(`${API}/api/v1/chat/threads/${id}`);
    if (!res.ok()) console.warn(`⚠ 清理会话 ${id} 失败：${res.status()}`);
  }
  if (created.length) console.log(`（已清理本次新建的 ${created.length} 条会话）`);
}

async function waitForTextToVanish(page, text, timeout = SLOW) {
  await page.waitForFunction(
    (label) => ![...document.querySelectorAll("button")].some((b) => b.textContent.trim() === label),
    text,
    { timeout },
  );
}

async function captureChat(context) {
  const before = await threadIds(context);
  const page = await context.newPage();
  // M10a 起 `/` 是研究首页，对话页迁到 /chat——走错页会填不到 #composer
  await page.goto(`${BASE}/chat`, { waitUntil: "networkidle" });

  await page.fill("#composer", "茅台最近行情如何？有哪些值得注意的公告？");
  await page.getByRole("button", { name: "发送" }).click();

  // 流式期间按钮变「停止」；它消失即本轮结束
  await page.getByRole("button", { name: "停止" }).waitFor({ timeout: SLOW });
  await waitForTextToVanish(page, "停止");
  await page.waitForTimeout(1200); // 等 markdown 与引用块渲染稳定

  // 消息列表（main 内的滚动容器）默认贴底；拉回顶部才能看到提问与工具调用步骤。
  // 必须限定在 main 内——侧栏也是 .overflow-y-auto，裸选择器会命中它。
  await page.evaluate(() => {
    const box = document.querySelector("main .overflow-y-auto");
    if (box) box.scrollTop = 0;
  });
  await page.waitForTimeout(400);

  await page.screenshot({ path: `${OUT_DIR}/chat.png` });
  console.log("✔ chat.png");
  await page.close();
  await deleteThreadsNotIn(context, before);
}

/** 点「运行」并等到跑完——图表入场动画约 1s + 主题色过渡 150ms。 */
async function runBacktest(page) {
  await page.getByRole("button", { name: "运行" }).click();
  await page.getByRole("button", { name: "运行中…" }).waitFor({ timeout: SLOW });
  await waitForTextToVanish(page, "运行中…");
  await page.waitForTimeout(2500);
}

async function captureBacktest(context) {
  const page = await context.newPage();
  await page.goto(`${BASE}/backtest`, { waitUntil: "networkidle" });

  // 默认值即「事件驱动 + 600519 + 双模式对比」，一次点击直达 PIT 对比表
  await runBacktest(page);
  await page.screenshot({ path: `${OUT_DIR}/backtest.png`, fullPage: true });
  console.log("✔ backtest.png");

  // PIT 对比表单独来一张——它是护城河的直接展示位，在整页长图里太小。
  //
  // ⚠ **这张刻意用 `min_score=60`，不是页面默认的 50**。默认门槛下这窗口里唯一
  // 一条跨线事件（news:854409：事发 07-28 11:09、平台 07-28 20:36 才给出）会被持有期
  // 吞掉——当天另有一条设卡更早的 news:854503 先触发了买入，于是两口径差 **0.00pp**。
  // 一张看不出闸门作用的图放在「护城河」节等于自证无效（2026-10-13 用户指出）。
  // 抬到 60 后 854503 被滤掉、854409 成为触发者：PIT 07-30 入场 vs 非 PIT 07-29，
  // 差 **+0.52pp**。README 的图注会写明这两档的关系，避免读成「PIT 总能拦出差值」。
  await page.fill("#bt-min_score", "60");
  await runBacktest(page);

  const pit = page
    .getByRole("heading", { name: "PIT 与非 PIT 的差异" })
    .locator("xpath=ancestor::section");
  if (await pit.count()) {
    await pit.scrollIntoViewIfNeeded();
    await page.waitForTimeout(600);
    await pit.screenshot({ path: `${OUT_DIR}/pit-comparison.png` });
    console.log("✔ pit-comparison.png（min_score=60）");
  } else {
    console.warn("⚠ 未找到 PIT 对比区块，跳过局部截图");
  }
  return page;
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 1000 },
      locale: "zh-CN",
    });
    const register = await context.request.post(`${API}/api/v1/auth/register`, {
      data: { email: EMAIL, password: PASSWORD },
    });
    if (!register.ok() && register.status() !== 409) {
      throw new Error(`注册失败 ${register.status()}`);
    }
    const login = await context.request.post(`${API}/api/v1/auth/login`, {
      data: { email: EMAIL, password: PASSWORD, remember: true },
    });
    if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);

    await captureChat(context);
    const backtest = await captureBacktest(context);
    await backtest.close();
    await context.close();
  } finally {
    await browser.close();
  }
  console.log(`\n输出目录：${OUT_DIR}`);
}

main().catch((error) => {
  console.error("截图失败：", error.message);
  process.exit(1);
});
