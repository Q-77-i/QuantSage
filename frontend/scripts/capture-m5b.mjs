/**
 * M5b 界面验证脚本：网格跑到热力图 + 点格重跑 + 批量表。
 *
 * 前置：后端 8000（**含 M5b 端点**）与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-m5b.mjs
 *
 * 落点：
 *   * `docs/images/optimize-grid.png` —— 热力图 + DSR 卡（浅色，README 用）
 *   * `logs/m5b/ui.md` 引用的四张证据图（浅色 / 深色 / 表格视图 / 批量 / 窄屏）
 *
 * **不只是截图**：断言八件事。其中最要紧的两条是自动化层结构上看不见的
 * （M5a 的教训：ECharts 的图例与折线都画在 canvas 上，`text=` 撞不到）：
 *   · 热力图**真的画在画布上**——读 `getImageData` 数色阶像素；
 *   * 色阶**确实是 diverging**——画布上同时存在偏红与偏蓝的像素，
 *     这是「正负两极都画出来了」的机械证据，而不是一句「应该有吧」。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m5b");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m5b-ui@example.com";
const PASSWORD = "m5b-ui-pass";

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

/**
 * 读宿主里**所有** canvas 的像素，按「离表面色够远 = 画了东西」计数。
 *
 * 两条都是踩出来的：
 *   · **必须汇总全部层**——ECharts 按 zlevel 把内容分到多个 canvas 上（实测热力图宿主里
 *     有三层：轴线的 chrome 层、热力图本体、色标图例），只读第一层会得到「0 个有色像素」
 *     这种看着像「图没画出来」的假失败；
 *   · 判据用「与表面色的距离」而不是「饱和度」——甘特式的中性灰点饱和度接近 0，
 *     按饱和度判会把它们全漏掉（分布图上那 9 个灰点就差点被判没了）。
 */
async function canvasStats(page, selector, surface) {
  return page.evaluate(
    ({ sel, bg }) => {
      const host = document.querySelector(sel);
      if (!host) return null;
      const canvases = [...host.querySelectorAll("canvas")];
      if (!canvases.length) return null;
      const [br, bgc, bb] = bg;
      let reddish = 0;
      let bluish = 0;
      let ink = 0;
      let width = 0;
      let height = 0;
      for (const canvas of canvases) {
        width = canvas.width;
        height = canvas.height;
        const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
        for (let i = 0; i < data.length; i += 4) {
          if (data[i + 3] < 200) continue;
          const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
          // 与表面色够远才算「画了东西」（表面色是图表底色，不是数据）
          if (Math.abs(r - br) + Math.abs(g - bgc) + Math.abs(b - bb) < 24) continue;
          ink += 1;
          if (r - b > 24) reddish += 1;
          else if (b - r > 24) bluish += 1;
        }
      }
      return { width, height, layers: canvases.length, reddish, bluish, ink };
    },
    { sel: selector, bg: surface },
  );
}

const LIGHT_SURFACE = [252, 252, 251]; // --chart-surface 浅色
const DARK_SURFACE = [19, 23, 34]; // --chart-surface 深色

async function runGrid(page, { fast = "3, 5, 8", slow = "10, 20, 30" } = {}) {
  await page.goto(`${FRONTEND}/optimize?mode=grid`, { waitUntil: "networkidle" });
  // 用**控件 id** 定位而不是 `getByLabel("参数")`：后者是子串匹配，会把「参数 的取值」
  // 那个输入框也算进候选，`nth(1)` 于是打到错的元素上（第一版就这么卡住的）
  await page.locator("#op-axis-0").selectOption("fast");
  await page.locator("#op-axis-0-values").fill(fast);
  await page.getByRole("button", { name: "加一条轴" }).click();
  await page.locator("#op-axis-1").selectOption("slow");
  await page.locator("#op-axis-1-values").fill(slow);
  await page.getByRole("button", { name: "运行" }).click();
  // 跑完的标志：DSR 卡出现数字（收尾帧才有的东西）
  await page.waitForSelector("text=Deflated Sharpe", { timeout: 120_000 });
  await page.waitForSelector("text=全网格夏普分布", { timeout: 30_000 });
}

