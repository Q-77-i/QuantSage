/**
 * CSP 生产模式浏览器级验证（P3 收尾项 6）。
 *
 * 现有的 `lib/csp.test.ts` 只断言字符串；这条脚本回答的是另一个问题：
 * **生产构建下，浏览器里真的没有 CSP 违规吗？**
 *
 * 做法：真生产构建（`pnpm build` + `pnpm start`）→ Playwright 打开关键页面 →
 * 监听 `securitypolicyviolation` 与 console 里的 `Refused to` → 逐页断言。
 *
 * 前置：后端 8000、前端 3001 **生产模式**（`next start`，不是 dev；dev 的 CSP 是
 * `unsafe-inline` 折中版，测了不算数）。
 * 用法：cd frontend && node scripts/verify-csp.mjs
 * 证据：logs/csp/verify.json / verify.md / *.png
 */

import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BACKEND = resolve(ROOT, "backend");
const EVIDENCE = resolve(ROOT, "logs/csp");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";
const STAMP = Date.now().toString(36);
const EMAIL = `csp-verify-${STAMP}@example.com`;
const PASSWORD = "csp-verify-pass-1234";

const RESULTS = [];
const CONSOLE = [];

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

async function createStrategy(context) {
  const templates = await json(context, "/api/v1/strategies/templates");
  const template = templates.find((item) => item.key === "ma_cross");
  if (!template) throw new Error("找不到 ma_cross 模板");
  const created = await json(context, "/api/v1/strategies", {
    method: "POST",
    data: { name: `CSP 验证·${STAMP}`, code: template.source },
  });
  return created.id;
}

async function main() {
  await mkdir(EVIDENCE, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    colorScheme: "light",
  });
  // 在每个页面脚本之前挂监听：CSP 违规事件不会冒泡到 window 以外
  await context.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (event) => {
      window.__cspViolations.push({
        effectiveDirective: event.effectiveDirective,
        violatedDirective: event.violatedDirective,
        blockedURI: event.blockedURI,
        sourceFile: event.sourceFile,
        lineNumber: event.lineNumber,
        columnNumber: event.columnNumber,
        sample: event.sample,
      });
    });
  });

  const page = await context.newPage();
  page.on("console", (message) => {
    const text = message.text();
    if (message.type() === "error" && /Content Security Policy|Refused to/i.test(text)) {
      CONSOLE.push({ url: page.url(), text });
    }
  });
  page.on("pageerror", (error) => {
    if (/Content Security Policy|Refused to/i.test(String(error))) {
      CONSOLE.push({ url: page.url(), text: String(error) });
    }
  });

  let strategyId = null;

  async function visit(label, path, options = {}) {
    const url = `${FRONTEND}${path}`;
    const response = await page.goto(url, {
      waitUntil: "domcontentloaded",
      timeout: options.timeout ?? 60_000,
    });
    if (options.selector) {
      await page.waitForSelector(options.selector, { timeout: options.timeout ?? 60_000 });
    }
    await page.waitForTimeout(options.settle ?? 1500);

    const violations = await page.evaluate(() => window.__cspViolations ?? []);
    const headers = response?.headers() ?? {};
    const csp = headers["content-security-policy"] ?? "";
    const hasNonce = /nonce-/.test(csp);
    const hasUnsafeInline = /script-src[^;]*'unsafe-inline'/.test(csp);

    check(`${label}：无 CSP 违规`, violations.length === 0, JSON.stringify(violations).slice(0, 600));
    check(`${label}：文档响应带 CSP`, Boolean(csp), csp.slice(0, 160));
    check(`${label}：生产 script-src 带 nonce`, hasNonce, csp.slice(0, 160));
    check(`${label}：生产 script-src 不含 unsafe-inline`, !hasUnsafeInline, csp.slice(0, 160));

    await page.screenshot({ path: resolve(EVIDENCE, `${label.replaceAll("/", "_")}.png`), fullPage: false });
    return { label, path, status: response?.status() ?? null, csp, violations };
  }

  const pages = [];
  try {
    // 公共页：先测登录/注册（未登录）
    pages.push(await visit("login", "/login", { selector: "input[type=email]" }));
    pages.push(await visit("register", "/register", { selector: "input[type=email]" }));

    // 登录后测主页面
    await signIn(context);
    strategyId = await createStrategy(context);

    pages.push(await visit("home", "/", { settle: 2000 }));
    pages.push(await visit("chat", "/chat", { settle: 2000 }));
    pages.push(
      await visit("strategies", `/strategies?id=${encodeURIComponent(strategyId)}`, {
        selector: ".monaco-editor",
        timeout: 90_000,
        settle: 3000,
      }),
    );
    pages.push(await visit("backtest", "/backtest", { settle: 2500 }));
    pages.push(await visit("paper", "/paper", { settle: 2500 }));

    // 打印页外壳：无有效令牌时可能显示错误/跳登录，但 CSP 头与脚本仍应成立
    pages.push(
      await visit(
        "print-shell",
        "/print/00000000-0000-0000-0000-000000000000?t=invalid",
        { settle: 1500 },
      ),
    );

    check("console 无 CSP 拒绝", CONSOLE.length === 0, JSON.stringify(CONSOLE).slice(0, 600));
  } finally {
    const evidence = {
      stamp: STAMP,
      frontend: FRONTEND,
      api: API,
      results: RESULTS,
      console: CONSOLE,
      pages,
    };
    await writeFile(resolve(EVIDENCE, "verify.json"), JSON.stringify(evidence, null, 2));
    const failed = RESULTS.filter((item) => !item.ok);
    const md = [
      `# CSP 生产验证（${STAMP}）`,
      "",
      `- 前端：${FRONTEND}`,
      `- 结果：${RESULTS.length - failed.length}/${RESULTS.length} 通过`,
      "",
      "| 检查 | 结果 | 说明 |",
      "|---|---|---|",
      ...RESULTS.map((item) => `| ${item.label} | ${item.ok ? "✅" : "❌"} | ${item.detail || ""} |`),
      "",
      "## console / pageerror",
      "",
      CONSOLE.length ? "```\n" + JSON.stringify(CONSOLE, null, 2) + "\n```" : "无",
      "",
    ].join("\n");
    await writeFile(resolve(EVIDENCE, "verify.md"), md);
    await browser.close();
    purge();
  }

  const failed = RESULTS.filter((item) => !item.ok);
  console.log(`\n${RESULTS.length - failed.length}/${RESULTS.length} 通过；证据 ${EVIDENCE}`);
  if (failed.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
