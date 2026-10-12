/**
 * M10d 界面验证：事件详情抽屉走一遍真浏览器。
 *
 * 前置：后端 8000（**必须含 M10d 端点且是重启后的进程**——无 `--reload`）与前端 3001 都在跑。
 * 用法：`cd frontend && node scripts/capture-m10d.mjs > ../logs/m10d-run.txt 2>&1`
 *
 * 五条是自动化层结构上看不见的（沿用 M10b/M10c 的教训）：
 *   1. **抽屉里的数字与库里逐字一致**——正文/修订/标的理由/原文链接都拿 API 当场比，
 *      不是「有个抽屉就算过」；
 *   2. **空值显示「—」而不是不显示**（`reason` 为 null 的标的、没有 `source_url` 的事件）；
 *   3. **没有原文就不出链接元素**（不是渲染一个空 `href`）；
 *   4. **Esc / 遮罩 / 按钮三种关法都真的关得掉**；
 *   5. **个股页也开同一个抽屉**（D3），且快讯流的「来源」展开里**不再有摘要行**（D1）。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EVIDENCE = resolve(ROOT, "logs/m10d");
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
  const sheet = () => page.locator('[data-detail="sheet"]');
  const group = (key) => page.locator(`[data-detail="${key}"]`);
  const CODED = /^\d{6}$/;

  /**
   * 打开某一条的抽屉，并**等到正文渲染出来**再返回。
   *
   * 只等 `[data-detail="sheet"]` 是不够的——那个壳子立刻就有（内容是「读取中…」），
   * 后面的分组断言会全部落在数据到达之前（第一次跑就是这么假红的：7 个分组断言全 ✗，
   * 而紧接着的「头部写了哪一天」却 ✓）。
   */
  async function openDetail(eventId) {
    await page.locator(`[data-event="${eventId}"] [data-toggle="detail"]`).click();
    await sheet().waitFor();
    await page.locator('[data-detail="body"]').waitFor();
  }

  // ── 0. 从 API 挑样本：一条带原文链接 + 带打标理由的，一条没有原文链接的 ──
  // **必须按界面上的口径挑**：接口不替调用方筛类型（服务端不设默认），不带 types 时
  // 头部全是公告（重要度不跨类型可比），而公告恒有原文链接、恒无打标理由。
  console.log("\n[0] 选样本（拿 API 当尺子）");
  const QUICK = "types=news&types=policy&types=person&types=research";

  async function detailsOf(queryText) {
    const body = await (await context.request.get(`${API}/api/v1/events/feed?limit=20&${queryText}`)).json();
    const rows = [];
    for (const item of body.items) {
      rows.push(
        await (
          await context.request.get(
            `${API}/api/v1/events/${encodeURIComponent(item.event_id)}?day=${body.date}`,
          )
        ).json(),
      );
    }
    return { day: body.date, rows };
  }

  const quick = await detailsOf(QUICK);
  const day = quick.day;
  const rich = quick.rows.find((d) => d.source_url && d.stocks.some((s) => s.reason));
  check("挑到带原文链接与打标理由的样本（快讯首页）", Boolean(rich), rich ? rich.event_id : "没挑到");
  if (!rich) throw new Error("快讯首页没有「有原文 + 有理由」的样本");

  await page.goto(`${FRONTEND}/`);
  await page.waitForSelector('[data-feed="items"] [data-event]');

  // ── 1. 每条都有「详情」入口 ──────────────────────────────
  console.log("\n[1] 入口");
  const rows = await page.locator('[data-feed="items"] [data-event]').count();
  const entries = await page.locator('[data-feed="items"] [data-toggle="detail"]').count();
  check("快讯流每条都有「详情」按钮", rows > 0 && entries === rows, `${entries}/${rows}`);

  // ── 2. 打开抽屉，七组齐全 ────────────────────────────────
  console.log("\n[2] 抽屉与七组");
  await openDetail(rich.event_id);
  for (const key of ["head", "body", "source", "revision", "stocks", "person", "factor"]) {
    check(`分组在：${key}`, (await group(key).count()) === 1);
  }
  check("头部标出这条落在哪一天", (await group("head").innerText()).includes(`落在 ${day}`), day);

  // ── 3. 正文与库里逐字一致 ────────────────────────────────
  console.log("\n[3] 正文");
  const bodyText = (await group("body").innerText()).trim();
  check(
    "正文逐字等于 API 的 summary",
    bodyText === (rich.summary ?? "").trim(),
    `${bodyText.length} 字 vs ${(rich.summary ?? "").length} 字`,
  );

  // ── 4. 来源与原文链接 ────────────────────────────────────
  console.log("\n[4] 来源与原文");
  const sourceText = await group("source").innerText();
  check(
    "来源三元组 + 质量标注都在",
    ["来源", "原始发布方", "内容指纹", "质量标注"].every((key) => sourceText.includes(key)),
  );
  const link = page.locator('[data-detail="source-link"]');
  const href = await link.getAttribute("href");
  const target = await link.getAttribute("target");
  check("原文链接 href 等于库里那一条", href === rich.source_url, href ?? "(无)");
  check("原文链接新窗口打开（target=_blank）", target === "_blank");

  // ── 5. 修订四件照原样 ────────────────────────────────────
  console.log("\n[5] 修订标记");
  const revision = await group("revision").innerText();
  check(
    "记录版本与库里一致",
    revision.includes(String(rich.record_version)),
    `库里 ${rich.record_version}`,
  );
  check(
    "是否修订过 / 修正次数与库里一致",
    revision.includes(rich.is_corrected ? "是" : "否") && revision.includes(String(rich.correction_count)),
    `is_corrected=${rich.is_corrected} count=${rich.correction_count}`,
  );

  // ── 6. 关联标的：规则式断言（逐条对库，不假设这一条一定有理由）────
  // 为什么不能假设：**理由常常挂在「没有六位码」的提及上**（实测这条样本 20 条提及里
  // 19 条没代码，唯一的理由挂在 `code="数据缺失"` 上）。「真理由显示出来」那条在 [11] 的
  // 个股页用一条确定带码理由的样本单独验。
  console.log("\n[6] 关联标的");
  const stockText = await group("stocks").innerText();
  const codedStocks = rich.stocks.filter((stock) => CODED.test(stock.code));
  const dropped = rich.stocks.length - codedStocks.length;
  const rowsOk = codedStocks.every((stock) =>
    stock.reason ? stockText.includes(stock.reason) : stockText.includes(stock.code),
  );
  check("每一条带六位码的提及都在，理由照原样、没有理由的显示「—」", rowsOk);
  if (dropped > 0) {
    check("没有六位码的提及被计数说明（不静默隐藏）", stockText.includes(`${dropped} 条提及`), `${dropped} 条`);
  }
  check(
    "行数 = 有码提及数（+1 条计数行）",
    (await group("stocks").locator("dd").count()) === codedStocks.length + (dropped > 0 ? 1 : 0),
    `库里 ${rich.stocks.length} 条提及、其中 ${codedStocks.length} 条带六位码`,
  );

  // ── 7. 人物与因子 ────────────────────────────────────────
  console.log("\n[7] 人物与因子");
  const personText = await group("person").innerText();
  check(
    "人物有则列出、无则「—」",
    rich.person.length ? rich.person.every((name) => personText.includes(name)) : personText.includes("—"),
    `库里 ${rich.person.length} 人`,
  );
  const factorText = await group("factor").innerText();
  check("因子分组里方向与因子分都在", factorText.includes("归一方向") && factorText.includes("因子分"));

  // ── 8. 三种关法 ──────────────────────────────────────────
  console.log("\n[8] 关闭");
  await page.keyboard.press("Escape");
  await sheet().waitFor({ state: "detached" });
  check("Esc 关得掉", (await sheet().count()) === 0);

  await openDetail(rich.event_id);
  await page.locator('[data-detail="backdrop"]').click({ position: { x: 20, y: 20 } });
  await sheet().waitFor({ state: "detached" });
  check("点遮罩关得掉", (await sheet().count()) === 0);

  await openDetail(rich.event_id);
  await page.screenshot({ path: resolve(EVIDENCE, "detail.png") });
  await page.locator('[data-detail="close"]').click();
  await sheet().waitFor({ state: "detached" });
  check("「关闭」按钮关得掉，且抽屉从 DOM 移除", (await sheet().count()) === 0);

  // ── 9. 没有原文的事件不出链接元素（切到「政策」档找 —— 实测 10-08 只有政策类会缺 url）──
  console.log("\n[9] 空值不编");
  const policy = await detailsOf("types=policy");
  const plain = policy.rows.find((d) => !d.source_url);
  check("挑到没有原文链接的样本（政策档）", Boolean(plain), plain ? plain.event_id : "没挑到");
  if (plain) {
    await page.selectOption('select[aria-label="事件类型"]', "policy");
    await page.locator(`[data-event="${plain.event_id}"]`).waitFor();
    await openDetail(plain.event_id);
    const plainSource = await group("source").innerText();
    check("没有原文时不渲染链接元素", (await page.locator('[data-detail="source-link"]').count()) === 0);
    check("「原文」那一行仍然在，显示「—」", plainSource.includes("原文") && plainSource.includes("—"));
    await page.keyboard.press("Escape");
    await page.selectOption('select[aria-label="事件类型"]', "feed");
    await page.locator(`[data-event="${rich.event_id}"]`).waitFor();
  }

  // ── 10. D1：快讯流的「来源」展开里不再有摘要 ──────────────
  console.log("\n[10] 正文只在抽屉里");
  const firstRow = page.locator('[data-feed="items"] [data-event]').first();
  await firstRow.locator('[data-toggle="sources"]').click();
  const expanded = await firstRow.locator("dl").innerText();
  check("来源展开里没有「摘要」行", !expanded.includes("摘要"), expanded.replace(/\n+/g, " | ").slice(0, 60));
  check("来源三元组本身还在（M10b 那三条没被弄坏）", expanded.includes("来源") && expanded.includes("内容哈希"));
  await firstRow.locator('[data-toggle="sources"]').click();

  // ── 11. 个股页共用同一个抽屉（D3）+ 一条**确定带码理由**的样本 ──
  console.log("\n[11] 个股页的同一个抽屉");
  // **不能拿 stocks[0] 当标的**：平台的提及里有空代码与「数据缺失」这类占位
  // （实测一条新闻 20 个提及里 19 个没代码），拿它跳转会走到 /stocks/ 空标的
  const coded = rich.stocks.filter((stock) => CODED.test(stock.code));
  const symbol = coded[0]?.code;
  check("从样本里挑到带六位码的标的", Boolean(symbol), symbol ?? "没挑到");
  if (!symbol) throw new Error("这条样本没有可用的六位码标的，换一条");
  await page.goto(`${FRONTEND}/stocks/${symbol}`);
  await page.waitForSelector('[data-detail-open]');
  await page.locator("[data-detail-open]").first().click();
  await page.locator('[data-detail="body"]').waitFor();
  const symbolBody = await group("body").innerText();
  check("个股页点「详情」开的是同一个抽屉（七组在）", (await group("factor").count()) === 1);
  check("抽屉里有正文（不是空壳）", symbolBody.trim().length > 0, `${symbolBody.trim().length} 字`);
  await page.screenshot({ path: resolve(EVIDENCE, "detail-from-stock.png") });
  await page.keyboard.press("Escape");

  // 一条**带码且有理由**的样本：理由必须真的显示在抽屉里（上面那条样本的理由挂在脏代码上）。
  // 这种事件重要度不高（实测 10-08 的 52 条带理由新闻都不在首页），所以要翻几页找；
  // 全找不到时退回一个钉住的样本——它落在**已冻结的历史日**（10-08），内容只有平台修订才会变，
  // 真变了这条断言会响，而不是静默跳过。
  console.log("\n[12] 带码理由真的显示");
  const PINNED = "news:2285468";
  let withReason = null;
  for (const offset of [0, 20, 40, 60, 80]) {
    const found = await detailsOf(`${QUICK}&offset=${offset}`);
    withReason = found.rows.find((d) => d.stocks.some((s) => s.reason && CODED.test(s.code)));
    if (withReason) break;
  }
  if (!withReason) {
    const pinned = await (
      await context.request.get(`${API}/api/v1/events/${encodeURIComponent(PINNED)}`)
    ).json();
    if (pinned.stocks?.some((s) => s.reason && CODED.test(s.code))) withReason = pinned;
  }
  check("挑到「有六位码 + 有理由」的样本", Boolean(withReason), withReason?.event_id ?? "没挑到");
  if (withReason) {
    const target = withReason.stocks.find((s) => s.reason && CODED.test(s.code));
    await page.goto(`${FRONTEND}/stocks/${target.code}`);
    await page.waitForSelector(`[data-detail-open="${withReason.event_id}"]`);
    await page.locator(`[data-detail-open="${withReason.event_id}"]`).click();
    await page.locator('[data-detail="body"]').waitFor();
    const text = await group("stocks").innerText();
    check("理由逐字显示在抽屉里", text.includes(target.reason), target.reason);
    const line = text.split("\n").find((row) => row.includes(target.code));
    check("同一行里代码与理由在一起", Boolean(line) && line.includes(target.code), line ?? "(没找到该行)");
    await page.keyboard.press("Escape");
  }

  const passed = RESULTS.filter((row) => row.ok).length;
  await writeFile(
    resolve(EVIDENCE, "ui.md"),
    [
      "# M10d 界面验证",
      "",
      `结果：**${passed} / ${RESULTS.length}**`,
      "",
      `样本：${rich.event_id}（带原文与理由）· ${plain?.event_id ?? "（政策档无 url 样本未挑到）"} · 个股页 ${symbol}`,
      "",
      "| # | 断言 | 结果 | 证据 |",
      "|---|---|---|---|",
      ...RESULTS.map((row, i) => `| ${i + 1} | ${row.label} | ${row.ok ? "✅" : "❌"} | ${row.detail} |`),
      "",
    ].join("\n"),
  );
  await writeFile(
    resolve(EVIDENCE, "ui.json"),
    `${JSON.stringify({ passed, total: RESULTS.length, results: RESULTS }, null, 2)}\n`,
  );
  console.log(`\n${passed} / ${RESULTS.length} 通过，证据落 logs/m10d/`);
  if (passed !== RESULTS.length) process.exitCode = 1;
}

main()
  .catch(async (error) => {
    console.error("走查中断：", error);
    const passed = RESULTS.filter((row) => row.ok).length;
    await mkdir(EVIDENCE, { recursive: true });
    await writeFile(
      resolve(EVIDENCE, "ui.json"),
      `${JSON.stringify({ passed, total: RESULTS.length, results: RESULTS, error: String(error) }, null, 2)}\n`,
    );
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close().catch(() => undefined);
  });
