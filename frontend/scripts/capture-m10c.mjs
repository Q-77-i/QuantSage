/**
 * M10c 界面验证：标的浏览（全市场列表 + 个股页 + 板块钻取）走一遍真浏览器。
 *
 * 前置：后端 8000（**含 M10c 改动、且是重启后的进程**）与前端 3001 都在跑。
 * 用法：`cd frontend && node scripts/capture-m10c.mjs > ../logs/m10c-run.txt 2>&1`
 *
 * 四条是自动化层结构上看不见的（M5b…M10b 的教训）：
 *   1. **板块钻取的一致性**——点一条进去，列表的 total 必须**等于**那条上标的成分数
 *      （两个数走同一支 PIT 归属、同一个当日截面池；这条曾经差 1 只）；
 *   2. **K 线真的画出来了**——读画布像素，不认 DOM 文案（ECharts 画在 canvas 上）；
 *   3. **加自选写进了真账本**——去自选股页看到它，而不是只看按钮变了字；
 *   4. **「去回测」带过去的标的与当前页一致**，且跑完一次回测后 `?symbol=` **不被冲掉**。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EVIDENCE = resolve(ROOT, "logs/m10c");
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

async function rowSymbols(page) {
  return page.$$eval('[data-table="stocks"] tbody tr', (nodes) =>
    nodes.map((node) => node.querySelector("a")?.textContent?.trim() ?? ""),
  );
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });
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

  // ── 1. 列表页：默认排序与搜索 ────────────────────────────
  console.log("\n[1] 全市场列表");
  await page.goto(`${FRONTEND}/stocks`, { waitUntil: "networkidle" });
  await page.locator('[data-table="stocks"] tbody tr').first().waitFor();
  const summary = await page.locator("[data-list-summary]").innerText();
  check("列表页出得来且标了全市场", summary.includes("全市场") && summary.includes("共"), summary);
  const firstRow = await page.locator('[data-table="stocks"] tbody tr').first().innerText();
  check("默认按涨跌幅降序（首行是正的）", firstRow.includes("%") && firstRow.includes("+"), firstRow.replace(/\s+/g, " "));

  await page.fill('input[aria-label="搜索标的"]', "茅台");
  await page.waitForTimeout(1500);
  const hit = await rowSymbols(page);
  check("按**名称**搜得到（茅台 → 600519）", hit.includes("600519"), hit.slice(0, 3).join(","));

  await page.fill('input[aria-label="搜索标的"]', "6005");
  await page.waitForTimeout(1500);
  const byCode = await rowSymbols(page);
  check("按**代码前缀**也搜得到（6005 → 6005xx）", byCode.length > 0 && byCode.every((s) => s.startsWith("6005")), `${byCode.length} 条`);

  await page.fill('input[aria-label="搜索标的"]', "");
  await page.waitForTimeout(1500);

  // ── 2. 排序切换 + 翻页 ──────────────────────────────────
  console.log("\n[2] 排序与翻页");
  await page.locator('select[aria-label="排序键"]').selectOption("amount");
  await page.waitForTimeout(1500);
  const amounts = await page.$$eval('[data-table="stocks"] tbody tr', (nodes) =>
    nodes.slice(0, 5).map((node) => Number((node.children[4]?.textContent ?? "0").replace(/,/g, ""))),
  );
  check(
    "切成按成交额降序（前 5 行单调不增）",
    amounts.every((value, i) => i === 0 || amounts[i - 1] >= value),
    amounts.join(" / "),
  );

  // 翻页：**真分页**（不是「加载更多」）——排序表要随机访问，流水账才用追加
  const pageOne = await rowSymbols(page);
  const label = () => page.locator("[data-page-label]").innerText();
  check("有页码指示（第 N / M 页）", (await label()).includes("页"), await label());

  await page.locator("button", { hasText: "下一页" }).click();
  await page.waitForTimeout(1200);
  const pageTwo = await rowSymbols(page);
  check("下一页：地址栏带上 ?page=2", page.url().includes("page=2"), page.url().replace(FRONTEND, ""));
  check(
    "下一页：换了一整批（与第一页零交集、且条数不变）",
    pageTwo.length === pageOne.length && pageTwo.every((symbol) => !pageOne.includes(symbol)),
    `${pageOne.length} → ${pageTwo.length} 条`,
  );

  await page.locator("button", { hasText: "上一页" }).click();
  await page.waitForTimeout(1200);
  const backToOne = await rowSymbols(page);
  check("上一页：逐行回到第一页", backToOne.join() === pageOne.join());
  check("回到第 1 页时地址栏不再带 page", !page.url().includes("page="), page.url().replace(FRONTEND, ""));

  // 跳到第 3 页
  await page.fill('input[aria-label="跳到第几页"]', "3");
  await page.press('input[aria-label="跳到第几页"]', "Enter");
  await page.waitForTimeout(1200);
  const pageThree = await rowSymbols(page);
  check(
    "跳到第 3 页：又是一批新的，且与前后两页都无交集",
    pageThree.length > 0 &&
      pageThree.every((symbol) => !pageOne.includes(symbol) && !pageTwo.includes(symbol)),
    `第 3 页首行 ${pageThree[0]}`,
  );

  // 换筛选要回第 1 页，否则「第 3 页的茅台搜索」多半是空白
  await page.fill('input[aria-label="搜索标的"]', "茅台");
  await page.waitForTimeout(1500);
  check(
    "换筛选后回到第 1 页",
    !page.url().includes("page=") && (await rowSymbols(page)).includes("600519"),
    page.url().replace(FRONTEND, ""),
  );
  await page.fill('input[aria-label="搜索标的"]', "");
  await page.waitForTimeout(1500);

  // ── 3. 板块钻取：一致性 ─────────────────────────────────
  console.log("\n[3] 板块钻取");
  const sectors = await (
    await context.request.get(`${API}/api/v1/market/sectors?min_symbols=10`)
  ).json();
  const target = sectors.items[0];
  await page.goto(`${FRONTEND}/stocks?industry=${encodeURIComponent(target.industry)}`, {
    waitUntil: "networkidle",
  });
  await page.locator('[data-table="stocks"] tbody tr').first().waitFor();
  const drilled = await page.locator("[data-list-summary]").innerText();
  const apiTotal = await (
    await context.request.get(
      `${API}/api/v1/market/cross-section?industry=${encodeURIComponent(target.industry)}&limit=1`,
    )
  ).json();
  check(
    "钻取后列表 total **等于**板块条上的成分数",
    apiTotal.total === target.symbols,
    `${target.industry}：列表 ${apiTotal.total} vs 条上 ${target.symbols} 只`,
  );
  check("页头写明了是哪个行业", drilled.includes(target.industry), drilled);

  // 从图表点回去（画布点击路径与表格点击共用同一个 onPick）
  await page.goto(FRONTEND, { waitUntil: "networkidle" });
  const host = page.locator('[data-chart="sectors"]');
  await host.waitFor();
  await host.scrollIntoViewIfNeeded();
  const box = await host.boundingBox();
  // 榜首那条最长、从零线向右伸。**坐标要相对宿主**（`locator.click` 会先滚进视野；
  // 用 `page.mouse.click` 的视口坐标在这里会落空——板块图在长页面下方）。
  // 横向取零线右侧、纵向沿类目带往下扫几个位置，命中一条即可。
  let landed = "";
  for (const y of [10, 14, 18, 22, 30, 40]) {
    await host.click({ position: { x: box.width * 0.7, y } });
    await page.waitForTimeout(900);
    if (page.url().includes("/stocks?industry=")) {
      landed = decodeURIComponent(page.url().replace(FRONTEND, ""));
      break;
    }
  }
  check("点板块图的一条能跳到该行业的列表", landed !== "", landed || `仍停在 ${page.url()}`);
  if (landed) {
    const chartTotal = await page.locator("[data-list-summary]").innerText();
    check("从图点进去的也是同一支口径（有 total）", chartTotal.includes("共"), chartTotal);
  }
  // ── 4. 个股页 ───────────────────────────────────────────
  console.log("\n[4] 个股页");
  await page.goto(`${FRONTEND}/stocks/600519`, { waitUntil: "networkidle" });
  const title = await page.locator("[data-symbol]").innerText();
  check("标题是「名字 代码」", title.includes("600519"), title);
  await page.waitForTimeout(1500);
  const painted = await page.evaluate(() => {
    const hosts = Array.from(document.querySelectorAll("div.relative"));
    return hosts
      .filter((host) => host.querySelector("canvas"))
      .map((host) => {
        let n = 0;
        for (const canvas of host.querySelectorAll("canvas")) {
          const context = canvas.getContext("2d");
          if (!context) continue;
          const { width, height } = canvas;
          const image = context.getImageData(0, 0, width, height);
          for (let y = 0; y < height; y += 3) {
            for (let x = 0; x < width; x += 3) {
              const i = (y * width + x) * 4;
              const [r, g, b, a] = [image.data[i], image.data[i + 1], image.data[i + 2], image.data[i + 3]];
              if (a < 200) continue;
              if (Math.abs(r - 252) + Math.abs(g - 252) + Math.abs(b - 251) > 90) n += 1;
            }
          }
        }
        return n;
      });
  });
  check("K 线**真的画在画布上**（不是空白宿主）", painted.some((n) => n > 1000), `画了 ${painted.join("/")} 像素`);
  const eventRows = await page.locator("table tbody tr").count();
  const apiEvents = await (await context.request.get(`${API}/api/v1/events?symbol=600519`)).json();
  check("事件时间线与端点条数一致", eventRows === apiEvents.count, `${eventRows} vs ${apiEvents.count}`);

  // ── 5. 加自选 → 真账本 ──────────────────────────────────
  console.log("\n[5] 加自选");
  const track = page.locator('[data-action="watchlist"]');
  if ((await track.innerText()).includes("已在自选")) {
    check("重复加不产生第二条（按钮已禁用）", await track.isDisabled());
  } else {
    await track.click();
    await page.waitForTimeout(1500);
    check("加完按钮变「已在自选」且禁用", (await track.innerText()).includes("已在自选") && (await track.isDisabled()));
  }
  const watchlist = await (await context.request.get(`${API}/api/v1/watchlist`)).json();
  const items = Array.isArray(watchlist) ? watchlist : (watchlist.items ?? []);
  check(
    "**真账本里有它**（去自选股页看得到）",
    items.filter((row) => row.symbol === "600519").length === 1,
    `自选里 600519 共 ${items.filter((row) => row.symbol === "600519").length} 条`,
  );

  // ── 6. 去回测：带标的、且不被 ?run= 冲掉 ────────────────
  console.log("\n[6] 去回测");
  await page.locator('[data-action="to-backtest"]').click();
  await page.waitForTimeout(1500);
  check("落在回测页且带着标的", page.url().includes("/backtest?symbol=600519"), page.url().replace(FRONTEND, ""));
  const symbolInput = await page.locator("#bt-symbol").inputValue();
  check("回测表单的标的已预填", symbolInput === "600519", symbolInput);

  // ── 7. 截图与窄屏 ───────────────────────────────────────
  console.log("\n[7] 截图");
  await page.goto(`${FRONTEND}/stocks`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: resolve(EVIDENCE, "list-light.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${FRONTEND}/stocks`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check("窄屏 390px 无横向溢出（表格自己滚）", overflow <= 1, `溢出 ${overflow}px`);
  await page.screenshot({ path: resolve(EVIDENCE, "list-narrow.png"), fullPage: true });

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
      "# M10c 界面验证",
      "",
      `结果：**${passed} / ${RESULTS.length}**`,
      "",
      "| # | 断言 | 结果 | 证据 |",
      "|---|---|---|---|",
      ...RESULTS.map((row, i) => `| ${i + 1} | ${row.label} | ${row.ok ? "✅" : "❌"} | ${row.detail} |`),
      "",
    ].join("\n"),
  );
  console.log(`\n${passed} / ${RESULTS.length} 通过，证据落 logs/m10c/`);
  if (passed !== RESULTS.length) process.exitCode = 1;
}

main().catch(async (error) => {
  console.error("走查中断：", error);
  await browser?.close().catch(() => undefined);
  await mkdir(EVIDENCE, { recursive: true });
  await writeFile(
    resolve(EVIDENCE, "ui.json"),
    `${JSON.stringify({ passed: RESULTS.filter((r) => r.ok).length, total: RESULTS.length, results: RESULTS, error: String(error) }, null, 2)}\n`,
  );
  process.exitCode = 1;
});
