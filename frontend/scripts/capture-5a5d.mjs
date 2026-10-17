/**
 * 收尾项 5a/5d 界面走查：策略版本抽屉 + Agent 提案的 HITL。
 *
 * 场景（真浏览器 + 真后端 + 真行情）：
 *   ① 策略：API 建 v1 → 改成 v2 → 打开工作台「版本历史」→ 选中 v1 → 看到真实 unified diff
 *      → 回滚 → 头部显示 v3，API 的 code 回到 v1；
 *   ② 模拟盘：建一个 ma_cross 会话 → 用服务层**种一条** Agent 待审批决策（确定性，不赌模型）
 *      → 页面出现「Agent 提案」徽章、「跑到结束」被禁用并给出原因；
 *   ③ 批准 → 推进一天 → 决策成交（API 里 status=filled、origin=agent）→「跑到结束」恢复可用；
 *   ④ 真聊天：让 Agent 调 `list_paper_accounts`，确认工具上下文（user_id）在真实链路里生效。
 *
 * 自备账号并清理。前置：后端 8000（**新代码——改了服务端契约必须先重启**）+ 前端 3001。
 * 用法：cd frontend && node scripts/capture-5a5d.mjs
 * 证据：logs/5a5d/ui.md / ui.json / *.png
 */

import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BACKEND = resolve(ROOT, "backend");
const EVIDENCE = resolve(ROOT, "logs/5a5d");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";
const STAMP = Date.now().toString(36);
const EMAIL = `5a5d-ui-${STAMP}@example.com`;
const PASSWORD = "5a5d-ui-pass-1234";

const CODE_V1 = `PARAMS = {"window": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "窗口"}}
USES_EVENTS = False


def on_bar(ctx, p):
    return []
`;
const CODE_V2 = `${CODE_V1}\n# v2\n`;

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

async function apiJson(context, path, init) {
  const response = await context.request.fetch(`${API}${path}`, init);
  if (!response.ok()) throw new Error(`${path} → ${response.status()}: ${await response.text()}`);
  return response.json();
}

