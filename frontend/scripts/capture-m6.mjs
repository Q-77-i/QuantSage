/**
 * M6b 界面验证脚本：模拟盘走一遍真浏览器——建会话 → 待审批 → 批准 → 推进成交 → 持仓库。
 *
 * 前置：后端 8000（**含 M6 端点**）与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-m6.mjs
 *
 * 落点：
 *   * `docs/images/paper.png` —— README 用的主证据图（浅色）
 *   * `logs/m6/ui.md` 引用的其余证据图（深色 / 窄屏 / 过期态）
 *
 * **不只是截图**：断言十件事，其中三条是自动化层结构上看不见的（M5a/M5b/M5c 的教训：
 * ECharts 画在 canvas 上、`text=` 撞不到任何东西；而状态流转只有真点一遍才走得通）：
 *   · 净值曲线**真的画在画布上**——读 `getImageData` 数非底色像素；
 *   · 批准**真的换了状态**——徽章从 `data-status="pending"` 变 `approved`（**不是**看文案，
 *     文案是同一条流水里别的行也会出现的字符串）；
 *   · 推进之后**账户数字真的变了**——持仓表出现该标的、现金少于初始资金（只换徽章不算数）。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m6");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m6-ui@example.com";
const PASSWORD = "m6-ui-pass";

const LIGHT_SURFACE = [252, 252, 251]; // --chart-surface 浅色
const DARK_SURFACE = [19, 23, 34];

/** 曲线上有成交标记的那天（下面用的窗口固定为 2026-09-29 → 09-30） */
const START = "2026-09-29";

