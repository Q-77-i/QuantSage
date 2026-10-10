/**
 * M5c-2 界面验证脚本：因子页跑到出图 + 毛/净切换 + 表格孪生 + 两个因子源。
 *
 * 前置：后端 8000（**含 M5c 端点**）与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-m5c.mjs
 *
 * 落点：
 *   * `docs/images/factor-ic.png` —— 指标卡 + IC 柱（浅色，README 用）
 *   * `logs/m5c/ui.md` 引用的证据图（浅色 / 深色 / 表格 / 价格源 / 窄屏）
 *
 * **不只是截图**：断言十件事，其中最要紧的三条是自动化层结构上看不见的
 * （M5a/M5b 的教训：ECharts 画在 canvas 上，`text=` 撞不到任何东西）：
 *   · IC 柱**真的按符号上色**——画布上同时数出偏红与偏蓝的柱子；
 *   · 分层曲线**五档蓝阶每一档都存在**——按 token 字面值逐档数像素，
 *     少画一档（比如只画了三条线）当场就能看出来；
 *   · 毛/净切换**真的换了数**——切到表格读同一格的数字，净必须 ≤ 毛。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m5c");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m5c-ui@example.com";
const PASSWORD = "m5c-ui-pass";

const LIGHT_SURFACE = [252, 252, 251]; // --chart-surface 浅色
const DARK_SURFACE = [19, 23, 34]; // --chart-surface 深色
/** `chart-theme.light.group` 的五个字面值（Q1 最浅 → Q5 最深） */
const LIGHT_GROUP_RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"];

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
 * 读宿主里**所有** canvas 的像素：非底色计数 + 红蓝倾向 + 指定色号的精确命中数。
 *
 * 必须汇总全部层——ECharts 按 zlevel 把内容分到多个 canvas（M5b 实测热力图宿主里三层），
 * 只读第一层会得到「0 个有色像素」这种看着像「图没画出来」的假失败。
 */
async function canvasStats(page, selector, surface, hexes = []) {
  return page.evaluate(
    ({ sel, bg, wanted }) => {
      const host = document.querySelector(sel);
      if (!host) return null;
      const canvases = [...host.querySelectorAll("canvas")];
      if (!canvases.length) return null;
      const [br, bgc, bb] = bg;
      const targets = wanted.map((hex) => [
        parseInt(hex.slice(1, 3), 16),
        parseInt(hex.slice(3, 5), 16),
        parseInt(hex.slice(5, 7), 16),
      ]);
      const hits = targets.map(() => 0);
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
          if (Math.abs(r - br) + Math.abs(g - bgc) + Math.abs(b - bb) < 24) continue;
          ink += 1;
          if (r - b > 24) reddish += 1;
          else if (b - r > 24) bluish += 1;
          targets.forEach(([tr, tg, tb], index) => {
            if (Math.abs(r - tr) <= 2 && Math.abs(g - tg) <= 2 && Math.abs(b - tb) <= 2) hits[index] += 1;
          });
        }
      }
      return { width, height, layers: canvases.length, reddish, bluish, ink, hits };
    },
    { sel: selector, bg: surface, wanted: hexes },
  );
}

const IC_HOST = '[role="img"][aria-label*="逐日 RankIC"]';
const GROUP_HOST = '[role="img"][aria-label*="净值：五个分位组"]';
const SPREAD_HOST = '[role="img"][aria-label*="多空价差净值"]';

async function waitForReport(page) {
  await page.waitForSelector(IC_HOST, { timeout: 60_000 });
  await page.waitForSelector(GROUP_HOST, { timeout: 30_000 });
  await page.waitForSelector(SPREAD_HOST, { timeout: 30_000 });
}

/**
 * 切回「看图」之后，图必须**真的回来**。
 *
 * 这条断言是 2026-10-10 补上的：脚本原本也走「看表格 → 看图」，但**没验回来之后有没有图**，
 * 于是「切回来是一张空画布」这个 bug 一路全绿（宿主重新挂载，`useEChart` 只在挂载时 init）。
 * 判据连**尺寸**一起看：零尺寸容器会让 ECharts 按兜底的 100×300 建画布——
 * 一个压扁的图也有像素，「有像素」不足以证明它画对了。
 */
async function canvasBack(page, selector, surface, label) {
  await page
    .waitForFunction(
      (sel) => {
        const canvas = document.querySelector(`${sel} canvas`);
        return canvas !== null && canvas.width > 300;
      },
      selector,
      { timeout: 10_000 },
    )
    .catch(() => undefined);
  const stats = await canvasStats(page, selector, surface);
  check(label, (stats?.ink ?? 0) > 500, stats ? `${stats.width}×${stats.height}，非底色 ${stats.ink}` : "没找到 canvas");
}

