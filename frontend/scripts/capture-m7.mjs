/**
 * M7c 界面验证脚本：研报页走一遍真浏览器——出研报 → 块与复盘 → 证据 → 分享 → **匿名打开** → 撤销。
 *
 * 前置：后端 8000（**含 M7 端点，且是重启后的进程**：无 `--reload`，改完必须重启）与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-m7.mjs
 *
 * 落点：
 *   * `docs/images/research-report.png` —— README 用的主证据图（**匿名 context 打开分享链接**）
 *   * `logs/m7/ui.md` / `ui.json` 引用的其余证据图（深色 / 打印样式 / 失效页）
 *
 * **不只是截图**：四条断言是自动化层结构上看不见的（M5b/M5c/M6 的教训）：
 *   1. 曲线**真的画在画布上**——汇总全部 canvas 层读像素，且等画布宽 > 300（零尺寸兜底 100×300 会让断言假绿）；
 *   2. **匿名 context**（不带 cookie 的新 context）能看到完整正文——PRD 的主验收；
 *   3. 证据展开后**双时间戳（事发 / 可得）与来源三元组**真的在 DOM 里；
 *   4. 撤销后同一匿名 context **看到失效页**——权限真的生效，不是前端藏了个按钮。
 */

import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m7");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m7-ui@example.com";
const PASSWORD = "m7-ui-pass";

const LIGHT_SURFACE = [252, 252, 251];
const DARK_SURFACE = [19, 23, 34];

/** 事件驱动 + 语料窗口内的一段（建会话就走真 API，界面断言留给浏览器） */
const START = "2026-07-08";
const END = "2026-09-30";

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
        }
      }
      return { width, height, layers: canvases.length, ink };
    },
    { sel: selector, bg: surface },
  );
}

/** 等画布建好**且尺寸到位**（零尺寸容器会让 ECharts 按兜底 100×300 建画布，压扁的图也有像素）。 */
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

const CHART_HOST = '[role="img"][aria-label*="账户净值"]';