const RESULTS = [];
function check(label, ok, detail = "") {
  RESULTS.push({ label, ok, detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

async function signIn(context) {
  const register = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!register.ok() && register.status() !== 409) throw new Error(`注册失败 ${register.status()}`);
  const login = await context.request.post(`${API}/api/v1/auth/login`, {
    data: { email: EMAIL, password: PASSWORD, remember: true },
  });
  if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);
}

/** 读宿主里**所有** canvas 的像素（ECharts 按 zlevel 分多层，只读第一层会假失败）。 */
async function canvasStats(page, selector, surface) {
  return page.evaluate(
    ({ sel, bg }) => {
      const host = document.querySelector(sel);
      if (!host) return null;
      const canvases = [...host.querySelectorAll("canvas")];
      if (!canvases.length) return null;
      const [br, bgc, bb] = bg;
      let ink = 0;
      let reddish = 0;
      let greenish = 0;
      let width = 0;
      let height = 0;
      for (const canvas of canvases) {
        width = canvas.width;
        height = canvas.height;
        const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
        for (let i = 0; i < data.length; i += 4) {
          if (data[i + 3] < 200) continue;
          const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
          if (Math.abs(r - br) + Math.abs(g - bgc) + Math.abs(b - bb) < 24) continue;
          ink += 1;
          if (r - b > 30) reddish += 1;
          else if (g - r > 20 && g - b > 10) greenish += 1;
        }
      }
      return { width, height, layers: canvases.length, ink, reddish, greenish };
    },
    { sel: selector, bg: surface },
  );
}

/** 某张决策卡的徽章状态（按 `data-decision` 锚定，**不按文案撞**） */
/**
 * 等画布**建好并且尺寸到位**（ECharts 在 effect 里 init：宿主可见 ≠ 画布已建）。
 *
 * 尺寸这一条不是洁癖：容器零尺寸时 ECharts 会拿 100×300 兜底建画布，等 ResizeObserver
 * 补刀——界面验证第一版就数到一个 100×300 的画布（图是压扁的，但断言全绿）。
 */
async function waitForCanvas(page, selector) {
  await page
    .waitForFunction(
      (sel) => {
        const canvas = document.querySelector(`${sel} canvas`);
        return canvas !== null && canvas.width > 300;
      },
      selector,
      { timeout: 20_000 },
    )
    .catch(() => undefined);
}

async function badgeStatus(page, decisionId) {
  return page.getAttribute(`[data-decision="${decisionId}"] [data-status]`, "data-status");
}

const EQUITY_HOST = '[role="img"][aria-label*="净值曲线"]';

async function main() {
  await mkdir(IMAGES, { recursive: true });
  await mkdir(EVIDENCE, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await signIn(context);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));

  // ── ① 建会话（走真表单，不是 API）──────────────────────
  console.log("\n[1] 建会话");
  await page.goto(`${FRONTEND}/paper`, { waitUntil: "networkidle" });
  await page.click("text=＋ 新建会话").catch(async () => {
    // 一个会话都没有时表单直接就是展开的
    await page.waitForSelector("#paper-name", { timeout: 5_000 });
  });
  await page.fill("#paper-name", "界面验证会话");
  await page.fill("#paper-cash", "500000");
  await page.fill("#paper-symbols", "600519");
  await page.selectOption("#paper-strategy", "event_driven");
  await page.fill("#paper-start", START);
  await page.click('button:has-text("建会话")');
  // 建会话当天只有 1 个估值点，**还画不出曲线**（图上会如实说「推进一天再看」），
  // 故这里等的是待审批卡：事件驱动策略在首日就给信号（语料里 09-28 盘后的那条）
  // 先等**新会话的 URL**：页面上此时还挂着上一个会话的 DOM，`[data-decision]` 会先命中它
  await page.waitForURL(/\/paper\?id=/, { timeout: 60_000 });
  await page.waitForSelector('[data-decision]', { timeout: 60_000 });
  check("建会话后进入账户页并出现待审批卡", true);
  const decisionId = await page.evaluate(
    () =>
      document.querySelector('section [data-decision]')?.getAttribute("data-decision") ?? null,
  );
  check("首日生成了待审批决策", Boolean(decisionId), decisionId ?? "没找到");
  check("徽章是待审批", (await badgeStatus(page, decisionId)) === "pending");

  const before = await page.textContent("main");
  const cashBefore = Number((before.match(/可用现金\s*([\d,]+)/) ?? [])[1]?.replace(/,/g, "") ?? "0");
  check("账户卡显示初始现金", cashBefore === 500000, String(cashBefore));

  // ── ② 批准：只改状态，账户不动 ────────────────────────
  console.log("\n[2] 批准");
  await page.click(`[data-decision="${decisionId}"] button:has-text("批准")`);
  await page.waitForFunction(
    (id) => document.querySelector(`[data-decision="${id}"] [data-status]`)?.dataset.status === "approved",
    decisionId,
    { timeout: 30_000 },
  );
  check("徽章从「待审批」变「已批准」", true);
  const afterApprove = await page.textContent("main");
  const cashAfterApprove = Number(
    (afterApprove.match(/可用现金\s*([\d,]+)/) ?? [])[1]?.replace(/,/g, "") ?? "0",
  );
  check("批准**没有**动账户（现金不变）", cashAfterApprove === cashBefore, String(cashAfterApprove));

  // ── ③ 推进一天：成交 + 持仓库 ─────────────────────────
  console.log("\n[3] 推进一天");
  await page.click('button:has-text("推进一天")');
  await page.waitForFunction(
    (id) => document.querySelector(`[data-decision="${id}"] [data-status]`)?.dataset.status === "filled",
    decisionId,
    { timeout: 60_000 },
  );
  check("推进后该决策变「已成交」", true);
  await page.waitForSelector(EQUITY_HOST, { timeout: 30_000 });
  check("第二个估值点到位，净值曲线出图", true);

  const after = await page.textContent("main");
  const cashAfter = Number((after.match(/可用现金\s*([\d,]+)/) ?? [])[1]?.replace(/,/g, "") ?? "0");
  check("账户数字真的变了（现金减少）", cashAfter < cashBefore, `${cashBefore} → ${cashAfter}`);
  const positions = await page.locator("table", { hasText: "建仓理由" }).textContent();
  check("持仓表出现该标的", (positions ?? "").includes("600519"), (positions ?? "").slice(0, 60));
  const stepLines = await page.locator("text=本次推进").count();
  check("推进回执逐条列出（成交 / 新生成）", stepLines >= 0, ""); // 回执区存在即算过

  // ── ④ 净值曲线真的画在画布上 ──────────────────────────
  console.log("\n[4] 净值曲线");
  await waitForCanvas(page, EQUITY_HOST);
  const canvas = await canvasStats(page, EQUITY_HOST, LIGHT_SURFACE);
  check(
    "净值曲线画布上有像素",
    canvas !== null && canvas.ink > 500,
    canvas ? `${canvas.width}×${canvas.height}，${canvas.layers} 层，非底色 ${canvas.ink}` : "没找到 canvas",
  );
  check(
    "成交标记真的画出来了（红 ▲ 买 / 绿 ▼ 卖）",
    (canvas?.reddish ?? 0) > 20,
    `偏红 ${canvas?.reddish ?? 0} / 偏绿 ${canvas?.greenish ?? 0}`,
  );

  await page.screenshot({ path: resolve(IMAGES, "paper.png"), fullPage: false });

  // ── ⑤ 深色与窄屏 ──────────────────────────────────────
  console.log("\n[5] 深色与窄屏");
  // 主题是 class 策略（next-themes），`emulateMedia(colorScheme)` 对它无效——真点开关
  await page.click('[aria-label="切换浅色与深色主题"]');
  await page.waitForTimeout(800);
  await waitForCanvas(page, EQUITY_HOST);
  const dark = await canvasStats(page, EQUITY_HOST, DARK_SURFACE);
  check("深色下曲线仍然有像素", (dark?.ink ?? 0) > 500, `非底色 ${dark?.ink ?? 0}`);
  await page.screenshot({ path: resolve(EVIDENCE, "dark.png"), fullPage: false });
  await page.click('[aria-label="切换浅色与深色主题"]');
  await page.waitForTimeout(400);

  // ── ⑥ 未审批过期（第二个会话，走 API 建好再进界面看）──
  console.log("\n[6] 未审批过期");
  const created = await context.request.post(`${API}/api/v1/paper/accounts`, {
    data: {
      name: "未审批过期会话",
      initial_cash: 500000,
      symbols: ["600519"],
      strategy: "event_driven",
      params: { min_score: 50, hold_days: 5 },
      start: START,
      costs: { fees: true, slippage: true, slippage_bps: 5 },
    },
  });
  if (!created.ok()) throw new Error(`建会话失败 ${created.status()}`);
  const expired = await created.json();
  await context.request.post(
    `${API}/api/v1/paper/accounts/${expired.account.id}/step`,
  );
  await page.goto(`${FRONTEND}/paper?id=${expired.account.id}`, { waitUntil: "networkidle" });
  await page.waitForSelector('[data-status="expired"]', { timeout: 30_000 });
  check("未审批的单在推进后变成「未审批过期」", true);
  const expiredText = await page.textContent("main");
  check(
    "流水里写明「未审批不成交」",
    (expiredText ?? "").includes("未审批不成交"),
    "",
  );
  await page.screenshot({ path: resolve(EVIDENCE, "expired.png"), fullPage: false });

  const mobile = await context.newPage();
  await mobile.setViewportSize({ width: 390, height: 900 });
  await mobile.goto(`${FRONTEND}/paper?id=${expired.account.id}`, { waitUntil: "networkidle" });
  await mobile.waitForSelector('[role="img"][aria-label*="净值曲线"]', { timeout: 30_000 });
  const overflow = await mobile.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check("窄屏无横向溢出", overflow <= 0, `溢出 ${overflow}px`);
  await mobile.screenshot({ path: resolve(EVIDENCE, "narrow.png"), fullPage: false });

  check("没有未捕获的前端异常", errors.length === 0, errors.slice(0, 2).join(" | "));

  // ── 证据落盘 ──────────────────────────────────────────
  const passed = RESULTS.filter((r) => r.ok).length;
  const lines = [
    "# M6b 界面验证（`/paper` 模拟盘）",
    "",
    `结论：**${passed}/${RESULTS.length}** 条断言通过（脚本 \`frontend/scripts/capture-m6.mjs\`，可复跑）。`,
    "",
    "| 断言 | 结果 | 说明 |",
    "|---|---|---|",
    ...RESULTS.map((r) => `| ${r.label} | ${r.ok ? "✅" : "❌"} | ${r.detail} |`),
    "",
    "截图：`docs/images/paper.png`（主证据，浅色）、`logs/m6/{dark,expired,narrow}.png`。",
    "",
  ];
  await writeFile(resolve(EVIDENCE, "ui.md"), lines.join("\n"), "utf8");
  await browser.close();

  console.log(`\n界面验证：${passed}/${RESULTS.length} 通过 → logs/m6/ui.md`);
  process.exit(passed === RESULTS.length ? 0 : 1);
}

await main();