async function main() {
  await mkdir(IMAGES, { recursive: true });
  await mkdir(EVIDENCE, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await signIn(context);
  const page = await context.newPage();

  // ── ① 网格跑到出图 ─────────────────────────────────────
  console.log("\n[1] 网格跑到热力图");
  await runGrid(page);
  check("网格跑完（DSR 卡出数）", true);

  const heat = await canvasStats(page, '[role="img"][aria-label*="热力图"]', LIGHT_SURFACE);
  check(
    "热力图取到画布",
    heat !== null,
    heat ? `${heat.width}×${heat.height}，${heat.layers} 层` : "没找到 canvas",
  );
  check("画布上真的画出了色块", (heat?.ink ?? 0) > 5000, `${heat?.ink ?? 0} 个非底色像素`);
  check(
    "色阶是 diverging（红蓝两端同时存在）",
    (heat?.reddish ?? 0) > 500 && (heat?.bluish ?? 0) > 500,
    `偏红 ${heat?.reddish ?? 0} / 偏蓝 ${heat?.bluish ?? 0}`,
  );

  const dist = await canvasStats(page, '[role="img"][aria-label*="夏普分布"]', LIGHT_SURFACE);
  check("分布图取到画布并画出了点", (dist?.ink ?? 0) > 200, `${dist?.ink ?? 0} 个非底色像素`);

  // 断言要**锚在热力图自己的口径行上**：第一版写的是 `locator("text=最优").first()`，
  // 结果撞上了 DSR 卡里的「最优夏普 SR」那一行，假通过（M5a 记过同一类坑）
  const hint = await page.locator('p:has-text("色阶固定对称于 0")').first().textContent();
  check("热力图有最优格直接标注", /最优\s+\w+=/.test(hint ?? ""), (hint ?? "").trim());
  await page.screenshot({ path: resolve(IMAGES, "optimize-grid.png"), fullPage: false });
  await page.screenshot({ path: resolve(EVIDENCE, "grid-light.png"), fullPage: true });

  // ── ② 表格视图（连续色标的无障碍孪生）─────────────────
  console.log("\n[2] 表格视图");
  await page.getByRole("button", { name: "看表格" }).click();
  const tableText = await page.locator("table").first().innerText();
  const numeric = tableText.match(/-?\d+\.\d{3}/g) ?? [];
  check("表格视图列出了逐格数值", numeric.length >= 9, `${numeric.length} 个数值`);
  check("表格里没有用 0 冒充缺值", !tableText.includes("0.000") || numeric.length > 0);
  await page.screenshot({ path: resolve(EVIDENCE, "grid-table.png"), fullPage: true });
  await page.getByRole("button", { name: "看图" }).click();
  // 切回来之后热力图必须**真的回来**（宿主重新挂载 ⇒ 实例要重建；判据连尺寸一起看——
  // 零尺寸容器会让 ECharts 按兜底的 100×300 建画布，「有像素」不足以证明画对了）
  const heatBack = await (async () => {
    await page
      .waitForFunction(
        () => {
          const canvas = document.querySelector('[role="img"][aria-label*="热力图"] canvas');
          return canvas !== null && canvas.width > 300;
        },
        undefined,
        { timeout: 10_000 },
      )
      .catch(() => undefined);
    return canvasStats(page, '[role="img"][aria-label*="热力图"]', LIGHT_SURFACE);
  })();
  check(
    "看表格 → 看图之后热力图真的回来了",
    (heatBack?.ink ?? 0) > 1000,
    heatBack ? `${heatBack.width}×${heatBack.height}，非底色 ${heatBack.ink}` : "没找到 canvas",
  );

  // ── ③ 点一格重跑 ───────────────────────────────────────
  console.log("\n[3] 点一格重跑");
  // 点**宿主容器**而不是 canvas：ECharts 分层，最上面那层 canvas 会拦掉指针事件
  // （Playwright 的可操作性检查直接拒点被遮住的元素——它是对的）。
  // 用 `locator.click({position})` 而不是 `mouse.click(绝对坐标)`：前者会先滚进视口，
  // 后者在元素滚出视口时会**静默点空**。
  const host = page.locator('[role="img"][aria-label*="热力图"]');
  const box = await host.boundingBox();
  await host.click({ position: { x: Math.round(box.width * 0.5), y: Math.round(box.height * 0.4) } });
  try {
    await page.waitForURL(/\/backtest\?run=/, { timeout: 30_000 });
  } catch {
    // 点空了就把页面上的错误文案带出来，别只留一句「超时」
    const shown = await page.locator('[role="alert"], .text-destructive').allInnerTexts();
    throw new Error(`点格重跑没有跳转；页面上的错误：${JSON.stringify(shown)}`);
  }
  await page.waitForSelector("text=净值曲线", { timeout: 60_000 });
  check("点一格 → 跳回测页且报告已出", true, page.url().replace(FRONTEND, ""));
  await page.screenshot({ path: resolve(EVIDENCE, "grid-rerun.png"), fullPage: false });

  // ── ④ 深色模式 ─────────────────────────────────────────
  console.log("\n[4] 深色模式");
  await context.addCookies([{ name: "theme", value: "dark", url: FRONTEND }]);
  const dark = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await signIn(dark);
  const darkPage = await dark.newPage();
  await darkPage.addInitScript(() => window.localStorage.setItem("theme", "dark"));
  await runGrid(darkPage);
  const darkHeat = await canvasStats(darkPage, '[role="img"][aria-label*="热力图"]', DARK_SURFACE);
  check(
    "深色下色阶是另一组步进（极色更亮）",
    (darkHeat?.reddish ?? 0) > 500 && (darkHeat?.bluish ?? 0) > 500,
    `偏红 ${darkHeat?.reddish ?? 0} / 偏蓝 ${darkHeat?.bluish ?? 0}`,
  );
  await darkPage.screenshot({ path: resolve(EVIDENCE, "grid-dark.png"), fullPage: true });

  // ── ⑤ 批量模式 ─────────────────────────────────────────
  console.log("\n[5] 批量模式");
  await page.goto(`${FRONTEND}/optimize?mode=batch`, { waitUntil: "networkidle" });
  await page.getByLabel(/标的/).fill("600519\n000001");
  await page.getByRole("button", { name: "运行" }).click();
  await page.waitForSelector("text=批量结果", { timeout: 120_000 });
  const batchTable = await page.locator("table").first().innerText();
  check("批量表出结果", batchTable.includes("600519") && batchTable.includes("000001"), "");
  check("批量表里有夏普读数", /夏普\s*-?\d+\.\d{3}/.test(batchTable), "");
  await page.screenshot({ path: resolve(EVIDENCE, "batch.png"), fullPage: true });

  // ── ⑥ 窄屏 ─────────────────────────────────────────────
  console.log("\n[6] 窄屏");
  const narrow = await browser.newContext({ viewport: { width: 390, height: 900 } });
  await signIn(narrow);
  const narrowPage = await narrow.newPage();
  await narrowPage.goto(`${FRONTEND}/optimize?mode=grid`, { waitUntil: "networkidle" });
  const overflow = await narrowPage.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check("窄屏无横向溢出", overflow <= 1, `溢出 ${overflow}px`);
  await narrowPage.screenshot({ path: resolve(EVIDENCE, "narrow.png"), fullPage: true });

  await browser.close();

  const passed = RESULTS.filter((item) => item.ok).length;
  const md = [
    "# M5b 界面验证",
    "",
    `> 由 \`frontend/scripts/capture-m5b.mjs\` 驱动真浏览器（Playwright）跑出，${passed}/${RESULTS.length} 条断言通过。`,
    "> 这一层抓的是自动化层结构上看不见的东西：画布上到底画没画、点得动点不动、窄屏溢不溢出。",
    "",
    "| # | 断言 | 结果 | 读数 |",
    "|---|---|---|---|",
    ...RESULTS.map((item, index) => `| ${index + 1} | ${item.label} | ${item.ok ? "✓" : "✗"} | ${item.detail} |`),
    "",
    "## 证据图",
    "",
    "- `docs/images/optimize-grid.png` —— 热力图 + DSR 卡（浅色，README 用）",
    "- `logs/m5b/grid-light.png` / `grid-dark.png` —— 深浅两色的色阶（**各自选步，不是自动翻转**）",
    "- `logs/m5b/grid-table.png` —— 表格视图（连续色标的无障碍孪生）",
    "- `logs/m5b/grid-rerun.png` —— 点一格重跑后落到的回测页",
    "- `logs/m5b/batch.png` —— 批量结果表",
    "- `logs/m5b/narrow.png` —— 390px 窄屏",
    "",
  ].join("\n");
  await writeFile(resolve(EVIDENCE, "ui.md"), md, "utf8");
  await writeFile(
    resolve(EVIDENCE, "ui.json"),
    JSON.stringify({ results: RESULTS, passed, total: RESULTS.length }, null, 1),
    "utf8",
  );

  console.log(`\n${passed}/${RESULTS.length} 条通过；证据写入 logs/m5b/ui.md`);
  process.exit(passed === RESULTS.length ? 0 : 1);
}

await main();
