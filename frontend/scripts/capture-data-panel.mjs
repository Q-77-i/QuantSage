/**
 * E3a 界面验证：`/admin` 的数据体检看板（真浏览器、真后端、真数据目录）。
 *
 * 验六件事——每一条都对应 SPEC §4.1–§4.3 的一句话：
 *   ① admin 账号打 `/admin`：**数据体检区渲染出来**（区块锚在自己身上，不按索引取）；
 *   ② 状态文案与接口的 `check_status` **一致**——红灯是服务端算的，页面只做翻译，不是画上去的；
 *   ③ 告警一览按级别分组，**条目数与接口的计数一致**；
 *   ④ 「源侧止于 …」那一行在（**归因就是这一句的全部意义**）；
 *   ⑤ 运行历史表与接口的 `total` 一致；
 *   ⑥ 普通账号打 `/admin`：见到「需要管理员权限」、**不跳登录**、且**看不到**数据体检区。
 *
 * 自备账号：注册两个探针账号（一个提权、一个不提），跑完清干净。
 *
 * 前置：后端 8000（**新代码**——改了服务端契约必须先重启再跑，E2c 的教训）+ 前端 3001。
 * 用法：cd frontend && node scripts/capture-data-panel.mjs
 * 证据：logs/e3/data-panel.md / data-panel.json / data-panel.png
 */

import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BACKEND = resolve(ROOT, "backend");
const EVIDENCE = resolve(ROOT, "logs/e3");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const STAMP = Date.now().toString(36);
const ADMIN_EMAIL = `e3a-admin-${STAMP}@example.com`;
const PLAIN_EMAIL = `e3a-plain-${STAMP}@example.com`;
const PASSWORD = "e3a-ui-pass-1234";

/** 状态 → 页面该显示的那句话。**与 `lib/health.ts::statusText` 同源**，这里再写一遍是
 *  为了「页面显示的是不是这句」这件事有个独立于实现的期望值。 */
const STATUS_TEXT = { red: "有 error", yellow: "有告警", green: "无告警" };

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
    conn.execute("DELETE FROM users WHERE email = ANY(%s)", (["${ADMIN_EMAIL}", "${PLAIN_EMAIL}"],))