async function seedAgentProposal(accountId) {
  py(`
import asyncio
from app.backtest.types import Side
from app.core.config import get_settings
from app.core.db import Database, open_pool
from app.paper.proposal import propose_agent_decision

async def main():
    async with open_pool(get_settings().postgres_dsn) as pool:
        db = Database(pool)
        row = await db._one("SELECT id FROM users WHERE email = %s", ("${EMAIL}",))
        assert row is not None

        await propose_agent_decision(
            db,
            user_id=int(row["id"]),
            account_id="${accountId}",
            symbol="600519",
            side=Side.BUY,
            reason="UI 走查：Agent 提案（待人工裁决）",
        )

asyncio.run(main())
`);
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

    // ── ① 策略版本 ─────────────────────────────────────────────
    const created = await apiJson(context, "/api/v1/strategies", {
      method: "POST",
      data: { name: `版本走查 ${STAMP}`, code: CODE_V1, params: {} },
    });
    check("策略创建为 v1", created.version === 1, `v${created.version}`);
    const strategyId = created.id;

    const updated = await apiJson(context, `/api/v1/strategies/${strategyId}`, {
      method: "PUT",
      data: { code: CODE_V2, expected_version: 1 },
    });
    check("内容变化后到 v2", updated.version === 2, `v${updated.version}`);

    const page = await context.newPage();
    await page.goto(`${FRONTEND}/strategies?id=${strategyId}`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "版本历史" }).waitFor({ timeout: 60_000 });
    check("工作台显示当前版本", await page.getByText("已保存 · v2").isVisible());
    await page.getByRole("button", { name: "版本历史" }).click();
    await page.waitForSelector("[data-version-panel]", { timeout: 15_000 });
    await page.waitForSelector('[data-version-row="1"]', { timeout: 15_000 });
    await page.locator('[data-version-row="1"]').click();
    await page.waitForSelector("[data-diff-kind]", { timeout: 15_000 });
    const diffText = await page.textContent("[data-version-panel]");
    check("版本抽屉展示 v1/v2 与真实 diff", Boolean(diffText?.includes("对 v2") && diffText.includes("# v2")));
    await page.screenshot({ path: resolve(EVIDENCE, "strategy-versions.png"), fullPage: false });

    await page.getByRole("button", { name: "回滚到此版本" }).click();
    await page.getByRole("button", { name: "确认回滚到 v1" }).click();
    await page.waitForSelector("[data-version-panel]", { state: "detached", timeout: 15_000 });
    await page.getByText("已保存 · v3").waitFor({ timeout: 30_000 });
    const afterRollback = await apiJson(context, `/api/v1/strategies/${strategyId}`);
    check(
      "回滚 = 追加 v3，代码回到 v1",
      afterRollback.version === 3 && afterRollback.code === CODE_V1,
      `v${afterRollback.version}`,
    );

    // ── ② 模拟盘 Agent 提案 ─────────────────────────────────────
    const paper = await apiJson(context, "/api/v1/paper/accounts", {
      method: "POST",
      data: {
        name: `5a 走查 ${STAMP}`,
        initial_cash: 1_000_000,
        symbols: ["600519"],
        strategy: "ma_cross",
        params: { fast: 1, slow: 2 },
        start: "2025-09-22",
      },
    });
    const accountId = paper.account.id;
    check("模拟盘会话创建", paper.account.status === "active", paper.account.as_of);

    await seedAgentProposal(accountId);
    await page.goto(`${FRONTEND}/paper?id=${accountId}`, { waitUntil: "networkidle" });
    await page.waitForSelector("[data-agent-badge]", { timeout: 60_000 });
    const paperText = await page.textContent("body");
    check("决策卡出现「Agent 提案」徽章", Boolean(paperText?.includes("Agent 提案")));
    const runButton = page.getByRole("button", { name: "跑到结束" });
    check("有待审批 Agent 提案时「跑到结束」被禁用", await runButton.isDisabled());
    check("页面写明必须逐条裁决", Boolean(paperText?.includes("有 Agent 提案待审批")));
    await page.screenshot({ path: resolve(EVIDENCE, "paper-agent.png"), fullPage: false });

    const agentCard = page.locator("[data-decision]", { hasText: "Agent 提案" }).first();
    await agentCard.getByRole("button", { name: "批准" }).click();
    await agentCard.getByText("已批准，等下一交易日开盘成交").waitFor({ timeout: 15_000 });
    const approved = await apiJson(context, `/api/v1/paper/accounts/${accountId}`);
    check(
      "批准只改状态（仍未成交）",
      approved.decisions.some((d) => d.origin === "agent" && d.status === "approved"),
    );

    await page.getByRole("button", { name: "推进一天" }).click();
    let filled = null;
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const detail = await apiJson(context, `/api/v1/paper/accounts/${accountId}`);
      filled = detail.decisions.find((d) => d.origin === "agent");
      if (filled?.status === "filled") break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    check("推进一天后 Agent 决策成交", filled?.status === "filled", `fill=${filled?.fill?.qty ?? "—"}`);
    check("没有 pending Agent 提案后「跑到结束」恢复", await runButton.isEnabled());
    await page.waitForSelector("[data-agent-badge]", { timeout: 15_000 });

    // ── ③ 真聊天：工具上下文 ───────────────────────────────────
    const chat = await context.request.post(`${API}/api/v1/chat`, {
      data: {
        message: "请调用 list_paper_accounts 工具列出我的模拟盘会话，然后只复述会话名称，不要做别的。",
      },
      timeout: 120_000,
    });
    const chatText = await chat.text();
    check(
      "真聊天能调 list_paper_accounts（user_id 上下文生效）",
      chat.ok() && chatText.includes("5a 走查"),
      chat.ok() ? "" : `HTTP ${chat.status()}`,
    );
  } finally {
    await context.close();
    await browser.close();
    try {
      purge();
    } catch (error) {
      console.warn("清理探针账号失败：", error.message);
    }
  }

  const passed = RESULTS.filter((item) => item.ok).length;
  const md = [
    "# 收尾项 5a/5d 界面走查",
    "",
    `> ${new Date().toISOString()} · 真浏览器 + 真后端 + 真行情`,
    `> 结果：**${passed}/${RESULTS.length}**`,
    "",
    "| # | 检查 | 结果 | 备注 |",
    "|---|---|---|---|",
    ...RESULTS.map(
      (item, index) => `| ${index + 1} | ${item.label} | ${item.ok ? "✅" : "❌"} | ${item.detail} |`,
    ),
    "",
    "截图：`strategy-versions.png`（版本抽屉与 diff）、`paper-agent.png`（Agent 提案 HUD）。",
    "",
  ].join("\n");
  await writeFile(resolve(EVIDENCE, "ui.md"), md, "utf8");
  await writeFile(resolve(EVIDENCE, "ui.json"), JSON.stringify(RESULTS, null, 2), "utf8");
  console.log(`\n${passed}/${RESULTS.length} 通过；证据：${EVIDENCE}/ui.md`);
  if (passed !== RESULTS.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
