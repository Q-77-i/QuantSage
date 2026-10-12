/**
 * M10a 界面验证：研究首页（落地页 + 大盘总览 + 板块块）走一遍真浏览器。
 *
 * 前置：后端 8000（**含 M10 端点、且是重启后的进程**——无 `--reload`，改完必须重启）与前端 3001 都在跑。
 * 用法：`cd frontend && node scripts/capture-m10.mjs`
 *
 * 六条是自动化层结构上看不见的（M5b/M5c/M6/M7/M8 的教训）：
 *   1. **大盘读数与独立复算一致**——脚本用 `/market/cross-section` 逐页自己数涨跌与成交额、
 *      自己算等权均值，**不复用 `/overview` 的实现**；涨跌停再用 raw 日线在 JS 里重算涨跌停价比对；
 *   2. **板块条真的画出来了**——读画布像素：红端与绿端**同时存在**（「涨跌都画出来了」的证据）；
 *   3. **条按涨跌幅降序**——顺序错了色阶就是一张废图；
 *   4. **搬迁回归**——`/` 是研究首页、`/chat` 是对话页、页头「首页」高亮、旧深链不再指向对话；
 *   5. 口径字真的在屏幕上（「非指数」、两个「截至」、覆盖率）；
 *   6. 深浅两色各一张、窄屏 390px 不横向溢出。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m10");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m10-ui@example.com";
const PASSWORD = "m10-ui-pass";

/** 等权代理的剔除阈值，与后端 `EXCLUDE_ABS_CHANGE_PCT` 同值。**故意写死**： */
/** 复算若也从接口取这个数，就等于用被验方给的尺子去验被验方。 */
const EXCLUDE_ABS_CHANGE_PCT = 30;

/**
 * 涨跌停候选的门槛（%）。**不能按「最窄的档是 5%」就取 4.9**：低价股的四舍五入会把
 * 实际涨跌幅压下来（前收 0.50 元时 5% 档的跌停价是 0.48，实际只有 −4.0%）。而且主板 ST
 * 实测是 10% 档，最低的档位也不再是 5%。取 3.5 留足余量，代价只是候选集略大。
 */
const LIMIT_CANDIDATE_PCT = 3.5;

const RESULTS = [];
const BLOCKERS = [];

/** 浏览器句柄：挂在模块级，好让 catch 分支也能关掉它（见 `main` 的注释）。 */
let browser = null;