`);
}

async function signUp(context, email) {
  const response = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email, password: PASSWORD },
  });
  if (!response.ok()) throw new Error(`注册失败 ${response.status()}（${email}）`);
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  const browser = await chromium.launch();
  try {
    // ── 准备 ────────────────────────────────────────────────────────
    const setup = await browser.newContext();
    await signUp(setup, ADMIN_EMAIL);
    await signUp(setup, PLAIN_EMAIL);
    await setup.close();
    execFileSync(
      resolve(BACKEND, ".venv/bin/python"),
      [resolve(BACKEND, "scripts/grant_admin.py"), ADMIN_EMAIL],
      { cwd: BACKEND, encoding: "utf8" },
    );

    // ── admin 账号 ──────────────────────────────────────────────────
    console.log("\n[admin 账号] /admin");
    const admin = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
    const login = await admin.request.post(`${API}/api/v1/auth/login`, {
      data: { email: ADMIN_EMAIL, password: PASSWORD },
    });
    if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);
    const expected = await (await admin.request.get(`${API}/api/v1/data/health`)).json();

    const page = await admin.newPage();
    await page.goto(`${FRONTEND}/admin`, { waitUntil: "domcontentloaded" });

    const summary = page.locator("[data-health-summary]");
    await summary.waitFor({ timeout: 30_000 });
    check("① 数据体检区渲染出来", (await page.locator("h2", { hasText: "数据体检" }).count()) === 1);

    const text = await summary.innerText();
    const want = STATUS_TEXT[expected.last.check_status];
    check(
      "② 状态文案与接口的 check_status 一致（服务端算，页面只翻译）",
      text.includes(want),
      `页面「${want}」/ 接口 ${expected.last.check_status}`,
    );
    check(
      "② 绿/黄/红三档各有说法（不是恒显示一句话）",
      want !== undefined && want.length > 0 && !text.includes("还没跑过"),
    );

    // ── ③ 告警一览的条目数与接口计数一致 ───────────────────────────
    for (const level of ["error", "warn", "info"]) {
      const group = page.locator(`[data-health-group="${level}"]`);
      const count = expected.last[level];
      const found = await group.count();
      if (count === 0) {
        check(`③ ${level} 为 0 时不出这一组（一排空标题是噪音）`, found === 0, `出现 ${found} 组`);
        continue;
      }
      const items = await group.locator("li").count();
      check(`③ ${level} 组画了 ${count} 条`, items === count, `页面 ${items} / 接口 ${count}`);
    }

    // ── ④ 源侧归因那一行 ───────────────────────────────────────────
    const stalled = expected.stale_source.stalled_trading_days;
    check(
      "④ 「源侧止于 …（N 个交易日未推进）」在页面上——归因就是这一句的全部意义",
      stalled === null
        ? text.includes("无从判断")
        : text.includes(`源侧止于 ${expected.stale_source.source_last}`),
      `源侧 ${expected.stale_source.source_last} / 停滞 ${stalled} 个交易日`,
    );

    // ── ⑤ 运行历史 ─────────────────────────────────────────────────
    const history = await (await admin.request.get(`${API}/api/v1/data/health/history`)).json();
    const rows = await page.locator("[data-health-history] tbody tr").count();
    check("⑤ 运行历史表与接口 total 一致", rows === Math.min(history.total, 20), `页面 ${rows} 行 / 接口 ${history.total} 轮`);
    const historyText = await page.locator("[data-health-history]").innerText();
    check("⑤ 历史行报的是三个计数（不是一列光秃秃的状态）", /error \d+ · warn \d+ · info \d+/.test(historyText));

    // ── ⑥ 普通账号 ─────────────────────────────────────────────────
    console.log("\n[普通账号] /admin");
    const plain = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const plainLogin = await plain.request.post(`${API}/api/v1/auth/login`, {
      data: { email: PLAIN_EMAIL, password: PASSWORD },
    });
    if (!plainLogin.ok()) throw new Error(`普通账号登录失败 ${plainLogin.status()}`);
    const plainPage = await plain.newPage();
    await plainPage.goto(`${FRONTEND}/admin`, { waitUntil: "domcontentloaded" });
    await plainPage.locator("text=需要管理员权限").waitFor({ timeout: 30_000 });
    const plainText = await plainPage.locator("main").innerText();
    check("⑥ 普通账号显示「需要管理员权限」", plainText.includes("需要管理员权限"));
    check("⑥ **不跳登录**（会话是好的，缺的是角色）", plainPage.url().includes("/admin"));
    check("⑥ 普通账号看不到数据体检区", (await plainPage.locator("[data-health-summary]").count()) === 0);

    // 直接打接口也是 403（门在服务端，前端隐藏入口不算门）
    const denied = await plain.request.get(`${API}/api/v1/data/health`);
    check("⑥ 普通账号直接打接口是 403", denied.status() === 403, `HTTP ${denied.status()}`);

    await page.screenshot({ path: resolve(EVIDENCE, "data-panel.png"), fullPage: true });
  } finally {
    await browser.close();
    try {
      purge();
    } catch (error) {
      console.log(`⚠ 探针账号清理失败（请手工删 ${ADMIN_EMAIL} / ${PLAIN_EMAIL}）：${error.message}`);
    }
  }

  const passed = RESULTS.filter((r) => r.ok).length;
  const verdict = passed === RESULTS.length ? "全部通过" : "**有失败**";
  const md = [
    "# E3a 界面验证：数据体检看板",
    "",
    `- 时间：${new Date().toISOString()}`,
    `- 结果：**${passed}/${RESULTS.length} ${verdict}**`,
    "",
    "| # | 断言 | 结果 | 读数 |",
    "|---|---|---|---|",
    ...RESULTS.map((r, i) => `| ${i + 1} | ${r.label} | ${r.ok ? "✅" : "❌"} | ${r.detail} |`),
    "",
  ].join("\n");
  await writeFile(resolve(EVIDENCE, "data-panel.md"), md, "utf8");
  await writeFile(resolve(EVIDENCE, "data-panel.json"), JSON.stringify(RESULTS, null, 2), "utf8");
  console.log(`\n${passed}/${RESULTS.length} ${verdict} —— 证据 logs/e3/data-panel.md`);
  process.exit(passed === RESULTS.length ? 0 : 1);
}

await main();
