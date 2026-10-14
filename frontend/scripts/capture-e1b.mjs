/**
 * 网格/批量「断线不丢」的走查与证据脚本（P3-E1b）。
 *
 * 前置：后端 8000 与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-e1b.mjs
 *
 * 三件事逐条断言——**这三条正是 M5b 时代做不到的**（那时「刷新等于重跑」）：
 *   ① 提交后**刷新页面**：`?task=` 把订阅接回来，结果照常出现；
 *   ② 跑着的时候点「停止」：协作式中止、界面说「未落库」、**记录数不变**；
 *   ③ 正常跑完：落库、记录数 +1（与 ①② 对照）。
 */

import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EVIDENCE = resolve(ROOT, "logs/e1b");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "e1b-ui@example.com";
const PASSWORD = "e1b-ui-pass";

const RESULTS = [];

function check(label, ok, detail = "") {
  RESULTS.push({ label, ok });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

async function signIn(context) {
  const register = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (register.ok()) return;
  if (register.status() !== 409) throw new Error(`注册失败 ${register.status()}`);
  const login = await context.request.post(`${API}/api/v1/auth/login`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);
}

async function json(context, path) {
  const response = await context.request.get(`${API}${path}`);
  if (!response.ok()) throw new Error(`${path} → ${response.status()}`);
  return response.json();
}

/**
 * 填一个够大的网格：**单轴 8 个取值**（8 格全期 ≈ 4–8s，够点停止、也够刷新一次）。
 *
 * 不用两轴：表单默认只有一条轴，加第二条要先点「加一条轴」——脚本没必要为了凑格数
 * 多踩一次界面状态。
 */
async function fillBigGrid(page) {
  // 轴参数默认是空的，得先选（与 capture-m5b.mjs 同一套控件 id 定位）
  await page.locator("#op-axis-0").selectOption("fast");
  // 取值都要小于基座的 slow=20：`fast=20 / slow=20` 那一格会被整单拒绝（M5b 的口径）
  await page.locator("#op-axis-0-values").fill("3, 5, 8, 10, 12, 14, 16, 18");
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });

  try {
    await signIn(context);
    const page = await context.newPage();
    const runsBefore = (await json(context, "/api/v1/optimize/runs")).length;

    // ── ① 提交后刷新：订阅接回来，结果照常 ─────────────────────
    await page.goto(`${FRONTEND}/optimize`, { waitUntil: "domcontentloaded" });
    await page.locator("#op-symbol").waitFor({ timeout: 30_000 });
    await fillBigGrid(page);
    await page.getByRole("button", { name: "运行" }).click();

    // 地址栏带上任务号（刷新是这里最常见的「断线」）
    await page.waitForURL(/\/optimize\?mode=grid&task=/, { timeout: 30_000 });
    const taskUrl = page.url();
    check("提交后地址栏带 `?task=`", /task=[0-9a-f-]{36}/.test(taskUrl));

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("text=Deflated Sharpe", { timeout: 180_000 });
    check("**刷新后**结果照常出现（订阅自动接回）", true);
    await page.screenshot({ path: `${EVIDENCE}/reloaded-finished.png` });

    const runsAfterOne = (await json(context, "/api/v1/optimize/runs")).length;
    check("跑完照常落库（记录数 +1）", runsAfterOne === runsBefore + 1, `${runsBefore} → ${runsAfterOne}`);

    // ── ② 跑着的时候「停止」：协作式中止，不落库 ────────────────
    await page.goto(`${FRONTEND}/optimize`, { waitUntil: "domcontentloaded" });
    await page.locator("#op-symbol").waitFor({ timeout: 30_000 });
    await fillBigGrid(page);
    await page.getByRole("button", { name: "运行" }).click();
    await page.waitForURL(/task=/, { timeout: 30_000 });
    await page.waitForTimeout(1200); // 让它真跑起来几格
    await page.getByRole("button", { name: "停止" }).click();

    await page.waitForSelector("text=未落库", { timeout: 30_000 });
    check("停止后界面说明「未落库」", true);
    await page.screenshot({ path: `${EVIDENCE}/stopped.png` });

    // 等后端把状态收干净再数记录
    await page.waitForTimeout(2000);
    const runsAfterStop = (await json(context, "/api/v1/optimize/runs")).length;
    check(
      "中止的结果**没有落库**（记录数不变）",
      runsAfterStop === runsAfterOne,
      `${runsAfterOne} → ${runsAfterStop}`,
    );
    await page.close();
  } finally {
    await context.close();
    await browser.close();
  }

  const failed = RESULTS.filter((item) => !item.ok);
  console.log(`\n断言 ${RESULTS.length - failed.length}/${RESULTS.length} 通过`);
  if (failed.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error("失败：", error.message);
  process.exit(1);
});