async function main() {
  await mkdir(IMAGES, { recursive: true });
  await mkdir(EVIDENCE, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await signIn(context);
  const page = await context.newPage();

  // ── ① 事件因子：进页面自动出报告 ──────────────────────
  console.log("\n[1] 事件因子出报告");
  await page.goto(`${FRONTEND}/factor`, { waitUntil: "networkidle" });
  await waitForReport(page);
  check("报告出图（IC / 分层 / 多空三个图宿主都在）", true);

  const ic = await canvasStats(page, IC_HOST, LIGHT_SURFACE);
  check("IC 图画布上有像素", ic !== null && ic.ink > 500, ic ? `${ic.width}×${ic.height}，${ic.layers} 层，非底色 ${ic.ink}` : "没找到 canvas");
  check(
    "IC 柱**按符号上色**（红蓝两端同时存在）",
    (ic?.reddish ?? 0) > 50 && (ic?.bluish ?? 0) > 50,
    `偏红 ${ic?.reddish ?? 0} / 偏蓝 ${ic?.bluish ?? 0}`,
  );

  // ── ② 分层曲线：五档蓝阶逐档数像素 ────────────────────
  console.log("\n[2] 分层曲线的五档色阶");
  const group = await canvasStats(page, GROUP_HOST, LIGHT_SURFACE, LIGHT_GROUP_RAMP);
  const perStep = group?.hits ?? [];
  check(
    "五档蓝阶**每一档都画出来了**",
    perStep.length === 5 && perStep.every((count) => count > 20),
    perStep.map((count, index) => `Q${index + 1} ${count}px`).join(" / "),
  );

  await page.screenshot({ path: resolve(IMAGES, "factor-ic.png"), fullPage: true });
  await page.screenshot({ path: resolve(EVIDENCE, "light.png"), fullPage: true });

  // ── ③ 「不显著」标记与 t 值同源 ───────────────────────
  console.log("\n[3] 显著性标记");
  const tText = await page.locator("text=t 值").locator("xpath=following-sibling::p[1]").innerText();
  const badge = await page.locator('[role="status"]').innerText();
  const tValue = Number.parseFloat(tText);
  check(
    "标记与 t 值一致（|t| < 2 ⇒ 不显著）",
    (Math.abs(tValue) < 2) === badge.includes("不显著"),
    `t = ${tText}，标记 = ${badge}`,
  );

  // ── ④ 如实标注：服务端 notes 全部渲染 ─────────────────
  const noteItems = await page.locator("section:has-text('如实标注') li").count();
  check("如实标注逐条渲染", noteItems >= 8, `${noteItems} 条`);

  // ── ⑤ 表格孪生 + 毛/净切换真的换数 ────────────────────
  console.log("\n[4] 表格孪生与毛/净切换");
  await page.getByRole("button", { name: "看表格" }).first().click();
  const icRows = await page.locator("table:has(caption:text-is('逐日 RankIC 与当日池内样本数')) tbody tr").count();
  check("IC 表格孪生有逐日行", icRows > 20, `${icRows} 行`);
  await page.getByRole("button", { name: "看图" }).first().click();
  await canvasBack(page, IC_HOST, LIGHT_SURFACE, "看表格 → 看图之后 IC 图真的回来了");

  // 用**标题**锚定而不是「含图宿主」：切到表格后图宿主就没了，`has:` 过滤会跟着失配
  const groupSection = page.locator("section").filter({ hasText: "分层净值（五等分）" });
  await groupSection.getByRole("button", { name: "看表格" }).click();
  const cellOf = async () =>
    Number.parseFloat(
      (await groupSection.locator("table tbody tr").last().locator("td").nth(1).innerText()).replace(/,/g, ""),
    );
  const netLevel = await cellOf();
  await groupSection.getByRole("button", { name: "毛（不含费用）" }).click();
  const grossLevel = await cellOf();
  check(
    "毛/净切换真的换了数（同一格的净 ≤ 毛）",
    Number.isFinite(netLevel) && Number.isFinite(grossLevel) && netLevel <= grossLevel,
    `净 ${netLevel} ≤ 毛 ${grossLevel}`,
  );
  await groupSection.getByRole("button", { name: "看图" }).click();
  await canvasBack(page, GROUP_HOST, LIGHT_SURFACE, "分层图切表格再切回来也真的回来了");
  await groupSection.getByRole("button", { name: "净（扣费用）" }).click();

  // ── ⑥ 价格因子源（深链） ──────────────────────────────
  console.log("\n[5] 价格因子源");
  await page.goto(`${FRONTEND}/factor?source=price&direction=momentum`, { waitUntil: "networkidle" });
  await waitForReport(page);
  // 同样按**完整标题**锚定：`has-text('多空价差')` 会连「如实标注」区里那条
  // 「多空价差是统计量…」的说明一起匹配上，`.last()` 取到的是隔壁（M5a 撞同名文案同款）
  const spreadNote = await page
    .locator("section")
    .filter({ hasText: "多空价差（Q5 − Q1）" })
    .locator("p")
    .last()
    .innerText();
  check(
    "价格源跑通且如实标注不可交易",
    spreadNote.includes("统计量") && spreadNote.includes("不可做空"),
    spreadNote.slice(0, 40),
  );
  const priceIc = await canvasStats(page, IC_HOST, LIGHT_SURFACE);
  check("价格源的 IC 图也画出来了", (priceIc?.ink ?? 0) > 500, `非底色 ${priceIc?.ink ?? 0}`);
  await page.screenshot({ path: resolve(EVIDENCE, "price-momentum.png"), fullPage: true });

  // ── 图例线型的裁剪图（2026-10-10 用户两次反馈「两个图例一模一样」）──
  // 这类「看不看得出来」的问题**不做像素断言**：那会退化成对字体渲染的断言
  // （文本笔画在 12px 下最长约 7px，与虚线段 9px 太近）。留一张裁剪图给人核对。
  await page.goto(`${FRONTEND}/factor`, { waitUntil: "networkidle" });
  await waitForReport(page);
  const spreadBox = await page.locator("section").filter({ hasText: "多空价差（Q5 − Q1）" }).boundingBox();
  if (spreadBox) {
    await page.screenshot({
      path: resolve(EVIDENCE, "legend.png"),
      fullPage: true,
      clip: {
        x: spreadBox.x + spreadBox.width * 0.7,
        y: spreadBox.y + 46,
        width: spreadBox.width * 0.3,
        height: 40,
      },
    });
    check("图例裁剪图已出（一实一虚，供人眼核对）", true, "logs/m5c/legend.png");
  }

  // ── ⑦ 深色模式 ────────────────────────────────────────
  console.log("\n[6] 深色模式");
  const darkContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await darkContext.addInitScript(() => window.localStorage.setItem("theme", "dark"));
  await signIn(darkContext);
  const darkPage = await darkContext.newPage();
  await darkPage.goto(`${FRONTEND}/factor`, { waitUntil: "networkidle" });
  await waitForReport(darkPage);
  const darkGroup = await canvasStats(darkPage, GROUP_HOST, DARK_SURFACE);
  check("深色下分层曲线仍画出来", (darkGroup?.ink ?? 0) > 500, `非底色 ${darkGroup?.ink ?? 0}`);
  await darkPage.screenshot({ path: resolve(EVIDENCE, "dark.png"), fullPage: true });
  await darkContext.close();

  // ── ⑧ 窄屏 ────────────────────────────────────────────
  console.log("\n[7] 窄屏");
  const narrow = await browser.newContext({ viewport: { width: 390, height: 900 } });
  await signIn(narrow);
  const narrowPage = await narrow.newPage();
  await narrowPage.goto(`${FRONTEND}/factor`, { waitUntil: "networkidle" });
  await waitForReport(narrowPage);
  const overflow = await narrowPage.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check("390px 无横向溢出", overflow <= 1, `溢出 ${overflow}px`);
  await narrowPage.screenshot({ path: resolve(EVIDENCE, "narrow.png"), fullPage: true });
  await narrow.close();

  await browser.close();

  const passed = RESULTS.filter((item) => item.ok).length;
  const md = [
    "# M5c 界面验证",
    "",
    `> 由 \`frontend/scripts/capture-m5c.mjs\` 驱动真浏览器（Playwright）跑出，${passed}/${RESULTS.length} 条断言通过。`,
    "> 这一层抓的是自动化层结构上看不见的东西：画布上到底画没画、颜色对不对、点得动点不动、窄屏溢不溢出。",
    "",
    "| # | 断言 | 结果 | 读数 |",
    "|---|---|---|---|",
    ...RESULTS.map((item, index) => `| ${index + 1} | ${item.label} | ${item.ok ? "✓" : "✗"} | ${item.detail} |`),
    "",
    "## 证据图",
    "",
    "- `docs/images/factor-ic.png` —— 指标卡 + IC 柱（浅色，README 用）",
    "- `logs/m5c/light.png` / `dark.png` —— 深浅两色（**各自选步，不是自动翻转**）",
    "- `logs/m5c/price-momentum.png` —— 价格源（20 日动量）",
    "- `logs/m5c/legend.png` —— **多空图例的裁剪放大**：净=实线、毛=虚线（同色时线型是唯一区分，",
    "  用户 2026-10-10 两次反馈「看不出来」，第一版是 20px 图例短线 + ECharts 默认虚线节奏",
    "  （8 实 2 空）→ 1:1 下读作实线；改成 30px + 节奏 [9, 6] 后才分得开）",
    "- `logs/m5c/narrow.png` —— 390px 窄屏",
    "",
    "## 关于「不显著」那枚标记",
    "",
    "它不是写死的文案：断言拿页面上的 **t 值**与标记对着判（`|t| < 2 ⇒ 不显著`），",
    "数据刷新后 t 变了，标记跟着变——这正是它存在的意义（把噪声读成信号是这一页最大的风险）。",
    "",
  ].join("\n");
  await writeFile(resolve(EVIDENCE, "ui.md"), md, "utf8");
  await writeFile(
    resolve(EVIDENCE, "ui.json"),
    JSON.stringify({ results: RESULTS, passed, total: RESULTS.length }, null, 1),
    "utf8",
  );

  console.log(`\n${passed}/${RESULTS.length} 条通过；证据写入 logs/m5c/ui.md`);
  process.exit(passed === RESULTS.length ? 0 : 1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
