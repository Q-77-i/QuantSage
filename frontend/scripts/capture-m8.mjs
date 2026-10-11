/**
 * M8c 界面验证脚本：深度研究的**过程可视化**走一遍真浏览器。
 *
 * 前置：后端 8000（**含 M8 端点、且是重启后的进程**——无 `--reload`，改完必须重启）与前端 3001 都在跑。
 * 用法：
 *   cd frontend && node scripts/capture-m8.mjs            # 场景一：完整跑一次（真模型，约 40s）
 *   cd frontend && node scripts/capture-m8.mjs --hitl     # 场景二：审批卡（后端须以低上限启动，见下）
 *
 * 场景二的前置（**人工**，因为它是服务端环境变量）：
 *   pkill -f "uvicorn app.main" && RESEARCH_MAX_LLM_CALLS=2 nohup uv run uvicorn app.main:app --port 8000 &
 *
 * 五条断言是自动化层结构上看不见的（M5b/M5c/M6/M7 的教训）：
 *   1. **阶段带真的在推进**——按 `data-stage` 的 `data-state` 断言，不看文案；
 *   2. **瀑布条真的画出来了**且**长短有别**——读条元素的实际像素宽度（零宽/等宽都算失败）；
 *   3. **审批卡真的拦住**——待批时页面停在 `awaiting_approval`，「收尾」之后状态真的变了；
 *   4. 研报页的**运行摘要块**有读数（模型档 + 成本），数字条**没有把 `-11.75%` 二次格式化**；
 *   5. 深色与窄屏各出一张图（窄屏不横向溢出）。
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const IMAGES = resolve(ROOT, "docs/images");
const EVIDENCE = resolve(ROOT, "logs/m8");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m8-ui@example.com";
const PASSWORD = "m8-ui-pass";
const QUESTION = "分析一下贵州茅台现在值不值得买";

const HITL = process.argv.includes("--hitl");
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

/** 阶段带的各段状态（不看文案，按 data 属性）。 */
async function stageStates(page) {
  return page.$$eval("[data-stage]", (nodes) =>
    nodes.map((node) => ({ node: node.dataset.stage, state: node.dataset.state })),
  );
}

/** 瀑布条的像素宽度（**等宽或零宽都算失败**——那是没画出来或画成了装饰）。 */
async function barWidths(page) {
  return page.$$eval("[data-bar]", (nodes) =>
    nodes.map((node) => ({
      node: node.dataset.bar,
      width: Math.round(node.getBoundingClientRect().width),
    })),
  );
}

async function startRun(page) {
  await page.goto(`${FRONTEND}/research`, { waitUntil: "networkidle" });
  await page.fill("input[placeholder*='贵州茅台']", QUESTION);
  await page.locator("button", { hasText: "开始研究" }).click();
}

