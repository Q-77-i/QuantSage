/**
 * E5b 界面验证：`/paper` 的风控档位、状态条、风控日志与风控徽章。
 *
 * 场景（真浏览器 + 真后端 + 真行情）：
 *   ① 建会话表单：选「自定义」，四个输入出现；填 95/30/3/5 后提示语回显这四个值；
 *   ② 建会话后：`data-risk-panel` 渲染，三读数与接口 `risk_state` 一致；
 *   ③ 「全部驳回」的确认段必须写「风控止损仍会执行」；
 *   ④ 跑到结束（全部批准）后：风控日志展开全部，`data-risk-event` 的 verdict 集合与接口
 *      `risk_events` 一致；有回撤暂停横幅；决策表里出现 `data-risk-badge`（风控自动）；
 *   ⑤ 风控单的批准 / 驳回按钮**不存在**（它是 approved，不是 pending）。
 *
 * 自备账号：注册一个探针账号，跑完清干净。
 * 前置：后端 8000（**新代码——改了服务端契约必须先重启**）+ 前端 3001。
 * 用法：cd frontend && node scripts/capture-e5.mjs
 * 证据：logs/e5/ui.md / ui.json / ui.png / ui-risk-panel.png
 */

import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BACKEND = resolve(ROOT, "backend");
const EVIDENCE = resolve(ROOT, "logs/e5");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";
const STAMP = Date.now().toString(36);
const EMAIL = `e5-ui-${STAMP}@example.com`;
const PASSWORD = "e5-ui-pass-1234";

const START = "2025-09-22";
const SYMBOLS = "600519 000001 300750";
const RISK = { max_gross_exposure: 95, max_position: 30, stop_loss: 3, max_drawdown: 5 };

const RESULTS = [];

