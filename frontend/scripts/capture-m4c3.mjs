/**
 * 回测页「我的策略」的走查与证据脚本（M4c-3）。
 *
 * 前置：后端 8000 与前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-m4c3.mjs
 *
 * **不只是截图**：脚本对五件事逐条断言——下拉分组、用户策略参数（含 `bool` 开关）
 * 按源码 schema 渲染、跑通并跳 `?run=`、静态检查被拦时 findings 逐条列出、
 * 重开旧记录时策略位与参数回填。跑完删掉自己造的两条策略。
 */

import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const EVIDENCE = resolve(ROOT, "logs/m4c3");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "m4c3-ui@example.com";
const PASSWORD = "m4c3-ui-pass";
/** 只删自己造的：带这个前缀的才是脚本的 */
const PREFIX = "走查·";
const SYMBOL = "600519";

/** 带一个 `bool` 参数的用户策略：参数区要按 schema 渲染出输入框 + 开关两种控件 */
const CODE = `PARAMS = {
    "fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"},
    "slow": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "慢线周期"},
    "keep_flat": {"type": "bool", "default": False, "label": "只空仓观察"},
}
USES_EVENTS = False


def mean(values):
    return sum(values) / len(values)


def on_bar(ctx, p):
    closes = [bar.close for bar in ctx.history]
    index = len(closes) - 1
    if index < p["slow"]:
        return []
    fast_now = mean(closes[index - p["fast"] + 1: index + 1])
    fast_prev = mean(closes[index - p["fast"]: index])
    slow_now = mean(closes[index - p["slow"] + 1: index + 1])
    slow_prev = mean(closes[index - p["slow"]: index])
    if p["keep_flat"]:
        return []
    if fast_prev <= slow_prev and fast_now > slow_now and ctx.position.is_flat:
        return [Signal(Side.BUY, reason="走查")]
    if fast_prev >= slow_prev and fast_now < slow_now and not ctx.position.is_flat:
        return [Signal(Side.SELL, reason="走查")]
    return []
`;

/** 静态检查会拦下的那一条（`import duckdb` 是 R1 的数据绕行） */
const BLOCKED = `import duckdb

PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线"}}
USES_EVENTS = False


def on_bar(ctx, p):
    return []
`;

const RESULTS = [];

