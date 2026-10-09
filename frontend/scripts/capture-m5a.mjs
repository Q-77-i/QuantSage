/**
 * M5a 界面验证脚本：基准叠加（第三条线 + 口径小字）与自选股按名称搜。
 *
 * 前置：后端 8000（**含 M5a 端点**）与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-m5a.mjs
 *
 * 落点：
 *   * `docs/images/backtest-benchmark.png` —— 三条线与口径标注（浅色，README 用）
 *   * `logs/m5a/ui.md` 引用的四张证据图（浅色 / 深色 / 窄屏 / 候选下拉）
 *
 * **不只是截图**：断言四件事——图例出现三项、口径小字可见、名称候选出得来、
 * 键盘选中后代码落进输入框。这些正是自动化层结构上看不见的一层
 * （M4c 的教训：CORS 预检、路由时序、事件语义、sticky 遮挡都只有真浏览器抓得到）。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m5a");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m5a-ui@example.com";
const PASSWORD = "m5a-ui-pass";

const RESULTS = [];
function check(label, ok, detail = "") {
  RESULTS.push({ label, ok, detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

async function signIn(context) {
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
}

/** 跑一次回测并把报告页打开（走的是真实端点与真实引擎）。 */
async function openBacktest(page) {
  await page.goto(`${FRONTEND}/backtest`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: /运行/ }).first().click();
  await page.waitForSelector("text=净值曲线", { timeout: 30_000 });
  // 净值图是 canvas：等 legend 文本出现即认为 ECharts 已重绘
  await page.waitForSelector("text=全市场等权", { timeout: 30_000 });
}

async function main() {
  await mkdir(IMAGES, { recursive: true });
  await mkdir(EVIDENCE, { recursive: true });

  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await signIn(context);
    const page = await context.newPage();

    // ── ① 基准叠加 ─────────────────────────────────────────
    await openBacktest(page);

    // 图例与线都画在 **canvas** 上，DOM 里没有这些文本——所以断言要读像素，
    // 不能拿 text= 去撞（撞上了也是撞到别处的同名文案，是假通过）。
    const painted = await page.evaluate(() => {
      // 净值图按**分节**定位而不是「页面上第一块 canvas」：同页下方还有 K 线图，
      // 按顺序取会取错（K 线是 Lightweight Charts，配色完全不同）
      const sections = [...document.querySelectorAll("section")];
      const target = sections.find((s) => s.textContent?.includes("净值曲线"));
      const canvas = target?.querySelector("canvas");
      if (!canvas) return null;
      const ctx = canvas.getContext("2d");
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
      // 浅色主题的三个 series token
      const palette = { 策略: [42, 120, 214], 买入持有: [235, 104, 52], 全市场等权: [138, 92, 214] };
      const hits = Object.fromEntries(Object.keys(palette).map((k) => [k, 0]));
      for (let i = 0; i < data.length; i += 4) {
        for (const [name, [r, g, b]] of Object.entries(palette)) {
          if (
            Math.abs(data[i] - r) <= 10 &&
            Math.abs(data[i + 1] - g) <= 10 &&
            Math.abs(data[i + 2] - b) <= 10
          ) {
            hits[name] += 1;
          }
        }
      }
      return { hits, width: canvas.width, height: canvas.height };
    });
    check("净值图取到画布", painted !== null);
    for (const [name, count] of Object.entries(painted?.hits ?? {})) {
      check(`画布上真的画出了「${name}」这条线`, count > 200, `${count} px`);
    }
    check(
      "画布只认出一个 canvas（没有画到别处）",
      (await page.locator("section", { hasText: "净值曲线" }).locator("canvas").count()) === 1,
    );

    const note = page.locator("text=数据源不覆盖指数");
    check("口径小字可见（且写明不是指数）", (await note.count()) > 0);
    const noteText = (await note.first().textContent()) ?? "";
    check("口径小字含剔除规则", noteText.includes("30%"), noteText.slice(0, 60) + "…");

    await page.screenshot({ path: `${IMAGES}/backtest-benchmark.png`, fullPage: true });

    // ── ② 名称搜索 ─────────────────────────────────────────
    await page.goto(`${FRONTEND}/space`, { waitUntil: "networkidle" });
    const input = page.getByLabel("股票代码或名称");
    await input.click();
    await input.fill("茅台");
    const option = page.locator("#symbol-candidates [role=option]").first();
    await option.waitFor({ timeout: 10_000 });
    const optionText = (await option.textContent()) ?? "";
    check("输名称出候选", optionText.includes("贵州茅台"), optionText.trim());
    await page.screenshot({ path: `${EVIDENCE}/name-dropdown.png`, fullPage: true });

    await input.press("ArrowDown");
    const active = await page.locator("#symbol-candidates [aria-selected=true]").count();
    check("↓ 键能选中候选", active === 1);

    await input.press("Enter");
    await page.waitForTimeout(300);
    const filled = await input.inputValue();
    check("Enter 把六位代码填进输入框", filled === "600519", filled);
    check("候选已收起", (await page.locator("#symbol-candidates").count()) === 0);

    // 选中后应走既有的代码体检（probe）——这条证明「写路径没被绕过」。
    // probe 有 300ms 防抖 + 一次往返，给足时间；文案里必然出现数据截止日
    await page.waitForTimeout(1200);
    const hint = ((await page.locator("form ~ p").first().textContent()) ?? "").trim();
    check("选中后触发了既有的代码体检", hint.length > 0 && !hint.includes("没有匹配"), hint.slice(0, 60));

    // ── ③ 深色 ─────────────────────────────────────────────
    await page.goto(`${FRONTEND}/backtest`, { waitUntil: "networkidle" });
    await page.emulateMedia({ colorScheme: "dark" });
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${EVIDENCE}/backtest-dark.png`, fullPage: true });

    // ── ④ 窄屏：下拉不溢出、不遮按钮 ───────────────────────
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(`${FRONTEND}/space`, { waitUntil: "networkidle" });
    const narrowInput = page.getByLabel("股票代码或名称");
    await narrowInput.fill("银行");
    const narrowOption = page.locator("#symbol-candidates [role=option]").first();
    await narrowOption.waitFor({ timeout: 10_000 });
    const box = await narrowOption.boundingBox();
    check("窄屏下候选不溢出右边界", !!box && box.x + box.width <= 390, box ? `${Math.round(box.x + box.width)}px` : "无");
    await page.screenshot({ path: `${EVIDENCE}/narrow.png`, fullPage: true });
  } finally {
    await browser.close();
  }

  const failed = RESULTS.filter((item) => !item.ok);
  console.log(`\n断言 ${RESULTS.length - failed.length}/${RESULTS.length} 通过`);
  await writeFile(
    `${EVIDENCE}/ui.json`,
    JSON.stringify({ results: RESULTS, failed: failed.length }, null, 1),
    "utf-8",
  );
  if (failed.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
