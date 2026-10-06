/**
 * 抓取 README 用截图：对话页与回测页。
 *
 * 前置：后端 8000、前端 3001 均已启动（见根 README「快速开始」）。
 * 用法：cd frontend && node scripts/capture-screenshots.mjs
 *
 * 两页都需要真实交互才有内容——对话页要发一条提问等流式结束，
 * 回测页要提交表单等图表渲染，故用 Playwright 驱动而非静态截图。
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

/** 截对话页会真的新建一条会话；跑完删掉自己造的那条，脚本才可反复重跑。 */
async function threadIds() {
  const res = await fetch(`${API}/api/v1/chat/threads?limit=100`);
  if (!res.ok) throw new Error(`会话列表返回 ${res.status}`);
  return new Set((await res.json()).map((t) => t.thread_id));
}

async function deleteThreadsNotIn(before) {
  const after = await threadIds();
  const created = [...after].filter((id) => !before.has(id));
  for (const id of created) {
    const res = await fetch(`${API}/api/v1/chat/threads/${id}`, { method: "DELETE" });
    if (!res.ok) console.warn(`⚠ 清理会话 ${id} 失败：${res.status}`);
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

async function captureChat(browser) {
  const before = await threadIds();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  await page.goto(BASE, { waitUntil: "networkidle" });

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
  await deleteThreadsNotIn(before);
}

async function captureBacktest(browser) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`${BASE}/backtest`, { waitUntil: "networkidle" });

  // 默认值即「事件驱动 + 600519 + 双模式对比」，一次点击直达 PIT 对比表
  await page.getByRole("button", { name: "运行" }).click();
  await page.getByRole("button", { name: "运行中…" }).waitFor({ timeout: SLOW });
  await waitForTextToVanish(page, "运行中…");
  // 图表入场动画约 1s + 主题色过渡 150ms
  await page.waitForTimeout(2500);

  await page.screenshot({ path: `${OUT_DIR}/backtest.png`, fullPage: true });
  console.log("✔ backtest.png");

  // PIT 对比表单独来一张——它是护城河的直接展示位，在整页长图里太小
  const pit = page
    .getByRole("heading", { name: "PIT 与非 PIT 的差异" })
    .locator("xpath=ancestor::section");
  if (await pit.count()) {
    await pit.scrollIntoViewIfNeeded();
    await page.waitForTimeout(600);
    await pit.screenshot({ path: `${OUT_DIR}/pit-comparison.png` });
    console.log("✔ pit-comparison.png");
  } else {
    console.warn("⚠ 未找到 PIT 对比区块，跳过局部截图");
  }
  return page;
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  try {
    await captureChat(browser);
    const backtest = await captureBacktest(browser);
    await backtest.close();
  } finally {
    await browser.close();
  }
  console.log(`\n输出目录：${OUT_DIR}`);
}

main().catch((error) => {
  console.error("截图失败：", error.message);
  process.exit(1);
});