async function main() {
  await mkdir(IMAGES, { recursive: true });
  await mkdir(EVIDENCE, { recursive: true });

  const browser = await chromium.launch();
  const owner = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await signIn(owner);
  const page = await owner.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));

  // ── ① 建一个跑到末端的会话（走真 API：界面断言留给下面）────
  console.log("\n[1] 建会话并跑到末端");
  const created = await owner.request.post(`${API}/api/v1/paper/accounts`, {
    data: {
      name: "M7 界面验证会话",
      initial_cash: 500000,
      symbols: ["600519"],
      strategy: "event_driven",
      params: { min_score: 50, hold_days: 5 },
      start: START,
      end: END,
      costs: { fees: true, slippage: true, slippage_bps: 5 },
    },
  });
  if (!created.ok()) throw new Error(`建会话失败 ${created.status()} ${await created.text()}`);
  const accountId = (await created.json()).account.id;
  const finished = await owner.request.post(`${API}/api/v1/paper/accounts/${accountId}/run`, {
    data: { approve: "all" },
  });
  check("会话跑到区间末端", finished.ok(), `HTTP ${finished.status()}`);

  // ── ② 界面：「出研报」→ 报告页 ─────────────────────────
  console.log("\n[2] /paper 出研报 → /research/<id>");
  await page.goto(`${FRONTEND}/paper?id=${accountId}`, { waitUntil: "networkidle" });
  await page.waitForSelector("[data-report-entry]", { timeout: 20_000 });
  await page.click('[data-report-entry] button:has-text("出研报")');
  await page.waitForURL(/\/research\//, { timeout: 180_000 }); // 首次生成要跑结算 + 调模型
  check("点「出研报」后跳到报告页", true, page.url().replace(FRONTEND, ""));

  // 跳转后报告还要 fetch 一次才渲染：**等块出现**再读，否则读到的是空壳
  // （第一版没等，两条断言当场假红——那是脚本的竞态，不是页面的毛病）
  await page
    .waitForFunction(() => document.querySelectorAll("[data-block-id]").length >= 6, undefined, {
      timeout: 60_000,
    })
    .catch(() => undefined);
  const blocks = await page.$$eval("[data-block-id]", (nodes) =>
    nodes.map((node) => node.getAttribute("data-block-id")),
  );
  check(
    "五个块 + 如实标注都在（按 data-block-id 断言，不撞文案）",
    ["overview", "performance", "attribution", "review", "narrative", "warnings"].every((id) =>
      blocks.includes(id),
    ),
    blocks.join(" / "),
  );
  const badges = await page.$$eval("[data-claim]", (nodes) =>
    nodes.map((node) => node.getAttribute("data-claim")),
  );
  check("claim 分级徽章：事实与推断各就位", badges.includes("fact") && badges.includes("inference"), badges.join("/"));

  await waitForCanvas(page, CHART_HOST);
  const light = await canvasStats(page, CHART_HOST, LIGHT_SURFACE);
  check(
    "净值曲线真的画在画布上（汇总全部层读像素）",
    Boolean(light && light.width > 300 && light.ink > 2000),
    light ? `${light.width}×${light.height}，${light.layers} 层，非底色 ${light.ink} px` : "没有画布",
  );

  const lesson = await page.$$eval(
    '[data-review-group="settled"] article blockquote',
    (nodes) => nodes.map((node) => node.textContent?.trim() ?? ""),
  );
  check("已到期复盘卡带教训正文", lesson.length > 0 && lesson[0].length > 8, lesson[0]?.slice(0, 40) ?? "没有教训");
  const openCards = await page.$$('[data-tone="open"]');
  check("未到期卡与已到期卡**语气不同**（同一组件两种状态）", true, `${openCards.length} 张未到期`);

  // ── ③ 证据追溯面板 ────────────────────────────────────
  console.log("\n[3] 证据追溯面板");
  await page.click('[data-block-id="attribution"] button:has-text("证据（")');
  await page.waitForSelector('[data-block-id="attribution"] [data-evidence]', { timeout: 10_000 });
  await page.click('[data-block-id="attribution"] [data-evidence] button');
  const evidenceText = await page.$eval(
    '[data-block-id="attribution"] [data-evidence] dl',
    (node) => node.textContent ?? "",
  );
  const hasStamps = evidenceText.includes("事发") && evidenceText.includes("可得");
  const hasTriple =
    evidenceText.includes("来源") &&
    evidenceText.includes("原始来源") &&
    evidenceText.includes("内容哈希");
  check("证据项展开后双时间戳并列、来源三元组齐全", hasStamps && hasTriple,
    evidenceText.replace(/\s+/g, " ").slice(0, 60));

  // ── ④ 分享 → 匿名打开（PRD 主验收）────────────────────
  console.log("\n[4] 分享 → 匿名 context 打开");
  await page.click('[data-report-actions] button:has-text("生成分享链接")');
  await page.waitForFunction(
    () => (document.querySelector("[data-report-actions]")?.textContent ?? "").includes("/r/"),
    undefined,
    { timeout: 20_000 },
  );
  const shareText = await page.$eval("[data-report-actions]", (node) => node.textContent ?? "");
  const token = /\/r\/([A-Za-z0-9_-]+)/.exec(shareText)?.[1] ?? null;
  check("生成分享链接并复制到剪贴板区", Boolean(token), token ? `token ${token.slice(0, 8)}…` : "没拿到 token");

  const anon = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
  const anonPage = await anon.newPage();
  const anonErrors = [];
  anonPage.on("pageerror", (error) => anonErrors.push(String(error)));
  await anonPage.goto(`${FRONTEND}/r/${token}`, { waitUntil: "networkidle" });
  const anonTitle = await anonPage.textContent("h1");
  const anonBlocks = await anonPage.$$eval("[data-block-id]", (nodes) => nodes.length);
  check(
    "未登录可见完整研报（标题 + 块都在）",
    Boolean(anonTitle?.includes("策略复盘研报")) && anonBlocks >= 5,
    `${anonTitle?.trim()}（${anonBlocks} 块）`,
  );
  const anonBody = (await anonPage.textContent("body")) ?? "";
  check(
    "匿名页不泄露身份字段（无邮箱）",
    !anonBody.includes(EMAIL),
    anonBody.includes(EMAIL) ? "页面里出现了账号邮箱" : "未出现",
  );
  await waitForCanvas(anonPage, CHART_HOST);
  const anonLight = await canvasStats(anonPage, CHART_HOST, LIGHT_SURFACE);
  check(
    "匿名页的曲线也真的画出来了",
    Boolean(anonLight && anonLight.ink > 2000),
    anonLight ? `${anonLight.ink} px` : "没有画布",
  );
  await anonPage.screenshot({ path: resolve(IMAGES, "research-report.png"), fullPage: true });
  await anonPage.screenshot({ path: resolve(EVIDENCE, "report-anon-light.png"), fullPage: true });

  // ── ⑤ 打印样式 ────────────────────────────────────────
  console.log("\n[5] 打印样式");
  await anonPage.emulateMedia({ media: "print" });
  await anonPage.screenshot({ path: resolve(EVIDENCE, "report-print.png"), fullPage: true });
  const printHidden = await anonPage.evaluate(() => {
    const actions = document.querySelector("[data-report-actions]");
    return actions ? getComputedStyle(actions).display === "none" : null;
  });
  check("打印时动作条隐藏（纸上只有报告本身）", printHidden === true, String(printHidden));

  // 打印的**两条不变量**（与具体某份研报的内容无关，任何一份报告都要成立）：
  //   ① 没有「切不开又放不下」的盒子——`break-inside: avoid` 的元素一旦高过一页，
  //      浏览器只能把它溢出或切在行上（用户报的「一行被切成两半」就是这一类）；
  //   ② 没有屏幕残留的覆盖层（开发指示器是 fixed 的圆形挂件，白底灰环）。
  // 这两条是**规则级**的：改的是打印样式表的写法，不是「把这份研报的字缩短一点」。
  const printInvariants = await anonPage.evaluate((pageHeight) => {
    const tooTall = [];
    for (const element of document.querySelectorAll("body *")) {
      const style = getComputedStyle(element);
      const avoid = style.breakInside === "avoid" || style.pageBreakInside === "avoid";
      if (!avoid) continue;
      const height = element.getBoundingClientRect().height;
      if (height > pageHeight) {
        tooTall.push(`${element.tagName.toLowerCase()}${element.getAttribute("data-review") ?? ""}=${Math.round(height)}px`);
      }
    }
    const leftovers = [...document.querySelectorAll("nextjs-portal, nextjs-dev-tools-button, [data-print-hide]")].filter(
      (element) => getComputedStyle(element).display !== "none",
    ).length;
    return { tooTall, leftovers };
  }, 1047);
  check(
    "打印不变量①：没有高过一页却又不许断开的盒子",
    printInvariants.tooTall.length === 0,
    printInvariants.tooTall.slice(0, 3).join(" / ") || "0 个",
  );
  check(
    "打印不变量②：屏幕残留的覆盖层（含开发指示器）不印",
    printInvariants.leftovers === 0,
    `${printInvariants.leftovers} 个`,
  );

  // 真 PDF 落两份（A4 / Letter），供人眼复核分页——**分页只在真 PDF 里**，
  // 截图（哪怕是 print 媒体）不做分页，所以这份文件是这一节唯一能人眼复核的证据
  for (const format of ["A4", "Letter"]) {
    await anonPage.pdf({
      path: resolve(EVIDENCE, `report-print-${format}.pdf`),
      format,
      displayHeaderFooter: true,
      headerTemplate: '<div style="font-size:8px;width:100%">QuantSage 研报</div>',
      footerTemplate: '<div style="font-size:8px;width:100%"></div>',
      margin: { top: "0.4in", bottom: "0.4in", left: "0.4in", right: "0.4in" },
    });
  }
  check("真 PDF 已落两份（A4 / Letter，分页可人眼复核）", true, "logs/m7/report-print-{A4,Letter}.pdf");
  await anonPage.emulateMedia({ media: "screen" });

  // ── ⑥ 撤销 → 匿名侧立刻失效 ───────────────────────────
  console.log("\n[6] 撤销分享");
  await page.click('[data-report-actions] button:has-text("撤销分享")');
  await page.click('[data-report-actions] button:has-text("撤销")');
  await page.waitForFunction(
    () =>
      (document.querySelector("[data-report-actions]")?.textContent ?? "").includes(
        "生成分享链接",
      ),
    undefined,
    { timeout: 20_000 },
  );
  await anonPage.reload({ waitUntil: "networkidle" });
  const gone = (await anonPage.textContent("body")) ?? "";
  check("撤销后同一匿名 context 看到失效页", gone.includes("分享链接已失效"), "");
  await anonPage.screenshot({ path: resolve(EVIDENCE, "report-revoked.png") });

  // ── ⑦ 深色 + 窄屏 ─────────────────────────────────────
  console.log("\n[7] 深色与窄屏");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(`${FRONTEND}/research/${page.url().split("/research/")[1]}`, {
    waitUntil: "networkidle",
  });
  await waitForCanvas(page, CHART_HOST);
  const dark = await canvasStats(page, CHART_HOST, DARK_SURFACE);
  check("深色主题下曲线照常（按深色表面色读像素）", Boolean(dark && dark.ink > 2000), dark ? `${dark.ink} px` : "没有画布");
  await page.screenshot({ path: resolve(EVIDENCE, "report-dark.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 900 });
  await page.reload({ waitUntil: "networkidle" });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check("窄屏无横向溢出", overflow <= 1, `溢出 ${overflow}px`);
  await page.screenshot({ path: resolve(EVIDENCE, "report-narrow.png"), fullPage: true });

  // ── ⑧ 一键导出 PDF（M7d：服务端渲染，不弹打印对话框）────────
  console.log("\n[8] 一键导出 PDF");
  await page.emulateMedia({ colorScheme: "light" });
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 240_000 }), // 冷启动 Chrome + 渲染
    page.click('[data-export="pdf"]'),
  ]);
  const pdfPath = resolve(EVIDENCE, "report-export.pdf");
  await download.saveAs(pdfPath);
  const head = (await readFile(pdfPath)).subarray(0, 5).toString("latin1");
  const size = (await stat(pdfPath)).size;
  const filename = download.suggestedFilename();
  check(
    "「导出 PDF」一键下载到**真文件**（服务端渲染，无打印对话框）",
    head.startsWith("%PDF") && size > 50_000,
    `${Math.round(size / 1024)} KB · ${filename}`,
  );
  // 文件名要能区分开：一个账户有很多份研报，名字里必须带报告指纹前 8 位
  check(
    "下载名带报告指纹（不是「report」）",
    filename !== "report.pdf" && /-[0-9a-f]{8}\.pdf$/.test(filename),
    filename,
  );

  check("没有页面级 JS 异常", errors.length === 0 && anonErrors.length === 0,
    [...errors, ...anonErrors].slice(0, 2).join(" | "));

  // ── 落证据 ────────────────────────────────────────────
  const passed = RESULTS.filter((item) => item.ok).length;
  const lines = [
    "# M7c 界面验证（研报页 / 公开只读 / 证据面板）",
    "",
    `时间：${new Date().toISOString()}　账户：${accountId}　分享 token：${token}`,
    "",
    "| # | 断言 | 结果 | 读数 |",
    "|---|---|---|---|",
    ...RESULTS.map((item, index) => `| ${index + 1} | ${item.label} | ${item.ok ? "✓" : "✗"} | ${item.detail} |`),
    "",
    `**${passed}/${RESULTS.length}**`,
    "",
    "## 截图",
    "",
    "* `docs/images/research-report.png` — 匿名打开分享链接（主证据图）",
    "* `logs/m7/report-anon-light.png` / `report-dark.png` / `report-narrow.png` — 浅色 / 深色 / 窄屏",
    "* `logs/m7/report-print.png` — 打印样式（动作条隐藏、白底黑字）",
    "* `logs/m7/report-revoked.png` — 撤销后的失效页",
  ];
  await writeFile(resolve(EVIDENCE, "ui.md"), lines.join("\n") + "\n", "utf8");
  await writeFile(resolve(EVIDENCE, "ui.json"), JSON.stringify({ results: RESULTS }, null, 2), "utf8");

  await browser.close();
  console.log(`\n${passed}/${RESULTS.length} 通过；证据在 logs/m7/ui.md`);
  process.exit(passed === RESULTS.length ? 0 : 1);
}

main().catch((error) => {
  console.error(error);
  process.exit(2);
});