function check(label, ok, detail = "") {
  RESULTS.push({ label, ok, detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

function blocker(where, detail) {
  BLOCKERS.push({ where, detail });
  console.log(`  ⚠ 断点 ${where}：${detail}`);
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

/** 画布像素统计：偏红 / 偏绿的像素个数（板块条的红绿两端各要有）。 */
async function barPixelTone(page) {
  return page.evaluate(() => {
    const host = document.querySelector('[data-chart="sectors"]');
    if (!host) return null;
    let red = 0;
    let green = 0;
    let total = 0;
    for (const canvas of host.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d");
      if (!context) continue;
      const { width, height } = canvas;
      // 逐像素太慢，按 4px 网格采样——两端各上千像素，采样足够分辨「有没有」
      const image = context.getImageData(0, 0, width, height);
      for (let y = 0; y < height; y += 4) {
        for (let x = 0; x < width; x += 4) {
          const i = (y * width + x) * 4;
          const [r, g, b, a] = [image.data[i], image.data[i + 1], image.data[i + 2], image.data[i + 3]];
          if (a < 200) continue;
          total += 1;
          if (r > g + 24 && r > b + 24) red += 1;
          else if (g > r + 24 && g > b + 12) green += 1;
        }
      }
    }
    return { red, green, total };
  });
}

/**
 * **独立复算**：用 `/market/cross-section` 逐页取全市场，自己数涨跌家数、自己求成交额与
 * 等权均值。这条路径与 `/overview` 的实现完全无关（一个按标的逐行给，一个在 SQL 里聚合）。
 */
async function recomputeFromCrossSection(context, date) {
  const pageSize = 500;
  let offset = 0;
  let total = null;
  let up = 0;
  let down = 0;
  let flat = 0;
  let withoutPct = 0;
  let amount = 0;
  const returns = [];
  // 两端都要收：封板的标的必然落在涨跌幅的某一端（最窄的档是 ST 主板的 5%），
  // 只收涨幅榜就会把跌停算成 0——第一版正是这么错的
  const upCandidates = [];
  const downCandidates = [];
  // **没有 change_pct 的也要收**：后端判涨跌停只看 raw 收盘与前收，根本不读涨跌幅，
  // 5,556 只里有 5 只没有涨跌幅——按涨跌幅筛候选集会把它们整个漏掉（差的那 1 只就在这儿）
  const nullPctCandidates = [];
  for (;;) {
    const response = await context.request.get(
      `${API}/api/v1/market/cross-section?date=${date}&limit=${pageSize}&offset=${offset}&sort=symbol&order=asc`,
    );
    if (!response.ok()) throw new Error(`cross-section ${response.status()}`);
    const body = await response.json();
    if (total === null) total = body.total;
    if (body.items.length === 0) break;
    for (const row of body.items) {
      if (row.change_pct === null) withoutPct += 1;
      else if (row.change_pct > 0) up += 1;
      else if (row.change_pct < 0) down += 1;
      else flat += 1;
      amount += row.amount ?? 0;
      if (row.change_pct === null) nullPctCandidates.push(row.symbol);
      if (row.change_pct !== null && row.change_pct >= LIMIT_CANDIDATE_PCT) upCandidates.push(row.symbol);
      if (row.change_pct !== null && row.change_pct <= -LIMIT_CANDIDATE_PCT) downCandidates.push(row.symbol);
      if (row.change_pct !== null && Math.abs(row.change_pct) <= EXCLUDE_ABS_CHANGE_PCT) {
        returns.push(row.change_pct);
      }
    }
    offset += body.items.length;
    if (offset >= total) break;
  }
  const equalWeight = returns.length ? returns.reduce((a, b) => a + b, 0) / returns.length : null;
  return {
    total, up, down, flat, withoutPct, amount, equalWeight, kept: returns.length,
    upCandidates, downCandidates, nullPctCandidates,
  };
}

/**
 * **独立复算涨跌停**：候选集是「涨跌幅 ≥ 4.9% 的标的」（ST 主板 5% 档是最低的一档，
 * 低于 4.9% 不可能封板）；对每个候选取 **raw** 日线，在 JS 里按板别算涨跌停价再比收盘。
 *
 * 为什么不用 `change_pct` 近似：`raw` 口径下它自 2026-09-21 起整列为空，且复权价上
 * 「前收 × 1.1 再舍到分」这个整数关系不成立——本次实测两者差 2 只。
 */
const MAIN_BOARD = ["000", "001", "002", "003", "600", "601", "603", "605"];

/**
 * 板别幅度。**这里没有 ST 档**——主板 ST 实测就是 10%（与主板档同值，2026-10-12 定，
 * 见后端 `_ST_MAIN_BOARD` 的读数表），创业板 / 科创板的 ST 本来也是 20%。
 * 所以复算不需要名称，判据只看代码前缀。
 */
function limitPctOf(symbol) {
  const head = symbol.slice(0, 3);
  if (["300", "301", "302", "688", "689"].includes(head)) return 0.2;
  if (head === "920") return 0.3;
  if (MAIN_BOARD.includes(head)) return 0.1;
  return null; // 未知板别：不算进分子也不算进分母（与后端同一条不对称性）
}

function roundToCent(value) {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

/** 取数窗口：只要「前一交易日 + 当日」两根，故给一个足够跨过长假的日历窗口。 */
function windowStart(date, days = 15) {
  const start = new Date(`${date}T00:00:00Z`);
  start.setUTCDate(start.getUTCDate() - days);
  return start.toISOString().slice(0, 10);
}

/** 上一交易日。取一只交投活跃的标的回看——与后端「只在最近 2 个交易日里找前收」同口径。 */
async function previousSession(context, date) {
  const response = await context.request.get(
    `${API}/api/v1/market/600519/bars?adjust=raw&start=${windowStart(date, 20)}&end=${date}`,
  );
  if (!response.ok()) throw new Error(`取基准标的日线失败 ${response.status()}`);
  const bars = (await response.json()).bars;
  if (bars.length < 2) throw new Error("基准标的取不到两根 bar，无法确定上一交易日");
  return bars[bars.length - 2].time;
}

async function recomputeLimits(context, day, prevSession, candidates) {
  // ⚠ `start` 必须给：不带窗口时 `/bars` 会把 2020 年以来的 raw 分片整个扫一遍，
  // 几百个候选就是几百次全史扫描（第一版这么写，跑了 7 分钟没跑完）。
  const heads = await Promise.all(
    candidates.map(async (symbol) => {
      const response = await context.request.get(
        `${API}/api/v1/market/${symbol}/bars?adjust=raw&start=${prevSession}&end=${day}`,
      );
      if (!response.ok()) return { symbol, state: "no_data" };
      const bars = (await response.json()).bars;
      // 与后端同口径：前收 = **上一个交易日**的收盘；那天没成交就是「判不了」，跳过
      if (bars.length < 2 || bars[bars.length - 2].time !== prevSession) {
        return { symbol, state: "no_prev" };
      }
      const close = bars[bars.length - 1].close;
      const prev = bars[bars.length - 2].close;
      const pct = limitPctOf(symbol);
      if (pct === null || !prev) return { symbol, state: "unknown_board" };
      const up = roundToCent(prev * (1 + pct));
      const down = roundToCent(prev * (1 - pct));
      if (close >= up - 0.005) return { symbol, state: "up" };
      if (close <= down + 0.005) return { symbol, state: "down" };
      return { symbol, state: "none" };
    }),
  );
  return heads;
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  await mkdir(IMAGES, { recursive: true });

  // 浏览器句柄挂在外面：中途抛错时也必须关掉它，否则进程会被未关闭的浏览器吊住不退出
  // （第一版就是这么挂住的——断言失败后终端既不返回也不报错，白等十分钟）
  browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });
  await signIn(context);
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);

  // ── 1. 落地页与搬迁 ───────────────────────────────────────
  console.log("\n[1] 落地页与路由搬迁");
  await page.goto(FRONTEND, { waitUntil: "networkidle" });
  const heading = await page.locator("h1").first().innerText();
  check("`/` 是研究首页", heading.includes("研究首页"), `h1 = ${heading}`);
  const homeActive = await page.getAttribute('[data-nav="home"]', "data-active");
  const homeCurrent = await page.getAttribute('[data-nav="home"]', "aria-current");
  check("页头「首页」高亮且带 aria-current", homeActive === "true" && homeCurrent === "page");
  const chatActive = await page.getAttribute('[data-nav="chat"]', "data-active");
  check("研究首页不点亮「对话」", chatActive === "false");

  // ── 2. 口径字与两个「截至」 ───────────────────────────────
  console.log("\n[2] 口径字");
  const indexCard = await page.locator('[data-card="index"]').innerText();
  check("代理指数卡上写着「非指数」", indexCard.includes("非指数"));
  check("口径小字含「等权代理」与基点", indexCard.includes("等权代理") && indexCard.includes("基点"));
  const bars = await page.locator('[data-stamp="bars"]').innerText();
  const events = await page.locator('[data-stamp="events"]').innerText();
  check("两个「截至」都在页面上", bars.includes("行情截至") && events.includes("事件截至"),
    `${bars.trim()} / ${events.trim()}`);

  // ── 3. 独立复算大盘读数 ───────────────────────────────────
  console.log("\n[3] 大盘读数与独立复算对账");
  const overview = await (await context.request.get(`${API}/api/v1/market/overview`)).json();
  const day = overview.trade_date;
  const mine = await recomputeFromCrossSection(context, day);
  check(
    "涨跌家数一致（独立分页复算）",
    mine.up === overview.breadth.up && mine.down === overview.breadth.down && mine.flat === overview.breadth.flat,
    `复算 ${mine.up}/${mine.down}/${mine.flat} vs 端点 ${overview.breadth.up}/${overview.breadth.down}/${overview.breadth.flat}`,
  );
  check("有价标的数一致", mine.total === overview.breadth.total, `${mine.total} vs ${overview.breadth.total}`);
  check(
    "成交额一致（相对差 < 1e-9）",
    Math.abs(mine.amount - overview.amount) / overview.amount < 1e-9,
    `复算 ${(mine.amount / 1e8).toFixed(1)} 亿 vs 端点 ${(overview.amount / 1e8).toFixed(1)} 亿`,
  );
  check(
    "等权代理当日涨跌一致（独立 JS 复算，容差 1e-9）",
    mine.equalWeight !== null && Math.abs(mine.equalWeight - overview.index.change_pct) < 1e-9,
    `复算 ${mine.equalWeight?.toFixed(4)}% vs 端点 ${overview.index.change_pct.toFixed(4)}%（样本 ${mine.kept}）`,
  );

  // 涨跌停：候选集 = 涨跌幅落在两端 4.9% 以外的标的（低于它不可能封板，最窄的档是
  // ST 主板的 5%），逐个取 raw 的「上一交易日 + 当日」在 JS 里重算涨跌停价
  const prevSession = await previousSession(context, day);
  const candidates = [...mine.upCandidates, ...mine.downCandidates, ...mine.nullPctCandidates];
  console.log(
    `    上一交易日 ${prevSession}；候选 ${candidates.length} 只（涨 ${mine.upCandidates.length} / 跌 ${mine.downCandidates.length} / 无涨跌幅 ${mine.nullPctCandidates.length}），逐个重算…`,
  );
  const judged = await recomputeLimits(context, day, prevSession, candidates);
  const myUp = judged.filter((row) => row.state === "up").length;
  const myDown = judged.filter((row) => row.state === "down").length;
  const mySkipped = judged.filter((row) => row.state === "no_prev").length;
  check(
    "涨停家数一致（候选逐个 raw 重算，含 ST 档）",
    myUp === overview.limits.limit_up,
    `复算 ${myUp} vs 端点 ${overview.limits.limit_up}`,
  );
  check(
    "跌停家数一致（候选逐个 raw 重算，含 ST 档）",
    myDown === overview.limits.limit_down,
    `复算 ${myDown} vs 端点 ${overview.limits.limit_down}`,
  );
  check(
    "候选集里「判不了」的都落在端点的跳过集内（前收口径没跑偏）",
    mySkipped <= overview.limits.skipped,
    `候选里判不了 ${mySkipped} vs 端点全市场跳过 ${overview.limits.skipped}`,
  );
  check(
    "涨跌停的判定数 + 跳过数 = 有价标的数",
    overview.limits.judged + overview.limits.skipped === overview.breadth.total,
    `${overview.limits.judged} + ${overview.limits.skipped} vs ${overview.breadth.total}`,
  );

  // ── 4. 板块块 ─────────────────────────────────────────────
  console.log("\n[4] 板块块");
  const sectors = await (await context.request.get(`${API}/api/v1/market/sectors?min_symbols=10`)).json();
  const coverageText = await page.locator("section", { hasText: "行业 / 概念板块" }).first().innerText();
  const coveragePercent = `${((sectors.coverage.covered / sectors.coverage.total) * 100).toFixed(1)}%`;
  check(
    "覆盖率照登在区块头上",
    coverageText.includes("归属覆盖率") && coverageText.includes(coveragePercent),
    `页面上找 ${coveragePercent}`,
  );

  const host = page.locator('[data-chart="sectors"]');
  const hostWidth = (await host.boundingBox())?.width ?? 0;
  check("图宿主有实际尺寸（零尺寸会让 ECharts 按兜底 100×300 建画布）", hostWidth > 300, `${Math.round(hostWidth)}px`);

  const tone = await barPixelTone(page);
  check(
    "板块条红绿两端像素同时存在（涨跌都画出来了）",
    tone !== null && tone.red > 200 && tone.green > 200,
    tone ? `偏红 ${tone.red} / 偏绿 ${tone.green} 像素` : "取不到宿主",
  );

  const values = sectors.items.map((row) => row.avg_pct);
  const descending = values.every((value, index) => index === 0 || values[index - 1] >= value);
  check("端点返回的板块按平均涨跌幅降序", descending, `首位 ${values[0]} → 末位 ${values.at(-1)}`);

  // 表格孪生：切换后逐行可读，且与图同序
  await page.locator("button", { hasText: "看表格" }).click();
  const table = page.locator('[data-table="sectors"]');
  await table.waitFor();
  const firstIndustry = (await table.locator("tbody tr").first().innerText()).split("\n")[0];
  const rows = await table.locator("tbody tr").count();
  check("表格孪生可切且逐行可读", rows === sectors.items.length, `${rows} 行 vs ${sectors.items.length} 个标签`);
  check("表与图同序（首行就是队首）", firstIndustry.includes(sectors.items[0].industry), firstIndustry.trim());
  await page.locator("button", { hasText: "看图" }).click();

  // ── 5. 样本下限真的改结果 ─────────────────────────────────
  console.log("\n[5] 样本下限");
  await page.locator('select[aria-label="板块成分数下限"]').selectOption("100");
  await page.waitForTimeout(1200);
  const strict = await (await context.request.get(`${API}/api/v1/market/sectors?min_symbols=100`)).json();
  const strictCount = await page.locator('[data-chart="sectors"]').count();
  check(
    "调高样本下限后标签数变少（小样本不再霸榜）",
    strict.count < sectors.count && strictCount === 1,
    `${sectors.count} → ${strict.count}`,
  );
  await page.locator('select[aria-label="板块成分数下限"]').selectOption("10");
  await page.waitForTimeout(1200);

  // ── 6. 对话页仍在，旧深链不再指向它 ───────────────────────
  console.log("\n[6] 搬迁回归");
  await page.goto(`${FRONTEND}/chat`, { waitUntil: "networkidle" });
  const chatHeadingVisible = await page.locator("textarea").count();
  const chatNavActive = await page.getAttribute('[data-nav="chat"]', "data-active");
  check("`/chat` 是对话页（有输入框）且页头点亮「对话」", chatHeadingVisible > 0 && chatNavActive === "true");
  const legacy = await page.goto(`${FRONTEND}/?thread=00000000-0000-0000-0000-000000000000`, {
    waitUntil: "networkidle",
  });
  const legacyHeading = await page.locator("h1").first().innerText();
  check(
    "旧深链 `/?thread=…` 落在研究首页（不再解析成对话）",
    legacy?.status() === 200 && legacyHeading.includes("研究首页"),
    `h1 = ${legacyHeading}`,
  );

  // ── 7. 截图：浅色 / 深色 / 窄屏 ───────────────────────────
  console.log("\n[7] 截图");
  await page.goto(FRONTEND, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: resolve(EVIDENCE, "home-light.png"), fullPage: true });
  await page.screenshot({ path: resolve(IMAGES, "home.png"), fullPage: false });

  await page.emulateMedia({ colorScheme: "dark" });
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await page.waitForTimeout(1500);
  const darkTone = await barPixelTone(page);
  await page.screenshot({ path: resolve(EVIDENCE, "home-dark.png"), fullPage: true });
  check(
    "深色下板块条仍有两端像素（色阶是各自选步，不是自动翻转）",
    darkTone !== null && darkTone.red > 200 && darkTone.green > 200,
    darkTone ? `偏红 ${darkTone.red} / 偏绿 ${darkTone.green}` : "取不到宿主",
  );
  await page.evaluate(() => document.documentElement.classList.remove("dark"));
  await page.emulateMedia({ colorScheme: "light" });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(FRONTEND, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check("窄屏 390px 无横向溢出", overflow <= 1, `溢出 ${overflow}px`);
  await page.screenshot({ path: resolve(EVIDENCE, "home-narrow.png"), fullPage: true });

  await browser.close();
  browser = null;

  const passed = RESULTS.filter((row) => row.ok).length;
  const summary = { passed, total: RESULTS.length, results: RESULTS, blockers: BLOCKERS };
  await writeFile(resolve(EVIDENCE, "ui.json"), `${JSON.stringify(summary, null, 2)}\n`);
  await writeFile(
    resolve(EVIDENCE, "ui.md"),
    [
      "# M10a 界面验证",
      "",
      `结果：**${passed} / ${RESULTS.length}**`,
      "",
      "| # | 断言 | 结果 | 证据 |",
      "|---|---|---|---|",
      ...RESULTS.map((row, i) => `| ${i + 1} | ${row.label} | ${row.ok ? "✅" : "❌"} | ${row.detail} |`),
      "",
      BLOCKERS.length ? `断点：${BLOCKERS.map((b) => `${b.where}：${b.detail}`).join("；")}` : "无断点。",
      "",
    ].join("\n"),
  );

  console.log(`\n${passed} / ${RESULTS.length} 通过，证据落 logs/m10/`);
  if (passed !== RESULTS.length) process.exitCode = 1;
}

main().catch(async (error) => {
  console.error("走查中断：", error);
  blocker("脚本", String(error));
  // 不关浏览器，Node 会一直等它——表现是「跑挂了但终端不返回」
  await browser?.close().catch(() => undefined);
  const passed = RESULTS.filter((row) => row.ok).length;
  await mkdir(EVIDENCE, { recursive: true });
  await writeFile(
    resolve(EVIDENCE, "ui.json"),
    `${JSON.stringify({ passed, total: RESULTS.length, results: RESULTS, blockers: BLOCKERS }, null, 2)}\n`,
  );
  process.exitCode = 1;
});