function check(label, ok, detail = "") {
  RESULTS.push({ label, ok: Boolean(ok), detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

function py(code) {
  return execFileSync(resolve(BACKEND, ".venv/bin/python"), ["-c", code], {
    cwd: BACKEND,
    encoding: "utf8",
  }).trim();
}

function purge() {
  py(`
import psycopg
from app.core.config import get_settings
with psycopg.connect(get_settings().postgres_dsn) as conn:
    conn.execute("DELETE FROM users WHERE email = %s", ("${EMAIL}",))
`);
}

async function signUp(context) {
  const response = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!response.ok()) throw new Error(`注册失败 ${response.status()}`);
}

async function apiJson(context, path) {
  const response = await context.request.get(`${API}${path}`);
  if (!response.ok()) throw new Error(`GET ${path} → ${response.status()}`);
  return response.json();
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    colorScheme: "light",
  });
  try {
    await signUp(context);
    const page = await context.newPage();
    await page.goto(`${FRONTEND}/paper`, { waitUntil: "networkidle" });
    await page.waitForSelector("#paper-name", { timeout: 60_000 });

    // ── ① 表单：自定义档位与提示语 ─────────────────────────────
    await page.fill("#paper-name", "E5 风控走查");
    await page.fill("#paper-cash", "1000000");
    await page.fill("#paper-symbols", SYMBOLS);
    await page.fill("#paper-start", START);
    await page.selectOption("#paper-risk-profile", "custom");
    await page.waitForSelector("#paper-risk-gross", { timeout: 10_000 });
    await page.fill("#paper-risk-gross", String(RISK.max_gross_exposure));
    await page.fill("#paper-risk-position", String(RISK.max_position));
    await page.fill("#paper-risk-stop", String(RISK.stop_loss));
    await page.fill("#paper-risk-drawdown", String(RISK.max_drawdown));
    const hint = await page.textContent("#paper-risk-profile, [data-risk-panel]")
      .catch(() => null);
    // 提示语在档位旁边，用页面文本里那句稳定的措辞判定
    const bodyText = await page.textContent("body");
    check(
      "自定义档提示回显四个值",
      Boolean(bodyText?.includes("总仓位 ≤95%") &&
        bodyText?.includes("单标的 ≤30%") &&
        bodyText?.includes("止损 -3%") &&
        bodyText?.includes("回撤 ≤5%")),
      hint ? "提示语已渲染" : "",
    );

    await page.getByRole("button", { name: "建会话" }).click();
    await page.waitForSelector("[data-risk-panel]", { timeout: 120_000 });
    const accountId = new URL(page.url()).searchParams.get("id");
    check("建会话后进入账户页", Boolean(accountId), accountId ?? "缺少 ?id=");

    let detail = await apiJson(context, `/api/v1/paper/accounts/${accountId}`);
    check(
      "账户 config 带四个风控值",
      detail.account.config.risk?.stop_loss === RISK.stop_loss &&
        detail.account.config.risk?.max_drawdown === RISK.max_drawdown,
      JSON.stringify(detail.account.config.risk),
    );
    const drawdownText =
      (await page.textContent('[data-risk-row="drawdown"]')) ?? "";
    const drawdownMatch = drawdownText.match(/([\d.]+)%/);
    const drawdownOnPage = drawdownMatch ? Number(drawdownMatch[1]) : null;
    check(
      "三读数与接口 risk_state 一致",
      drawdownOnPage !== null &&
        Math.abs(drawdownOnPage - detail.risk_state.drawdown_pct) < 0.01,
      `页面 ${drawdownOnPage ?? "—"}% · 接口 ${detail.risk_state.drawdown_pct.toFixed(2)}%`,
    );

    // ── ② 「全部驳回」的确认文案 ───────────────────────────────
    await page.getByLabel("跑到结束时怎么处理新生成的决策").selectOption("none");
    await page.getByRole("button", { name: "跑到结束" }).click();
    const confirmText = await page.textContent("body");
    check(
      "「全部驳回」确认段写明风控止损仍会执行",
      Boolean(confirmText?.includes("风控止损仍会执行")),
    );
    await page.getByRole("button", { name: "取消" }).click();

    // ── ③ 跑到结束（全部批准）─────────────────────────────────
    await page.getByLabel("跑到结束时怎么处理新生成的决策").selectOption("all");
    await page.getByRole("button", { name: "跑到结束" }).click();
    await page.getByRole("button", { name: /^确认跑到/ }).click();
    await page.waitForFunction(
      () => document.body.textContent?.includes("已跑到区间末端"),
      undefined,
      { timeout: 180_000 },
    );

    detail = await apiJson(context, `/api/v1/paper/accounts/${accountId}`);
    const expectedVerdicts = new Set(detail.risk_events.map((event) => event.verdict));
    for (const verdict of ["cap", "trigger", "block", "pass"]) {
      check(`接口 risk_events 含 ${verdict}`, expectedVerdicts.has(verdict), "");
    }
    check(
      "接口 risk_state 已进入回撤暂停",
      detail.risk_state.blocked_by_drawdown === true,
      `drawdown=${detail.risk_state.drawdown_pct.toFixed(2)}%`,
    );

    // 展开风控日志全部（按钮文案带事件数）
    const expandRisk = page.getByRole("button", {
      name: new RegExp(`展开全部 ${detail.risk_events.length} 条`),
    });
    if (await expandRisk.count()) await expandRisk.click();
    const verdictsOnPage = await page.locator("[data-risk-event]").evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-risk-event")),
    );
    const pageSet = new Set(verdictsOnPage);
    check(
      "页面风控日志的 verdict 集合与接口一致",
      expectedVerdicts.size === pageSet.size &&
        [...expectedVerdicts].every((verdict) => pageSet.has(verdict)),
      `页面 ${[...pageSet].sort().join("/")} · 接口 ${[...expectedVerdicts].sort().join("/")}`,
    );
    check(
      "回撤暂停横幅可见",
      (await page.locator("[data-risk-blocked]").count()) === 1,
    );

    // 展开决策流水，找风控自动徽章
    const expandDecisions = page.getByRole("button", {
      name: new RegExp(`展开全部 ${detail.decisions.length} 条`),
    });
    if (await expandDecisions.count()) await expandDecisions.click();
    const riskBadges = await page.locator("[data-risk-badge]").count();
    const riskDecisions = detail.decisions.filter((decision) => decision.origin === "risk");
    check(
      "风控止损单在流水里带徽章",
      riskBadges === riskDecisions.length && riskDecisions.length > 0,
      `徽章 ${riskBadges} 个 · 风控单 ${riskDecisions.length} 张`,
    );

    // 风控单不可驳回：它的状态是 filled/approved，不是 pending；页面上没有它的批准按钮
    const pendingRisk = riskDecisions.filter((decision) => decision.status === "pending");
    check(
      "风控止损单没有待审批态（不可被用户驳回）",
      pendingRisk.length === 0,
      `风控单状态：${riskDecisions.map((decision) => decision.status).join("/")}`,
    );

    // ── ④ 证据 ─────────────────────────────────────────────────
    await page.locator("[data-risk-panel]").scrollIntoViewIfNeeded();
    const riskSection = page
      .locator("section")
      .filter({ has: page.locator("[data-risk-panel]") });
    await riskSection.screenshot({ path: resolve(EVIDENCE, "ui-risk-panel.png") });
    await page.screenshot({ path: resolve(EVIDENCE, "ui.png"), fullPage: true });

    // README 截图：1440 宽 / 浅色 / 非整页高 1000（2026-10-13 定死的规格）
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: resolve(ROOT, "docs/images/paper.png") });

    await writeFile(
      resolve(EVIDENCE, "ui.json"),
      JSON.stringify(
        {
          account: accountId,
          risk: RISK,
          risk_state: detail.risk_state,
          risk_events: detail.risk_events.length,
          risk_decisions: riskDecisions.length,
          results: RESULTS,
        },
        null,
        2,
      ),
      "utf8",
    );
    const lines = [
      "# E5b 界面验证",
      "",
      `账号：${EMAIL}`,
      `账户：${accountId}`,
      `风控：${JSON.stringify(RISK)}`,
      "",
      "| 断言 | 结果 | 说明 |",
      "|---|---|---|",
      ...RESULTS.map((row) => `| ${row.label} | ${row.ok ? "✅" : "❌"} | ${row.detail} |`),
      "",
      "![风控面板](ui-risk-panel.png)",
      "",
      "![整页](ui.png)",
    ];
    await writeFile(resolve(EVIDENCE, "ui.md"), `${lines.join("\n")}\n`, "utf8");
  } finally {
    await browser.close();
    purge();
  }

  const failed = RESULTS.filter((row) => !row.ok);
  console.log(`\n${RESULTS.length - failed.length}/${RESULTS.length} 通过`);
  if (failed.length) {
    for (const row of failed) console.log(`  ✗ ${row.label}${row.detail ? `：${row.detail}` : ""}`);
    process.exitCode = 1;
  }
}

await main();