async function main() {
  await mkdir(IMAGES, { recursive: true });
  await mkdir(EVIDENCE, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
  await signIn(context);
  const page = await context.newPage();

  if (!HITL) {
    console.log("场景一：完整跑一次深度研究");
    await startRun(page);

    // ⓪ 页面级的三条（用户当场逮到的）：页头高亮对、正文不贴左边界、不出现字面 markdown
    const nav = await page.evaluate(() => ({
      active: [...document.querySelectorAll("[data-nav][data-active='true']")].map((n) => n.dataset.nav),
      // 量**标题文字**的位置，不是容器的：padding 在容器内部，元素自身左偏移永远是 0
      left: Math.round(document.querySelector("h1")?.getBoundingClientRect().left ?? -1),
      literalMarkdown: /\*\*|`/.test(document.body.innerText),
    }));
    check("页头高亮「深度研究」而不是模拟盘", nav.active.join(",") === "research", nav.active.join(",") || "（无）");
    check("正文不贴左边界（容器内边距在）", nav.left >= 12, `main 左偏移 ${nav.left}px`);
    check("页面上没有字面 markdown（** 与反引号）", !nav.literalMarkdown);

    // ① 阶段带真的在推进：先是取证那几段进入 running/done
    await page.waitForSelector("[data-stage][data-state='running'], [data-stage][data-state='done']", {
      timeout: 90_000,
    });
    const early = await stageStates(page);
    const started = early.filter((s) => s.state !== "pending").length;
    check("阶段带真的在推进", started > 0, `${started}/10 段已离开 pending`);

    // ② 跑完再量瀑布——**中途量会假绿**：只有一条时它自然占满宽度（第一版就栽在这）
    await page.waitForSelector("text=看这份研报", { timeout: 180_000 });
    const widths = await barWidths(page);
    const maxWidth = Math.max(...widths.map((w) => w.width));
    const minWidth = Math.min(...widths.map((w) => w.width));
    check("瀑布条有实际宽度", maxWidth > 40, `最长条 ${maxWidth}px，共 ${widths.length} 行`);
    check(
      "并行条**长短有别**（没被归一成一样长）",
      maxWidth > minWidth * 1.5,
      `${maxWidth}px vs ${minWidth}px`,
    );
    const stages = await stageStates(page);
    check(
      "全部阶段跑到终态",
      stages.every((s) => s.state === "done"),
      stages.map((s) => `${s.node}:${s.state}`).join(" "),
    );

    // 预算条读数（四支都在）
    const meters = await page.$$eval("[data-meter]", (nodes) => nodes.map((n) => n.dataset.meter));
    check("预算四支计量条都在", meters.length === 4, meters.join(","));

    await page.screenshot({ path: resolve(EVIDENCE, "ui-run.png"), fullPage: true });

    // ④ 研报页的运行摘要 + 数字条不被二次格式化
    await page.locator("a", { hasText: "看这份研报" }).click();
    await page.waitForSelector("text=运行摘要", { timeout: 60_000 });
    const summary = await page
      .locator("section", { hasText: "运行摘要" })
      .first()
      .innerText();
    check(
      "运行摘要带模型档与中文标签（不是原始键路径）",
      /思考：关/.test(summary) && /成本（美元）/.test(summary) && /调用次数/.test(summary),
      summary.replace(/\s+/g, " ").slice(0, 140),
    );
    // 数字条：角色块里的值是**服务端格式化好的字符串**（`-11.75%`），前端不得二次格式化
    const roleChips = await page
      .locator("section", { hasText: "行情技术面" })
      .first()
      .locator("dl dd")
      .allInnerTexts();
    check(
      "角色数字按原样显示（% 与中文量词都在）",
      roleChips.some((text) => text.includes("%")) && roleChips.some((text) => text.includes("元")),
      roleChips.slice(0, 3).join(" | "),
    );
    await page.screenshot({ path: resolve(IMAGES, "agent-run.png"), fullPage: true });

    // ⑤ 深色 + 窄屏
    await page.goto(`${FRONTEND}/research/runs/${await lastRunId(context)}`, { waitUntil: "networkidle" });
    await page.emulateMedia({ colorScheme: "dark" });
    await page.evaluate(() => document.documentElement.classList.add("dark"));
    await page.screenshot({ path: resolve(EVIDENCE, "ui-run-dark.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 900 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.evaluate(() => document.documentElement.classList.remove("dark"));
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    check("窄屏没有横向溢出", overflow <= 1, `溢出 ${overflow}px`);
    await page.screenshot({ path: resolve(EVIDENCE, "ui-run-narrow.png"), fullPage: true });
  } else {
    console.log("场景二：审批卡（需要后端以 RESEARCH_MAX_LLM_CALLS=2 启动）");
    await startRun(page);
    await page.waitForSelector("[data-approval]", { timeout: 180_000 });
    const kind = await page.getAttribute("[data-approval]", "data-approval");
    check("审批卡真的拦住了运行", kind === "budget", `kind=${kind}`);

    const before = await page.textContent("[data-run-status]");
    await page.screenshot({ path: resolve(EVIDENCE, "ui-approval.png"), fullPage: true });

    const runHref = await page.getAttribute("a[href^='/research/runs/']", "href");
    // 用 locator 点：`text=` 选择器在按钮上不会派发到 onClick（真栽过一次）
    await page.locator("[data-approval] button", { hasText: "收尾" }).click();
    // 决定之后**就地跑完**（恢复流接着喂同一块面板）——不需要刷新页面
    await page.waitForSelector("[data-run-status='degraded']", { timeout: 120_000 });
    const after = await page.textContent("[data-run-status]");
    check("决定之后状态真的变了", before !== after, `${before} → ${after}`);
    check("收尾后的状态是降级（不是假装成功）", after?.includes("降级") ?? false, String(after));

    // 再去**运行页**看同一件事：那才是服务端真源（入口页的实时态是内存里的）
    await page.goto(`${FRONTEND}${runHref}`, { waitUntil: "networkidle" });
    const persisted = await page.textContent("[data-run-status]");
    check("运行页（服务端真源）同样是降级", persisted?.includes("降级") ?? false, String(persisted));
  }

  await browser.close();

  const passed = RESULTS.filter((r) => r.ok).length;
  await writeFile(
    resolve(EVIDENCE, HITL ? "ui-hitl.json" : "ui.json"),
    JSON.stringify({ passed, total: RESULTS.length, results: RESULTS }, null, 2),
    "utf-8",
  );
  console.log(`\n${passed}/${RESULTS.length} 断言通过`);
  if (passed !== RESULTS.length) process.exitCode = 1;
}

async function lastRunId(context) {
  const response = await context.request.get(`${API}/api/v1/research/runs`);
  const data = await response.json();
  return data.runs[0].id;
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
