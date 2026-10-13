/**
 * M9 文档走查脚本：**按 `docs/tutorials/getting-started.md` 的流程逐条走一遍真浏览器**。
 *
 * 它验的不是「功能能不能用」（M1–M8 各自的 capture 脚本已经验过），而是
 * **「教程里承诺的东西，屏幕上真的会出现」**——教程写了什么判据，这里就断言什么。
 * 每条断言的 label 都能对回教程小节号（`docs/private/Pn-n/P2-Mn/P2-M9.md` 的对照表）。
 *
 * 前置：后端 8000 与前端 3001 都在跑（数据已落盘）。
 * 用法：
 *   cd frontend && node scripts/capture-m9.mjs           # 全部（含一次 PDF 下载，真模型）
 *   cd frontend && node scripts/capture-m9.mjs --fast    # 跳过耗时项（PDF / 深研）
 *   cd frontend && node scripts/capture-m9.mjs --deep    # 额外跑一次深度研究（真花钱，约 $0.02）
 *
 * 证据：`logs/m9/ui.md`（逐条）、`logs/m9/ui.json`、`logs/m9/*.png`。
 * **断言一律不做「恒真」兜底**——一条永远绿的断言等于没验（M5b 的教训）。
 */

import { mkdir, stat, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EVIDENCE = resolve(ROOT, "logs/m9");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = process.env.M9_EMAIL ?? "m9-ui@example.com";
const PASSWORD = process.env.M9_PASSWORD ?? "m9-ui-pass";
const NEW_EMAIL = `m9-new-${Date.now().toString(36)}@example.com`;
const FAST = process.argv.includes("--fast");
const DEEP = process.argv.includes("--deep");

const RESULTS = [];
const BLOCKERS = [];

function check(label, ok, detail = "") {
  RESULTS.push({ label, ok: Boolean(ok), detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

function blocker(where, detail) {
  BLOCKERS.push({ where, detail });
  console.log(`  ⚠ 断点 [${where}]：${detail}`);
}

/** 页面上可见的字面 markdown（`**粗体**` 或行首 `* `）。 */
async function literalMarkdown(page) {
  return page.evaluate(() => {
    const hits = [];
    for (const line of document.body.innerText.split("\n")) {
      if (/\*\*[^*]+\*\*/.test(line) || /^\s*\* [^\s]/.test(line)) hits.push(line.trim());
    }
    return hits;
  });
}

async function shot(page, name) {
  await page.screenshot({ path: resolve(EVIDENCE, `${name}.png`), fullPage: false });
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  const browser = await chromium.launch();

  // ── 流程 1：注册（用一次性的新账号，教程 §1 的第一步）──────────────────
  console.log("\n[流程 1] 注册（新用户视角）");
  const fresh = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const regPage = await fresh.newPage();
  regPage.setDefaultTimeout(20_000);
  await regPage.goto(`${FRONTEND}/register`, { waitUntil: "networkidle" });
  const regTitle = await regPage.locator("h2").first().textContent();
  check("注册页标题是「注册」（教程 §1）", regTitle?.includes("注册"), regTitle ?? "");
  const regSubmit = await regPage.locator("button[type=submit]").textContent();
  check(
    "提交按钮原文是「注册并登录」（教程 §1 说注册即登录态）",
    regSubmit?.includes("注册并登录"),
    regSubmit ?? "",
  );
  await regPage.fill("input[type=email]", NEW_EMAIL);
  await regPage.fill("input[type=password]", "m9-walkthrough-pw");
  await regPage.locator("#register-confirm, input[autocomplete=new-password]").last().fill("m9-walkthrough-pw");
  await regPage.locator("button[type=submit]").click();
  await regPage.waitForURL((url) => !url.pathname.includes("register"), { timeout: 30_000 });
  check("注册成功后直接落在应用内（无需再登录，教程 §1）", !regPage.url().includes("/login"), regPage.url());

  // 新账号的工作台空态（教程 §5 说从模板开始）
  await regPage.goto(`${FRONTEND}/strategies`, { waitUntil: "networkidle" });
  const emptyHint = await regPage.locator("text=还没有策略。从下面的模板「另存为我的」开始最快。").count();
  check("新账号工作台显示空态引导（教程 §5）", emptyHint > 0);
  await fresh.close();

  // ── 用固定账号登录（后面各流程都用它）────────────────────────────────
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: "zh-CN",
  });
  const reg = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!reg.ok() && reg.status() !== 409) throw new Error(`注册失败 ${reg.status()}`);
  const login = await context.request.post(`${API}/api/v1/auth/login`, {
    data: { email: EMAIL, password: PASSWORD, remember: true },
  });
  if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);

  try {
    // ── 流程 11：界面地图 + 未登录守卫（教程 §1/§2）──────────────────────
    console.log("\n[流程 11] 未登录守卫与页头地图");
    const anon = await browser.newContext();
    const anonPage = await anon.newPage();
    await anonPage.goto(`${FRONTEND}/backtest`, { waitUntil: "networkidle" });
    check("未登录访问受保护页 → 跳登录页（教程 §1）", anonPage.url().includes("/login"), anonPage.url());
    await anon.close();

    await page.goto(FRONTEND, { waitUntil: "networkidle" });
    const navLabels = await page.$$eval("header nav a", (nodes) =>
      nodes.map((node) => node.textContent.trim()),
    );
    const expectedNav = ["对话", "回测", "策略", "优化", "因子", "模拟盘", "深度研究", "个人空间"];
    check(
      "页头八项与教程 §2 的表逐项一致",
      JSON.stringify(navLabels) === JSON.stringify(expectedNav),
      navLabels.join(" / "),
    );
    check("页头没有「研报」项（教程 §2 特意提醒的那条）", !navLabels.some((l) => l.includes("研报")));

    // ── 流程 2：加自选（教程 §4）────────────────────────────────────────
    console.log("\n[流程 2] 加自选");
    await page.goto(`${FRONTEND}/space`, { waitUntil: "networkidle" });
    const box = page.locator("input[placeholder='代码或名称']");
    check("自选股输入框 placeholder 是「代码或名称」（教程 §4）", (await box.count()) > 0);
    if ((await box.count()) > 0) {
      await box.first().fill("600519");
      await page.waitForTimeout(1500);
      const formText = await page.locator("form").first().innerText();
      check(
        "输入六位代码后给出体检提示（教程 §4 说边输边给结果）",
        /最近交易日|已在自选|加自选/.test(formText),
        formText.split("\n").filter(Boolean).slice(0, 3).join(" | "),
      );
      const addBtn = page.locator("button", { hasText: /加自选|移出/ }).first();
      check("提交按钮在「加自选 / 移出」两态之间（教程 §4）", (await addBtn.count()) > 0, await addBtn.textContent());
      await shot(page, "01-watchlist");
    }

    // ── 流程 3：策略工作台（教程 §5）────────────────────────────────────
    console.log("\n[流程 3] 策略工作台");
    await page.goto(`${FRONTEND}/strategies`, { waitUntil: "networkidle" });
    const heading = await page.locator("h1").first().textContent();
    check("页标题是「策略工作台」（教程 §5）", heading?.includes("策略工作台"), heading ?? "");

    const workbenchMarkdown = await literalMarkdown(page);
    check(
      "工作台无字面 markdown（本轮修复项，M8c 遗留）",
      workbenchMarkdown.length === 0,
      workbenchMarkdown.join(" | "),
    );

    const templateTexts = await page.locator("aside").innerText();
    const knownTemplates = ["双均线交叉", "事件驱动", "唐奇安通道突破", "RSI 超卖反转", "放量突破"];
    const found = knownTemplates.filter((name) => templateTexts.includes(name));
    check(`五个模板都在（教程 §5 的表）`, found.length === 5, found.join("、"));

    await page.locator("aside button", { hasText: "双均线交叉" }).first().click();
    await page.waitForTimeout(1500);
    check(
      "选中模板后提示「模板只读」（教程 §5）",
      (await page.locator("text=模板只读，改它请先「另存为我的」").count()) > 0,
    );
    check("「另存为我的」按钮在（教程 §5）", (await page.locator("button", { hasText: "另存为我的" }).count()) > 0);

    // 往编辑器里注入一个未来函数，验 R3 拦截（教程 §5 的规则表）
    const saveAs = page.locator("button", { hasText: "另存为我的" }).first();
    await saveAs.click();
    await page.waitForTimeout(2500);
    // 「另存为」要改名才存得下：填个唯一名，再**保存并运行**一次（教程 §5 说的就是这一步）
    const nameInput = page.locator("input[placeholder='策略名（保存时用）']");
    if ((await nameInput.count()) > 0) {
      await nameInput.fill(`走查用策略 ${Date.now().toString(36)}`);
    }
    // 走 Monaco 自己的 API 改源码：DOM 里有两个 textarea（`.inputarea` 与只读的 `.ime-text-area`），
    // 且 fill 对 Monaco 的受控模型不生效——setValue 是唯一稳的路子（它仍会触发 onDidChangeModelContent）
    const wrote = await page.evaluate(() => {
      const monaco = window.monaco;
      const editor = monaco?.editor?.getEditors?.()[0];
      const model = editor?.getModel?.();
      if (!model) return false;
      // 两处形状是**走查期实测校准的**（第一版写错了两回，都靠后端 check 端点问出来）：
      //   ① 不写 import——`Signal` / `Side` 由沙箱预注入（模板头注释明说「无需也不能 import」），
      //      带上 import 会先触发 R1，报文里就读不到 R3；
      //   ② 要**下标取值**（`ctx.history[ctx.index + 1]`）才触发 R3；
      //      写成比较式 `ctx.index + 1 < len(ctx.history)` 是合法的、后端 0 findings。
      model.setValue(
        "PARAMS = {}\nUSES_EVENTS = False\n\n" +
          "def on_bar(ctx):\n    if ctx.history[ctx.index + 1] is not None:\n        return [Signal(Side.BUY, reason='未来')]\n    return []\n",
      );
      return true;
    });
    if (wrote) {
      await page.waitForTimeout(2500);
      const panel = page.locator("section").filter({ hasText: "检查结果" }).first();
      const panelText = (await panel.count()) ? await panel.innerText() : "";
      check(
        "前视静态检查拦下越界索引（教程 §5：R3 是 error 档）",
        panelText.includes("error 会拦住运行"),
        panelText.split("\n").find((l) => l.includes("error")) ?? panelText.slice(0, 80),
      );
      check(
        "findings 带规则号 R3（教程 §5 的表）",
        panelText.includes("R3"),
        panelText.split("\n").find((l) => l.includes("R3")) ?? "面板里没有 R3",
      );
      check(
        "R3 报文指向那一行（教程 §5 说点一下跳行）",
        /第 \d+ 行/.test(panelText),
        panelText.split("\n").find((l) => l.includes("第")) ?? "",
      );
      await shot(page, "07-strategies");

      // 保存并运行：应被 422 闸门拦下（教程 §5「error 命中即不能运行」）
      await page.locator("button", { hasText: "保存并运行" }).first().click();
      await page.waitForTimeout(4000);
      const alertText = await page.locator("[role=alert]").first().innerText().catch(() => "");
      check(
        "带 error 的策略点「保存并运行」被拦下（教程 §5 的 422 闸门）",
        /静态检查|error/.test(alertText),
        alertText.slice(0, 120),
      );
    } else {
      blocker("策略工作台", "Monaco 编辑器没挂上（window.monaco 取不到实例），无法验静态检查");
    }

    // ── 流程 4：回测（教程 §6）──────────────────────────────────────────
    console.log("\n[流程 4] 回测与 PIT 对比");
    await page.goto(`${FRONTEND}/backtest`, { waitUntil: "networkidle" });
    const btTitle = await page.locator("h1").first().textContent();
    check("页标题是「回测」（教程 §6）", btTitle?.includes("回测"), btTitle ?? "");

    await page.locator("button", { hasText: /^运行$/ }).first().click();
    await page.waitForSelector("text=PIT 与非 PIT 的差异", { timeout: 150_000 });
    check("PIT 对比区出现（教程 §6 的表）", true, "（事件驱动默认模式）");
    check(
      "核心量化值「期末权益差异（非 PIT 相对 PIT）」在（教程 §6）",
      (await page.locator("text=期末权益差异（非 PIT 相对 PIT）").count()) > 0,
    );
    check(
      "口径句「看到未来不等于赚得更多」在（教程 §6 / §12-3）",
      (await page.locator("text=看到未来不等于赚得更多").count()) > 0,
    );
    check("事件语料区在（教程 §6）", (await page.locator("text=事件语料").count()) > 0);
    check(
      "事件表把「事发」与「首次可用」并列成列（教程 §6）",
      (await page.locator("th", { hasText: "首次可用" }).count()) > 0,
    );
    await shot(page, "02-backtest");

    // 双均线 → 教程承诺的那句「不做无意义的二次回测」
    const strategySelect = page.locator("select").first();
    await strategySelect.selectOption({ label: "双均线" });
    await page.waitForTimeout(400);
    await page.locator("button", { hasText: /^运行$/ }).first().click();
    await page.waitForSelector("text=双均线不消费事件语料", { timeout: 150_000 });
    check("双均线显示「不做无意义的二次回测」（教程 §6）", true);

    // ── 流程 5：参数网格与 DSR（教程 §7）────────────────────────────────
    console.log("\n[流程 5] 参数网格与过拟合检验");
    await page.goto(`${FRONTEND}/optimize`, { waitUntil: "networkidle" });
    const optTitle = await page.locator("h1").first().textContent();
    check("页标题是「网格与批量」（教程 §7）", optTitle?.includes("网格与批量"), optTitle ?? "");
    check(
      "副标题写明「进度逐格推送；断线即断、不续传」（教程 §2 的优化页）",
      (await page.locator("text=进度逐格推送").count()) > 0,
    );
    // 默认表单只有 1 条参数轴（那种情况画的是折线，不是热力图）——加第二条轴才是教程 §7 说的网格
    const addAxis = page.locator("button", { hasText: "加一条轴" });
    if ((await addAxis.count()) > 0) {
      await addAxis.first().click();
      await page.waitForTimeout(500);
      const axisSelects = page.locator("fieldset select");
      if ((await axisSelects.count()) >= 2) {
        await axisSelects.nth(1).selectOption({ label: "慢线" }).catch(() => {});
      }
      // 第二条轴**必须填值**：空取值时前端判「至少要有 2 个取值」，运行按钮点不动（走查期踩到）
      const axisInputs = page.locator("fieldset input[aria-label$='的取值']");
      if ((await axisInputs.count()) >= 2) {
        await axisInputs.nth(1).fill("15, 20, 30");
      }
    }
    // 填完轴要留一拍：表单校验有 debounce，立刻点运行会打在被重渲染的按钮上（走查期踩到）
    await page.waitForTimeout(1200);
    const gridRun = page.locator("button", { hasText: /^运行$/ }).first();
    await gridRun.waitFor({ state: "visible", timeout: 30_000 });
    await gridRun.click();
    // 等「过拟合检验」区出现（文本与 CSS 不能混在同一个 waitForSelector 里，会解析失败）
    await page.locator("text=过拟合检验").first().waitFor({ timeout: 300_000 });
    // 再等网格逐格跑完：热力图/表格视图要等 9 格都有结果才渲染（3s 时只有 DSR 卡在那儿）
    await page.locator("text=格有结果").first().waitFor({ timeout: 300_000 });
    await page.waitForTimeout(4000);
    const bodyText = await page.locator("main").innerText();
    check("出现「过拟合检验」区（教程 §7）", bodyText.includes("过拟合检验"));
    check(
      "DSR 说明句在（教程 §7 引用的原文）",
      bodyText.includes("不是运气的概率") || bodyText.includes("常用显著线"),
    );
    check("热力图区在（教程 §7）", bodyText.includes("参数网格"));
    const tableView = await page.locator("button", { hasText: "看表格" }).count();
    check("热力图有「看表格」孪生（教程 §7）", tableView > 0);
    await shot(page, "08-optimize");

    // ── 流程 6：因子分析（教程 §12-3 的如实标注）─────────────────────────
    console.log("\n[流程 6] 因子分析");
    await page.goto(`${FRONTEND}/factor`, { waitUntil: "networkidle" });
    const factorTitle = await page.locator("h1").first().textContent();
    check("页标题是「因子分析」（教程 §2）", factorTitle?.includes("因子分析"), factorTitle ?? "");
    await page.locator("button", { hasText: /^运行$/ }).first().click();
    await page.waitForSelector("text=逐日 RankIC", { timeout: 240_000 });
    const factorText = await page.locator("main").innerText();
    check("英雄数字「RankIC 均值」在（教程 §2）", factorText.includes("RankIC 均值"));
    check("「如实标注」区在（教程 §12）", factorText.includes("如实标注"));
    check(
      "多空价差如实标注「A 股不可做空」（教程 §12）",
      factorText.includes("不可做空") || factorText.includes("统计量"),
    );
    await shot(page, "03-factor");

    // ── 流程 7：模拟盘（教程 §8）────────────────────────────────────────
    console.log("\n[流程 7] 模拟盘");
    await page.goto(`${FRONTEND}/paper`, { waitUntil: "networkidle" });
    const paperTitle = await page.locator("h1").first().textContent();
    check("页标题是「模拟盘」（教程 §8）", paperTitle?.includes("模拟盘"), paperTitle ?? "");

    // 新账号先要建会话（教程 §8 第一步）——建完闸门/六态/推进这些块才会出现
    const createForm = page.locator("#paper-name");
    if ((await createForm.count()) > 0) {
      await createForm.fill(`走查会话 ${Date.now().toString(36)}`);
      await page.locator("#paper-symbols").fill("600519");
      await page.locator("#paper-start").fill("2026-08-03");
      await page.locator("button", { hasText: "建会话" }).first().click();
      await page.waitForSelector("text=决策闸门", { timeout: 90_000 });
      check("建会话后出现决策闸门（教程 §8）", true);
    }
    const gate = await page.locator("text=批准只改状态；成交发生在下一交易日的开盘").count();
    check("闸门区写明「批准只改状态」（教程 §8.1 第 1 条口径）", gate > 0);
    check(
      "决策流水写明六态（教程 §8.1 的表）",
      (await page.locator("text=六态：待审批 / 已批准 / 已成交 / 已驳回 / 未审批过期 / 已批准未成交").count()) > 0,
    );
    check(
      "推进旁写明它做什么（教程 §8.1）",
      (await page.locator("text=推进 = 成交上一日已批准的单（按当日开盘价）").count()) > 0,
    );
    const stepBtn = page.locator("button", { hasText: "推进一天" });
    if ((await stepBtn.count()) > 0 && (await stepBtn.first().isEnabled())) {
      await stepBtn.first().click();
      await page.waitForTimeout(3000);
      const receipt = await page
        .locator("text=/成交 \\d+ 笔|新生成 \\d+ 条待审批|未审批过期 \\d+ 笔|没有发生任何事/")
        .count();
      check("推进后给出回执（教程 §8.1）", receipt > 0);
    }
    await shot(page, "04-paper");

    // ── 流程 8：出研报与导出（教程 §9）──────────────────────────────────
    console.log("\n[流程 8] 出研报与导出");
    check("模拟盘页有「出研报」入口（教程 §2 说它不在页头）", (await page.locator("[data-report-entry]").count()) > 0);
    check(
      "入口旁说明与教程 §9 引用一致",
      (await page.locator("text=绩效研报：指标 / 归因 / 逐笔复盘（含教训）/ 证据链，可分享给未登录的人看").count()) > 0,
    );
    await page.locator("button", { hasText: "出研报" }).first().click();
    await page.waitForURL(/\/research\/[0-9a-f-]+/, { timeout: 150_000 });
    await page.waitForTimeout(2000);
    check(
      "研报页脚免责声明在（教程 §9）",
      (await page.locator("text=本报告由 QuantSage 生成，仅供研究用途，不构成投资建议。").count()) > 0,
    );
    check("分享按钮在（教程 §9 的动作表）", (await page.locator("button", { hasText: /生成分享链接|复制分享链接/ }).count()) > 0);
    check("「导出 PDF」在（教程 §9）", (await page.locator("a", { hasText: "导出 PDF" }).count()) > 0);
    check("「导出 Markdown」在（教程 §9）", (await page.locator("a", { hasText: "导出 Markdown" }).count()) > 0);
    await shot(page, "05-report");

    if (!FAST) {
      const [download] = await Promise.all([
        page.waitForEvent("download", { timeout: 120_000 }),
        page.locator("a", { hasText: "导出 PDF" }).first().click(),
      ]);
      const path = await download.path();
      const size = path ? (await stat(path)).size : 0;
      check("「导出 PDF」真的下到一个真文件（教程 §9：服务端渲染）", size > 10_000, `${Math.round(size / 1024)}KB`);
    }

    // ── 流程 10：个人空间（教程 §11）────────────────────────────────────
    console.log("\n[流程 10] 个人空间");
    await page.goto(`${FRONTEND}/space`, { waitUntil: "networkidle" });
    const spaceText = await page.locator("main").innerText();
    const expectedTabs = ["我的自选", "我的回测", "会话历史", "我的策略", "我的优化", "我的模拟盘", "深度研究"];
    const foundTabs = expectedTabs.filter((name) => spaceText.includes(name));
    check("个人空间七个页签与教程 §11 的表一致", foundTabs.length === 7, `找到 ${foundTabs.length}/7`);
    await page.goto(`${FRONTEND}/space?tab=optimizations`, { waitUntil: "networkidle" });
    check(
      "「我的优化」自述与教程 §11 一致（点一条重开、不重跑）",
      (await page.locator("text=网格 / 批量的汇总；点一条重开（不重跑）").count()) > 0,
    );
    await shot(page, "06-space");

    // ── 流程 9：深度研究（可选，真花钱）────────────────────────────────
    if (DEEP) {
      console.log("\n[流程 9] 深度研究（真模型，约 40s）");
      await page.goto(`${FRONTEND}/research`, { waitUntil: "networkidle" });
      const researchTitle = await page.locator("h1").first().textContent();
      check("页标题是「深度研究」（教程 §10）", researchTitle?.includes("深度研究"), researchTitle ?? "");
      const researchText = await page.locator("main").innerText();
      check(
        "时点说明写明 PIT 过滤（教程 §10 引用 available_at ≤ 时点）",
        researchText.includes("available_at ≤ 时点") || researchText.includes("available_at"),
      );
      await page.locator("select").last().selectOption({ label: "强制深度研究" });
      await page.locator("button", { hasText: "开始研究" }).click();
      await page.waitForSelector("[data-stage]", { timeout: 60_000 });
      check("提交后原地出现执行阶段（教程 §10）", (await page.locator("[data-stage]").count()) >= 10);
      await page.waitForSelector("text=并行瀑布", { timeout: 120_000 });
      // **等条真的画出来**：节点只有跑完（拿到 end 事件）才会成行，段落标题一出现量、只会是 0 条
      // ——M8c 记过同一条教训（「断言时序：瀑布条要在跑完之后量」），这次在文档走查里又踩了一次
      await page.locator("[data-bar]").first().waitFor({ timeout: 180_000 });
      await page.waitForTimeout(3000);
      const widths = await page.$$eval("[data-bar]", (nodes) =>
        nodes.map((n) => Math.round(n.getBoundingClientRect().width)),
      );
      check(
        "并行瀑布条真的画出来了且长短有别（教程 §10 说条长即真实耗时）",
        widths.length > 0 && Math.max(...widths) - Math.min(...widths) > 5,
        `${widths.length} 条，宽 ${Math.min(...widths)}–${Math.max(...widths)}px`,
      );
      await page.waitForSelector("text=看这份研报", { timeout: 240_000 });
      check("跑完给出「看这份研报」（教程 §10）", true);
      await shot(page, "09-research");
    }
  } catch (error) {
    blocker("走查中断", String(error).slice(0, 400));
  } finally {
    const passed = RESULTS.filter((item) => item.ok).length;
    await writeFile(
      resolve(EVIDENCE, "ui.json"),
      JSON.stringify({ passed, total: RESULTS.length, results: RESULTS, blockers: BLOCKERS }, null, 2),
      "utf-8",
    );
    await writeFile(
      resolve(EVIDENCE, "ui.md"),
      [
        "# M9 文档走查（对照 `docs/tutorials/getting-started.md`）",
        "",
        `结果：**${passed} / ${RESULTS.length}** 条断言通过`,
        "",
        "| # | 断言 | 结果 | 说明 |",
        "|---|---|---|---|",
        ...RESULTS.map((r, i) => `| ${i + 1} | ${r.label} | ${r.ok ? "✅" : "❌"} | ${r.detail} |`),
        "",
        "## 断点",
        "",
        BLOCKERS.length ? BLOCKERS.map((b) => `- **${b.where}**：${b.detail}`).join("\n") : "无。",
        "",
      ].join("\n"),
      "utf-8",
    );
    await browser.close();
    console.log(`\n${passed} / ${RESULTS.length} 通过；断点 ${BLOCKERS.length} 条。证据：logs/m9/ui.md`);
  }
}

await main();
