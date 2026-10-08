/**
 * 策略工作台的截图与验收脚本（M4c）。
 *
 * 前置：后端 8000（**含 M4c 的端点**）与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-strategies.mjs
 *
 * 产四张图，两处落点：
 *   * `docs/images/strategies.png`       —— 工作台（浅色，README 用）
 *   * `docs/images/strategy-markers.png` —— 前视检查的标注与拦截（验收 ③）
 *   * `logs/m4c/workbench-dark.png`      —— 深色主题（证据，不入库）
 *   * `logs/m4c/workbench-narrow.png`    —— 768px 单列塌陷（证据，不入库）
 *
 * **不只是截图**：脚本同时断言三件事——编辑器出现 error 波浪线、面板报出「会拦住运行」、
 * 点「保存并运行」被 422 闸门拦下。跑完把自己造的策略删掉（账号留着，撞号就登录）。
 */

import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m4c");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m4c-ui@example.com";
const PASSWORD = "m4c-ui-pass";
/** 只删自己造的策略：带这个前缀的才是脚本的 */
const PREFIX = "示例·";

/** 含未来索引与数据绕行各一条：两档 error 各给一行，标注与面板都能看见 */
const BUGGY_CODE = `import duckdb

PARAMS = {"window": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "均线窗口"}}
USES_EVENTS = False


def on_bar(ctx, p):
    closes = [bar.close for bar in ctx.history]
    if len(closes) < p["window"] + 1:
        return []
    return [] if ctx.history[ctx.index + 1] else []
`;

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

async function json(context, path, init) {
  const response = await context.request.fetch(`${API}${path}`, init);
  if (!response.ok()) throw new Error(`${path} → ${response.status()}：${await response.text()}`);
  return response.json();
}

/** 清掉上一轮脚本造的策略（名字前缀是记号，不动用户自己的） */
async function cleanLeftovers(context) {
  const existing = await json(context, "/api/v1/strategies");
  for (const strategy of existing.filter((item) => item.name.startsWith(PREFIX))) {
    await context.request.delete(`${API}/api/v1/strategies/${strategy.id}`);
  }
}

async function main() {
  await mkdir(IMAGES, { recursive: true });
  await mkdir(EVIDENCE, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const created = [];

  try {
    await signIn(context);
    await cleanLeftovers(context);

    const templates = await json(context, "/api/v1/strategies/templates");
    const maCross = templates.find((item) => item.key === "ma_cross");

    const clean = await json(context, "/api/v1/strategies", {
      method: "POST",
      data: { name: `${PREFIX}双均线`, code: maCross.source },
    });
    const buggy = await json(context, "/api/v1/strategies", {
      method: "POST",
      data: { name: `${PREFIX}未来函数`, code: BUGGY_CODE },
    });
    created.push(clean.id, buggy.id);

    const page = await context.newPage();

    // ── ① 前视检查：标注 + 面板 + 422 闸门（验收 ③）──────────────────
    await page.goto(`${FRONTEND}/strategies?id=${buggy.id}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".monaco-editor", { timeout: 60_000 });
    await page.waitForSelector(".squiggly-error", { timeout: 30_000 });

    const squiggles = await page.locator(".squiggly-error").count();
    check("编辑器出现 error 波浪线", squiggles >= 1, `${squiggles} 处`);

    const panel = page.getByText(/个 error 会拦住运行/);
    await panel.waitFor({ timeout: 15_000 });
    check("面板报出「会拦住运行」", true, (await panel.first().textContent())?.trim());

    await page.getByRole("button", { name: "保存并运行" }).click();
    const blocked = page.getByText("策略没通过前视静态检查，已拦在运行前。");
    await blocked.waitFor({ timeout: 30_000 });
    check("点「保存并运行」被 422 闸门拦下", true);

    await page.screenshot({ path: `${IMAGES}/strategy-markers.png`, fullPage: true });
    console.log("  ✔ docs/images/strategy-markers.png");

    // ── ② 干净策略：参数表单 + 好走的工作台（浅色 / 深色 / 窄屏）─────
    await page.getByRole("button", { name: `${PREFIX}双均线`, exact: true }).click();
    await page.waitForSelector("text=未发现问题。", { timeout: 30_000 });
    const paramInputs = await page.locator('input[type="number"]').count();
    check("参数表单由 PARAMS 生成", paramInputs >= 2, `${paramInputs} 个数字输入`);

    await page.screenshot({ path: `${IMAGES}/strategies.png`, fullPage: true });
    console.log("  ✔ docs/images/strategies.png");

    await page.getByRole("button", { name: "深色" }).click();
    await page.waitForTimeout(400); // 150ms 颜色过渡 + 编辑器主题切换
    // 证据图用**视口截图**：`fullPage` 会把 sticky 的运行条画在长图中间，看着像压住了内容
    await page.screenshot({ path: `${EVIDENCE}/workbench-dark.png` });
    console.log("  ✔ logs/m4c/workbench-dark.png");

    await page.getByRole("button", { name: "浅色" }).click();
    await page.setViewportSize({ width: 768, height: 1000 });
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${EVIDENCE}/workbench-narrow-top.png` });
    // 再滚到底：证明运行条之外的每一段都能完整看到（底部留白是为此加的）
    await page.evaluate(() => window.scrollTo({ top: document.body.scrollHeight }));
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${EVIDENCE}/workbench-narrow-bottom.png` });
    console.log("  ✔ logs/m4c/workbench-narrow-{top,bottom}.png");
    await page.close();
  } finally {
    for (const id of created) {
      await context.request.delete(`${API}/api/v1/strategies/${id}`).catch(() => undefined);
    }
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