function check(label, ok, detail = "") {
  RESULTS.push({ label, ok });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

async function signIn(context) {
  const register = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (register.ok()) return;
  if (register.status() !== 409) throw new Error(`注册失败 ${register.status()}`);
  const login = await context.request.post(`${API}/api/v1/auth/login`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);
}

async function json(context, path, init) {
  const response = await context.request.fetch(`${API}${path}`, init);
  if (!response.ok()) throw new Error(`${path} → ${response.status()}：${await response.text()}`);
  return response.json();
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const created = [];

  try {
    await signIn(context);
    for (const stale of await json(context, "/api/v1/strategies")) {
      if (stale.name.startsWith(PREFIX)) {
        await context.request.delete(`${API}/api/v1/strategies/${stale.id}`);
      }
    }

    const mine = await json(context, "/api/v1/strategies", {
      method: "POST",
      data: { name: `${PREFIX}我的双均线`, code: CODE },
    });
    const blocked = await json(context, "/api/v1/strategies", {
      method: "POST",
      data: { name: `${PREFIX}数据绕行`, code: BLOCKED },
    });
    created.push(mine.id, blocked.id);

    const page = await context.newPage();

    // ── ① 下拉分组 + 用户策略的参数区 ─────────────────────────────
    await page.goto(`${FRONTEND}/backtest`, { waitUntil: "domcontentloaded" });
    const select = page.locator("#bt-strategy");
    await select.waitFor({ timeout: 30_000 });
    // 「我的策略」是一次异步请求的结果，等它到位再断言分组（否则测的是「加载中」那一瞬）
    await page
      .locator('#bt-strategy optgroup[label="我的策略"]')
      .waitFor({ timeout: 30_000 })
      .catch(() => undefined);

    const groups = await page.locator("#bt-strategy optgroup").evaluateAll((nodes) =>
      nodes.map((node) => node.label),
    );
    check("下拉含「内置」与「我的策略」两组", groups.join(",") === "内置,我的策略", groups.join(","));

    await select.selectOption({ label: `${PREFIX}我的双均线` });
    const fastLabel = page.getByText("快线周期", { exact: true });
    await fastLabel.first().waitFor({ timeout: 30_000 });
    check("参数区按源码 schema 渲染（快线周期出现）", await fastLabel.first().isVisible());
    check(
      "`bool` 参数渲染成开关（只空仓观察）",
      await page.locator("#bt-keep_flat").isVisible(),
    );
    await page.screenshot({ path: `${EVIDENCE}/form-user-strategy.png` });

    // ── ② 跑通并跳 ?run= ─────────────────────────────────────────
    await page.locator("#bt-symbol").fill(SYMBOL);
    await page.getByRole("button", { name: "运行" }).click();
    await page.waitForURL(/\/backtest\?run=/, { timeout: 120_000 });
    const runUrl = page.url();
    check("提交后跳 ?run=（报告页）", /\/backtest\?run=[0-9a-f-]{36}/.test(runUrl), runUrl);

    const runId = new URL(runUrl).searchParams.get("run");
    const detail = await json(context, `/api/v1/backtest/runs/${runId}`);
    check(
      "报告是那条用户策略跑出来的",
      detail.report.meta.strategy_kind === "user" &&
        detail.report.meta.strategy_name === `${PREFIX}我的双均线`,
      `${detail.report.meta.strategy_kind}/${detail.report.meta.strategy_name}`,
    );
    check("落库带 strategy_id", detail.strategy_id === mine.id);
    check(
      "参数含布尔值（开关真的随请求发出）",
      detail.request.params.keep_flat === false && detail.request.params.fast === 5,
      JSON.stringify(detail.request.params),
    );
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${EVIDENCE}/report-user-strategy.png` });

    // ── ③ 重开：策略位与参数回填 ─────────────────────────────────
    await page.goto(`${FRONTEND}/backtest?run=${runId}`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#bt-keep_flat", { timeout: 30_000 });
    const selectedLabel = await select
      .locator("option:checked")
      .evaluate((node) => node.textContent.trim());
    check("重开后策略位回填到那条策略", selectedLabel === `${PREFIX}我的双均线`, selectedLabel);
    check(
      "重开后参数回填成当次的值",
      (await page.locator("#bt-fast").inputValue()) === "5" &&
        !(await page.locator("#bt-keep_flat").isChecked()),
    );

    // ── ④ 静态检查被拦：findings 逐条列出 ────────────────────────
    await page.goto(`${FRONTEND}/backtest`, { waitUntil: "domcontentloaded" });
    await page.locator("#bt-strategy").waitFor({ timeout: 30_000 });
    await page.locator("#bt-strategy").selectOption({ label: `${PREFIX}数据绕行` });
    await page.getByRole("button", { name: "运行" }).click();
    // 按**文案**挑，不按序号：Next 的开发浮层也会挂 `role="alert"`（实测多出一个空的），
    // 序号匹配会跟着浮层的出现与否漂移
    const alert = page.locator('[role="alert"]', { hasText: "静态检查" });
    await alert.first().waitFor({ timeout: 60_000 });
    const alertText = (await alert.first().innerText()).replace(/\s+/g, " ");
    check("静态检查被拦的文案出现在页面上", alertText.includes("静态检查"), alertText.slice(0, 60));
    check("findings 逐条列出（带行号与规则码）", /第 1 行/.test(alertText) && /R1/.test(alertText));
    await page.screenshot({ path: `${EVIDENCE}/blocked-findings.png` });
    await page.close();
  } finally {
    for (const id of created) {
      await context.request.delete(`${API}/api/v1/strategies/${id}`).catch(() => undefined);
    }
    await context.close();
    await browser.close();
  }

  const failed = RESULTS.filter((item) => !item.ok);
  console.log(`\n断言 ${RESULTS.length - failed.length}/${RESULTS.length} 通过`);
  if (failed.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error("失败：", error.message);
  process.exit(1);
});
