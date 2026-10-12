/**
 * M10b 界面验证：事件日历 / 快讯流走一遍真浏览器。
 *
 * 前置：后端 8000（**含 M10b 端点、且是重启后的进程**——无 `--reload`，改完必须重启）与前端 3001 都在跑。
 * 用法：`cd frontend && node scripts/capture-m10b.mjs > ../logs/m10b-run.txt 2>&1`
 *
 * 五条是自动化层结构上看不见的（M5b/M5c/M6/M7/M8/M10a 的教训）：
 *   1. **默认口径真的生效**——「快讯（除公告）」下流里**一条公告都没有**（这是本轮的核心设计，
 *      不筛就没有「流」，只有一张公告列表）；
 *   2. **两个时间戳与滞后都在 DOM 里**——PRD 硬条款，且滞后是这个项目的护城河展示位；
 *   3. **来源三元组展开后三件都在**（`source` / `original_source` / `content_hash`）；
 *   4. **翻页不重不漏**——追加后逐条键唯一，且条数真的变多；
 *   5. 日期带计数与快讯流 total 对得上（同一把尺子的两半）。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EVIDENCE = resolve(ROOT, "logs/m10b");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m10-ui@example.com";
const PASSWORD = "m10-ui-pass";

const RESULTS = [];
let browser = null;

function check(label, ok, detail = "") {
  RESULTS.push({ label, ok, detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
  });
  const register = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!register.ok() && register.status() !== 409) throw new Error(`注册失败 ${register.status()}`);
  const login = await context.request.post(`${API}/api/v1/auth/login`, {
    data: { email: EMAIL, password: PASSWORD, remember: true },
  });
  if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);

  const page = await context.newPage();
  page.setDefaultTimeout(20_000);

  const items = () => page.locator('[data-feed="items"] [data-event]');
  const types = () =>
    page.$$eval('[data-feed="items"] [data-event]', (nodes) =>
      nodes.map((node) => node.dataset.type),
    );

  // ── 1. 块在页面上，日期带画出来了 ─────────────────────────
  console.log("\n[1] 事件日历块");
  await page.goto(FRONTEND, { waitUntil: "networkidle" });
  const strip = page.locator('[data-strip="calendar"] [data-day]');
  await strip.first().waitFor();
  const stripCount = await strip.count();
  check("日期带画出来了（近 30 日）", stripCount >= 25, `${stripCount} 根柱`);
  // 柱高**真的在编码条数**——实测单日 322–6,512 条，差二十倍；等高就是没画出来
  const heights = await page.$$eval('[data-strip="calendar"] [data-day] > span > span', (nodes) =>
    nodes.map((node) => Math.round(node.getBoundingClientRect().height)),
  );
  const tallest = Math.max(...heights);
  const shortest = Math.min(...heights);
  check(
    "柱高按条数分档（最高 ≥ 最短的 3 倍）",
    heights.length > 0 && tallest >= shortest * 3,
    `最高 ${tallest}px / 最短 ${shortest}px`,
  );

  const selected = await page.$$eval('[data-strip="calendar"] [data-day]', (nodes) =>
    nodes.filter((node) => node.dataset.selected === "true").map((node) => node.dataset.day),
  );
  check("缺省选中了**某一天**且只有一天", selected.length === 1, selected[0] ?? "无");
  const theDay = selected[0];

  // ── 2. 默认口径：快讯（除公告）─────────────────────────────
  console.log("\n[2] 默认口径：快讯（除公告）");
  await items().first().waitFor();
  const defaultTypes = await types();
  check(
    "默认流里**一条公告都没有**",
    defaultTypes.length > 0 && !defaultTypes.includes("announcement"),
    `首屏 ${defaultTypes.length} 条，类型 ${[...new Set(defaultTypes)].join("/")}`,
  );
  const typeSelect = page.locator('select[aria-label="事件类型"]');
  check("类型筛选的缺省档是「快讯（除公告）」", (await typeSelect.inputValue()) === "feed");

  // ── 3. 双时间戳与滞后在 DOM 里 ────────────────────────────
  console.log("\n[3] 双时间戳与可见滞后");
  const first = items().first();
  const eventStamp = await first.locator('[data-stamp="event"]').innerText();
  const availableStamp = await first.locator('[data-stamp="available"]').innerText();
  const lag = await first.locator("[data-lag]").innerText();
  check("每条的『事发』在", eventStamp.includes("事发"));
  check("每条的『可得』在", availableStamp.includes("可得"));
  check("两者之差（滞后）在", lag.trim().length > 0 && lag !== "—", lag.trim());

  // ── 4. 来源三元组展开 ────────────────────────────────────
  console.log("\n[4] 来源三元组");
  const toggle = first.locator('[data-toggle="sources"]');
  check("展开前三元组不在 DOM 里（不是藏在 hidden 里）", (await first.locator("dl").count()) === 0);
  await toggle.click();
  const rows = await first.locator("dl").innerText();
  check(
    "展开后来源三元组三件齐",
    rows.includes("来源") && rows.includes("原始来源") && rows.includes("内容哈希"),
    rows.replace(/\n+/g, " | ").slice(0, 90),
  );
  const hashText = await first.locator("dd.font-mono").first().innerText();
  check("内容哈希做了截断（不是 64 位全文）", hashText.length > 0 && hashText.length <= 16, hashText);
  await toggle.click();

  // ── 5. 翻页：真分页，换一整批 ────────────────────────────
  console.log("\n[5] 翻页");
  const pageOne = await page.$$eval('[data-feed="items"] [data-event]', (nodes) =>
    nodes.map((node) => node.dataset.event),
  );
  const label = await page.locator("[data-page-label]").innerText();
  check("有页码指示（第 N / M 页）", label.includes("页"), label);
  await page.locator('[data-pager] button', { hasText: "下一页" }).click();
  await page.waitForTimeout(1200);
  const pageTwo = await page.$$eval('[data-feed="items"] [data-event]', (nodes) =>
    nodes.map((node) => node.dataset.event),
  );
  check(
    "下一页：换了一整批（与第一页零交集、且条数不变）",
    pageTwo.length === pageOne.length && pageTwo.every((key) => !pageOne.includes(key)),
    `${pageOne.length} → ${pageTwo.length} 条`,
  );
  await page.locator('[data-pager] button', { hasText: "上一页" }).click();
  await page.waitForTimeout(1200);
  const backToOne = await page.$$eval('[data-feed="items"] [data-event]', (nodes) =>
    nodes.map((node) => node.dataset.event),
  );
  check("上一页：逐条回到第一页", backToOne.join() === pageOne.join());

  // ── 6. 切筛选：公告档真的全是公告 ─────────────────────────
  console.log("\n[6] 切到「公告」");
  await typeSelect.selectOption("announcement");
  await page.waitForTimeout(1200);
  const announcementTypes = await types();
  check(
    "「公告」档下**全是**公告",
    announcementTypes.length > 0 && announcementTypes.every((type) => type === "announcement"),
    `${announcementTypes.length} 条`,
  );
  await typeSelect.selectOption("feed");
  await page.waitForTimeout(1200);

  // ── 7. 日期带与流对得上（同一把尺子的两半）────────────────
  console.log("\n[7] 日期带计数 vs 快讯流 total");
  const calendar = await (await context.request.get(`${API}/api/v1/events/calendar`)).json();
  const apiAll = await (
    await context.request.get(
      `${API}/api/v1/events/feed?date=${theDay}&types=news&types=policy&types=person&types=research&limit=1`,
    )
  ).json();
  const dayRow = calendar.days.find((row) => row.date === theDay);
  const byType = dayRow?.by_type ?? {};
  const derived = ["news", "policy", "person", "research"].reduce(
    (sum, type) => sum + (byType[type] ?? 0),
    0,
  );
  check(
    "日期带那天的计数（去掉公告）= 快讯流 total",
    derived === apiAll.total,
    `日期带推得 ${derived} vs feed.total ${apiAll.total}`,
  );
  check("该日确实有公告（否则第 6 条断言是空的）", (byType.announcement ?? 0) > 0, `${byType.announcement} 条`);

  // ── 8. 切一天，流跟着变 ──────────────────────────────────
  console.log("\n[8] 切日期");
  const otherDay = await page.$$eval('[data-strip="calendar"] [data-day]', (nodes) =>
    nodes.length > 10 ? nodes[Math.floor(nodes.length / 3)].dataset.day : null,
  );
  if (otherDay && otherDay !== theDay) {
    await page.locator(`[data-day="${otherDay}"]`).click();
    await page.waitForTimeout(1500);
    const selectedNow = await page.$$eval('[data-strip="calendar"] [data-day]', (nodes) =>
      nodes.filter((node) => node.dataset.selected === "true").map((node) => node.dataset.day),
    );
    check("选中态跟着切", selectedNow[0] === otherDay, `${theDay} → ${selectedNow[0]}`);
    const dateInput = await page.locator('input[aria-label="跳到某一天"]').inputValue();
    check("原生日期框同步到新选中日", dateInput === otherDay, dateInput);
  } else {
    check("找到另一天可切", false, String(otherDay));
  }

  // ── 9. 截图与窄屏 ────────────────────────────────────────
  console.log("\n[9] 截图");
  await page.goto(FRONTEND, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.locator('[data-strip="calendar"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(EVIDENCE, "feed-light.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(FRONTEND, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check("窄屏 390px 无横向溢出（日期带自己滚）", overflow <= 1, `溢出 ${overflow}px`);
  await page.screenshot({ path: resolve(EVIDENCE, "feed-narrow.png"), fullPage: true });

  await browser.close();
  browser = null;

  const passed = RESULTS.filter((row) => row.ok).length;
  await writeFile(
    resolve(EVIDENCE, "ui.json"),
    `${JSON.stringify({ passed, total: RESULTS.length, results: RESULTS }, null, 2)}\n`,
  );
  await writeFile(
    resolve(EVIDENCE, "ui.md"),
    [
      "# M10b 界面验证",
      "",
      `结果：**${passed} / ${RESULTS.length}**`,
      "",
      "| # | 断言 | 结果 | 证据 |",
      "|---|---|---|---|",
      ...RESULTS.map((row, i) => `| ${i + 1} | ${row.label} | ${row.ok ? "✅" : "❌"} | ${row.detail} |`),
      "",
    ].join("\n"),
  );
  console.log(`\n${passed} / ${RESULTS.length} 通过，证据落 logs/m10b/`);
  if (passed !== RESULTS.length) process.exitCode = 1;
}

main().catch(async (error) => {
  console.error("走查中断：", error);
  await browser?.close().catch(() => undefined);
  const passed = RESULTS.filter((row) => row.ok).length;
  await mkdir(EVIDENCE, { recursive: true });
  await writeFile(
    resolve(EVIDENCE, "ui.json"),
    `${JSON.stringify({ passed, total: RESULTS.length, results: RESULTS, error: String(error) }, null, 2)}\n`,
  );
  process.exitCode = 1;
});
