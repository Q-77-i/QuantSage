/**
 * Monaco 集成 spike 的验证脚本（M4c-1）。
 *
 * 前置：后端 8000 与前端 3001 都已启动（`pnpm dev` 会先自动同步 public/monaco）。
 * 用法：cd frontend && node scripts/spike-monaco.mjs
 *
 * 验四件事，逐条对应 SPEC §5 的 spike 判据：
 *   1. **离线**：拦掉一切非本机请求——不是「观察没发生」，而是**发不出去**；
 *   2. 编辑器真的挂上（`.monaco-editor` 出现，且是 Python 语言）；
 *   3. `setModelMarkers` 生效（error / warning 波浪线各自出现）；
 *   4. 资源体积如实记录（`/monaco/vs` 的传输字节 + `public/monaco` 的磁盘体积）。
 *
 * 账号用固定邮箱：注册撞号（409）就改成登录，跑多少遍都不堆垃圾账号。
 */

import { mkdir, readdir, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const OUT_DIR = resolve(ROOT, "logs/m4c");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const EMAIL = "monaco-spike@example.com";
const PASSWORD = "spike-monaco-pass";

/** 本机请求的白名单：一切别的域名都会被 abort */
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

async function dirSize(path) {
  let total = 0;
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const child = resolve(path, entry.name);
    total += entry.isDirectory() ? await dirSize(child) : (await stat(child)).size;
  }
  return total;
}

async function signIn(context) {
  const register = await context.request.post(`${API}/api/v1/auth/register`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (register.ok()) return;
  if (register.status() !== 409) {
    throw new Error(`注册失败 ${register.status()}：${await register.text()}`);
  }
  const login = await context.request.post(`${API}/api/v1/auth/login`, {
    data: { email: EMAIL, password: PASSWORD },
  });
  if (!login.ok()) throw new Error(`登录失败 ${login.status()}：${await login.text()}`);
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });

  // ── 判据 1：离线。非本机请求直接 abort，并记账 ──────────────────────────
  const blocked = [];
  await context.route("**/*", (route) => {
    const host = new URL(route.request().url()).hostname;
    if (LOCAL_HOSTS.has(host)) return route.continue();
    blocked.push(route.request().url());
    return route.abort();
  });

  // 记 /monaco/vs 的传输量（判据 4）。**线上字节走 CDP 的 `encodedDataLength`**：
  // 开发服务器带 gzip，`content-length` 在压缩响应里会被省略，而 `body().length` 是解压后的
  // ——两个都会把体积说错（后者大约 3 倍），只有 CDP 给的是真正上线的字节
  let monacoWire = 0;
  let monacoRaw = 0;
  let monacoFiles = 0;
  context.on("response", async (response) => {
    if (!response.url().includes("/monaco/vs/")) return;
    monacoFiles += 1;
    try {
      monacoRaw += (await response.body()).length;
    } catch {
      // 已释放 / 被 abort 的响应不记原始字节（线上字节另有 CDP 记账）
    }
  });

  const errors = [];
  context.on("pageerror", (error) => errors.push(error.message));

  try {
    await signIn(context);
    const page = await context.newPage();

    // 线上字节按 CDP 的 encodedDataLength 累计（见上）
    const cdp = await context.newCDPSession(page);
    await cdp.send("Network.enable");
    const monacoRequests = new Set();
    cdp.on("Network.responseReceived", ({ requestId, response }) => {
      if (response.url.includes("/monaco/vs/")) monacoRequests.add(requestId);
    });
    cdp.on("Network.loadingFinished", ({ requestId, encodedDataLength }) => {
      if (monacoRequests.has(requestId)) monacoWire += encodedDataLength;
    });

    await page.goto(`${FRONTEND}/strategies`, { waitUntil: "domcontentloaded" });

    // 判据 2：编辑器挂上（Monaco 的根容器 + 模型语言确实是 python）
    await page.waitForSelector(".monaco-editor", { timeout: 60_000 });
    const language = await page.evaluate(
      () => globalThis.monaco?.editor.getModels()[0]?.getLanguageId() ?? "（读不到）",
    );
    await page.screenshot({ path: `${OUT_DIR}/monaco-spike.png` });

    // 判据 3：标注生效——先断言没有，再点按钮，再断言有
    const before = await page.locator(".squiggly-error, .squiggly-warning").count();
    await page.getByRole("button", { name: "演示标注" }).click();
    await page.waitForSelector(".squiggly-error", { timeout: 15_000 });
    const after = await page.locator(".squiggly-error, .squiggly-warning").count();
    await page.screenshot({ path: `${OUT_DIR}/monaco-spike-markers.png` });

    const diskBytes = await dirSize(resolve(ROOT, "frontend/public/monaco"));

    console.log("Monaco spike 结果");
    console.log(`  编辑器挂载        : 语言 = ${language}`);
    console.log(`  标注（点击前/后） : ${before} → ${after} 条波浪线`);
    console.log(`  外部请求（已拦截）: ${blocked.length}${blocked.length ? ` → ${blocked.join(", ")}` : ""}`);
    console.log(
      `  /monaco/vs 加载   : ${monacoFiles} 个文件 / 线上 ${(monacoWire / 1024 / 1024).toFixed(2)} MB` +
        `（解压后 ${(monacoRaw / 1024 / 1024).toFixed(2)} MB）`,
    );
    console.log(`  public/monaco 磁盘: ${(diskBytes / 1024 / 1024).toFixed(2)} MB`);
    console.log(`  页面错误          : ${errors.length ? errors.join(" | ") : "无"}`);
    console.log(`  截图              : logs/m4c/monaco-spike{,-markers}.png`);

    if (before !== 0 || after < 2 || blocked.length) process.exitCode = 1;
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch((error) => {
  console.error("spike 失败：", error.message);
  process.exit(1);
});
