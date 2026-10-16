/**
 * E2c 界面验证：`/admin` 运维页（真浏览器、真后端）。
 *
 * 验六件事——每一条都对应 SPEC §3.4 / §3.5 的一句话：
 *   ① admin 账号的页头**多一项**「运维」；
 *   ② 配额用量表渲染，且「共 N 个账号」与接口的 `total` 一致（数字不是画上去的）；
 *   ③ **搜索**按邮箱走服务端过滤（搜自己 → 只剩 1 个）；
 *   ④ **分页**真的换页（URL 进 `?page=2`、首行账号跟着变）——不是把全量摆在客户端切；
 *   ⑤ **拒绝记录**表渲染出内容（先直接往库里种一条，验的是 UI 会画行）；
 *   ⑥ 普通账号打 `/admin`：显示「需要管理员权限」、**不跳登录**、页头**没有**那一项。
 *
 * 自备账号：注册两个探针账号（一个提权、一个不提），跑完清干净。提权与种行走
 * `scripts/grant_admin.py` 与一段 python——界面验证不该为了造数据而手改库。
 *
 * 前置：后端 8000（新代码）+ 前端 3001 都在跑。
 * 用法：cd frontend && node scripts/capture-admin.mjs
 * 证据：logs/e2/admin-ui.md / admin-ui.json / *.png
 */

import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const BACKEND = resolve(ROOT, "backend");
const EVIDENCE = resolve(ROOT, "logs/e2");
const FRONTEND = process.env.APP_BASE ?? "http://127.0.0.1:3001";
const API = process.env.API_BASE ?? "http://127.0.0.1:8000";

const STAMP = Date.now().toString(36);
const ADMIN_EMAIL = `e2c-admin-${STAMP}@example.com`;
const PLAIN_EMAIL = `e2c-plain-${STAMP}@example.com`;
const PASSWORD = "e2c-ui-pass-1234";

const RESULTS = [];

function check(label, ok, detail = "") {
  RESULTS.push({ label, ok: Boolean(ok), detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

/** 跑一段后端侧的 python（提权 / 种行 / 清账号都走它，别在脚本里手搓 SQL 字符串）。 */
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
    // ── 准备：两个探针账号；其一提权；给提权那个种一条拒绝记录 ──────────
    const setup = await browser.newContext();
    await signUp(setup, ADMIN_EMAIL);
    await signUp(setup, PLAIN_EMAIL);
    await setup.close();
    execFileSync(
      resolve(BACKEND, ".venv/bin/python"),
      [resolve(BACKEND, "scripts/grant_admin.py"), ADMIN_EMAIL],
      { cwd: BACKEND, encoding: "utf8" },
    );
    py(`
import psycopg
from app.core.config import get_settings
from app.core import quota
from datetime import timedelta
with psycopg.connect(get_settings().postgres_dsn) as conn:
    conn.execute(
        "INSERT INTO quota_rejections (user_id, metric, used, limit_value) "
        "SELECT id, 'active_tasks', 3, 3 FROM users WHERE email = %s",
        ("${ADMIN_EMAIL}",),
    )
    uid = conn.execute("SELECT id FROM users WHERE email = %s", ("${ADMIN_EMAIL}",)).fetchone()[0]
    # 今天 2 次、昨天 3 次：时间窗断言要钉具体数字，不能拿「0 → 0」当通过
    for day, used in ((quota.period_key(), 2),
                      (quota.period_key(quota.day_start() - timedelta(days=1)), 3)):
        conn.execute(
            "INSERT INTO usage_counters (user_id, metric, period_key, used) VALUES (%s, 'agent_calls', %s, %s) "
            "ON CONFLICT (user_id, metric, period_key) DO UPDATE SET used = EXCLUDED.used",
            (uid, day, used),
        )
`);

    // ── ① 页头多一项 + ② 用量表 + ⑤ 拒绝记录 ───────────────────────────
    console.log("\n[admin 账号] /admin");
    const admin = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const login = await admin.request.post(`${API}/api/v1/auth/login`, {
      data: { email: ADMIN_EMAIL, password: PASSWORD },
    });
    if (!login.ok()) throw new Error(`登录失败 ${login.status()}`);
    const page = await admin.newPage();
    await page.goto(`${FRONTEND}/admin`, { waitUntil: "domcontentloaded" });

    await page.locator('[data-nav="admin"]').waitFor({ timeout: 30_000 });
    check("① 页头多一项「运维」", (await page.locator('[data-nav="admin"]').innerText()) === "运维");

    // **锚在自己的区块上**（M5b 的教训）：同一页有两个 `Pager`，按索引取第一个
    // 会在改版时悄悄指错地方；认 `[data-usage-summary]` 所在的那个 section。
    const usage = page.locator("section").filter({ has: page.locator("[data-usage-summary]") });
    const rejectionsSection = page
      .locator("section")
      .filter({ has: page.locator("h2", { hasText: "最近的拒绝记录" }) });
    await page.locator("[data-usage-summary]").waitFor({ timeout: 30_000 });
    const summary = await page.locator("[data-usage-summary]").innerText();
    const total = (await (await admin.request.get(`${API}/api/v1/admin/usage`)).json()).total;
    check(
      "② 用量表渲染且「共 N 个账号」与接口的 total 一致",
      summary.includes(`共 ${total} 个账号`),
      `页面「${summary.match(/共 \d+ 个账号/)?.[0]}」 / 接口 ${total}`,
    );
    check(
      "② 说明行把两个口径分开讲（在飞任务=此刻快照；调用=区间累计）",
      summary.includes("此刻的快照") && summary.includes("累计") && summary.includes("200"),
    );
    const pageRows = await usage.locator("tbody tr").count();
    check("② 每页 20 条（用户 2026-10-16 定的页长）", pageRows === 20, `${pageRows} 行`);
    const pageText = await page.locator("main").innerText();
    check(
      "⑧ 页面文案不泄漏实现细节（环境变量名 / 脚本路径都不该出现在界面上）",
      !pageText.includes("QUOTA_") && !pageText.includes("grant_admin"),
      pageText.split("\n")[1]?.slice(0, 60) ?? "",
    );
    await page.screenshot({ path: resolve(EVIDENCE, "admin-usage.png") });

    const rejections = await rejectionsSection.locator("table").innerText();
    check(
      "⑤ 拒绝记录表画出那条种子行",
      rejections.includes(ADMIN_EMAIL) && rejections.includes("active_tasks"),
      rejections.split("\n").slice(0, 2).join(" | "),
    );

    // ── ③ 搜索（服务端过滤）───────────────────────────────────────────
    console.log("\n[admin 账号] 搜索");
    await page.getByLabel("按邮箱搜索账号").fill(`e2c-admin-${STAMP}`);
    // 有筛选时文案是「筛选后 N 个账号」（与「共 N」区分开：数字是谁的口径一目了然）
    await page.waitForFunction(
      (needle) => document.querySelector("[data-usage-summary]")?.textContent?.includes(needle),
      `筛选后 1 个账号`,
      { timeout: 30_000 },
    );
    const searched = await usage.locator("table").innerText();
    check(
      "③ 搜索把结果收到 1 行，且就是被搜的那个账号",
      searched.includes(ADMIN_EMAIL) && !searched.includes(PLAIN_EMAIL),
    );
    await page.screenshot({ path: resolve(EVIDENCE, "admin-search.png") });

    // ── ④ 分页（清掉搜索，翻到第 2 页）─────────────────────────────────
    console.log("\n[admin 账号] 翻页");
    await page.getByLabel("按邮箱搜索账号").fill("");
    await page.waitForFunction(
      () => document.querySelector("[data-usage-summary]")?.textContent?.includes("（匹配") === false,
      null,
      { timeout: 30_000 },
    );
    const firstRow = await usage.locator("tbody tr").first().innerText();
    await usage.getByRole("button", { name: "下一页" }).click();
    await page.waitForURL(/page=2/, { timeout: 30_000 });
    // **等数据真的换页**，而不是等一个立即为真的条件：`textContent` 比 `innerText`
    // 恒不等（格式差异），那等于没等——首版就是这么假通过的
    let secondRow = firstRow;
    for (let i = 0; i < 60 && secondRow === firstRow; i += 1) {
      await page.waitForTimeout(250);
      secondRow = await usage.locator("tbody tr").first().innerText();
    }
    check("④ 翻页真的换了一页（URL 进 ?page=2，首行变了）", firstRow !== secondRow, `${firstRow} → ${secondRow}`);
    const label = await usage.locator("[data-page-label]").innerText();
    check("④ 页码标签跟着走", label.includes("第 2 /"), JSON.stringify(label));
    await page.screenshot({ path: resolve(EVIDENCE, "admin-page2.png") });

    // ── ⑦ 筛选：只看有读数 / 按角色 ────────────────────────────────────
    console.log("\n[admin 账号] 筛选");
    const roleCount = (
      await (await admin.request.get(`${API}/api/v1/admin/usage?role=admin`)).json()
    ).total;
    await usage.getByLabel("按角色筛选").selectOption("admin");
    await page.waitForFunction(
      (needle) => document.querySelector("[data-usage-summary]")?.textContent?.includes(needle),
      `筛选后 ${roleCount} 个账号`,
      { timeout: 30_000 },
    );
    // 等**稳定态**再断言：筛选一变会先按旧 offset 发一发、再被「回第 1 页」纠正——
    // 中间那一帧里 `total` 已对而行还没换（URL 是页码唯一真源的固有形态，与标的浏览同形）
    let adminRows = [];
    for (let i = 0; i < 40 && adminRows.length !== roleCount; i += 1) {
      await page.waitForTimeout(250);
      adminRows = await usage.locator("tbody tr").allInnerTexts();
    }
    check(
      "⑦ 角色筛选只留 admin 行，且行数与接口的 total 一致",
      adminRows.length === roleCount && adminRows.every((row) => row.split("\t")[1] === "admin"),
      `${adminRows.length} 行 / 接口 ${roleCount}`,
    );

    await usage.getByLabel("按角色筛选").selectOption("");
    const busyTotal = (
      await (await admin.request.get(`${API}/api/v1/admin/usage?active_only=true`)).json()
    ).total;
    await usage.getByLabel("只看有读数的").check();
    // 同上：等**期望的那个数字**出现。「筛选后」三个字在数据回来之前就已经在页面上了
    // （文案由本地筛选状态决定、数字由旧数据决定），等它等于没等
    await page.waitForFunction(
      (needle) => document.querySelector("[data-usage-summary]")?.textContent?.includes(needle),
      `筛选后 ${busyTotal} 个账号`,
      { timeout: 30_000 },
    );
    const busyRows = await usage.locator("tbody tr").allInnerTexts();
    const readings = busyRows.map((row) => row.split("\t").slice(2).map(Number));
    check(
      "⑦「只看有读数的」把全 0 的账号挡在外面（每行至少一个读数非零）",
      readings.length > 0 && readings.every((cells) => cells.some((value) => value > 0)),
      `${readings.length} 行`,
    );
    check(
      "⑦ 筛选一改就回第 1 页（URL 里的 page 已被摘掉）",
      !page.url().includes("page="),
      page.url(),
    );
    check(
      "⑦ 刚种过拒绝记录的那个账号在筛选结果里（它有读数）",
      busyRows.some((row) => row.includes(ADMIN_EMAIL)),
    );
    await usage.getByLabel("只看有读数的").uncheck();
    await usage.getByLabel("按角色筛选").selectOption("");
    await page.screenshot({ path: resolve(EVIDENCE, "admin-filtered.png") });

    // ── ⑨ 排序：按「今日被拒」降序 / 升序 ──────────────────────────────
    console.log("\n[admin 账号] 排序");
    await usage.getByLabel("排序键").selectOption("rejected");
    await usage.getByRole("button", { name: "升序" }).click(); // 切到降序
    let sortedRows = [];
    for (let i = 0; i < 40; i += 1) {
      await page.waitForTimeout(250);
      const rows = await usage.locator("tbody tr").allInnerTexts();
      const values = rows.map((row) => Number(row.split("\t")[4]));
      const descending = values.every((value, index) => index === 0 || values[index - 1] >= value);
      if (values.length > 0 && descending && values[0] > 0) {
        sortedRows = rows;
        break;
      }
    }
    check(
      "⑨ 降序：被拒次数非增，被拒过的账号排在前面",
      sortedRows.length > 0 && Number(sortedRows[0].split("\t")[4]) > 0,
      `首行 ${sortedRows[0]?.split("\t")[0] ?? "（没等到）"}`,
    );
    // 断言「探针账号在本页」而不是「它是首行」：另一个账号的被拒数同样是 1，
    // 同值时按 id 兜底——那是**对的行为**，别把断言写死在具体名次上
    check(
      "⑨ 今天被拒过的那个账号在这一页里",
      sortedRows.some((row) => row.includes(ADMIN_EMAIL)),
    );

    await usage.getByRole("button", { name: "降序" }).click(); // 切回升序
    let ascending = [];
    for (let i = 0; i < 40; i += 1) {
      await page.waitForTimeout(250);
      const rows = await usage.locator("tbody tr").allInnerTexts();
      const values = rows.map((row) => Number(row.split("\t")[4]));
      if (values.length > 0 && values.every((v, i) => i === 0 || values[i - 1] <= v)) {
        ascending = values;
        break;
      }
    }
    check(
      "⑨ 升序：被拒次数非减（全 0 的那些排在前面）",
      ascending.length > 0 && ascending[0] === 0,
      `首行 ${ascending[0]}`,
    );
    await usage.getByLabel("排序键").selectOption("id");
    await page.screenshot({ path: resolve(EVIDENCE, "admin-sorted.png") });

    // ── ⑩ 时间范围：今日 2 次 / 近 7 天 5 次（昨天 3 次也落在窗内）────────
    console.log("\n[admin 账号] 时间范围");
    await usage.getByLabel("按邮箱搜索账号").fill(`e2c-admin-${STAMP}`);
    await page.waitForFunction(
      (needle) => document.querySelector("[data-usage-summary]")?.textContent?.includes(needle),
      "筛选后 1 个账号",
      { timeout: 30_000 },
    );
    const callsNow = async () =>
      Number((await usage.locator("tbody tr").first().innerText()).split("\t")[3]);
    const todayCalls = await callsNow();

    await usage.getByLabel("时间范围").selectOption("7");
    await usage.locator("thead").filter({ hasText: "近 7 天 Agent 调用" }).waitFor({ timeout: 30_000 });
    let weekCalls = 0;
    for (let i = 0; i < 40 && weekCalls !== 5; i += 1) {
      await page.waitForTimeout(250);
      weekCalls = await callsNow();
    }
    check(
      "⑩ 今日 2 / 近 7 天 5：窗口真的跨天求和（不是换了个列名）",
      todayCalls === 2 && weekCalls === 5,
      `${todayCalls} → ${weekCalls}`,
    );
    check(
      "⑩ 列头与说明行跟着改口径",
      (await page.locator("[data-usage-summary]").innerText()).includes("近 7 天"),
    );
    check(
      "⑩ 在飞任务列标明是当前值（gauge 不随窗口变）",
      (await usage.locator("thead").innerText()).includes("在飞任务（当前）"),
    );
    await page.screenshot({ path: resolve(EVIDENCE, "admin-range.png") });

    // ── ⑥ 普通账号：403 不当成「没登录」───────────────────────────────
    console.log("\n[普通账号] /admin");
    const plain = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const plainLogin = await plain.request.post(`${API}/api/v1/auth/login`, {
      data: { email: PLAIN_EMAIL, password: PASSWORD },
    });
    if (!plainLogin.ok()) throw new Error(`登录失败 ${plainLogin.status()}`);
    const plainPage = await plain.newPage();
    await plainPage.goto(`${FRONTEND}/admin`, { waitUntil: "domcontentloaded" });
    await plainPage.waitForSelector("text=需要管理员权限", { timeout: 30_000 });
    check("⑥ 渲染「需要管理员权限」", true);
    check("⑥ **不跳登录**（会话是好的，缺的是角色）", plainPage.url().includes("/admin"), plainPage.url());
    check("⑥ 页头没有「运维」那一项", (await plainPage.locator('[data-nav="admin"]').count()) === 0);
    await plainPage.screenshot({ path: resolve(EVIDENCE, "admin-forbidden.png") });

    await admin.close();
    await plain.close();
  } finally {
    await browser.close();
    purge();
  }

  const failed = RESULTS.filter((item) => !item.ok);
  await writeFile(
    resolve(EVIDENCE, "admin-ui.json"),
    `${JSON.stringify({ results: RESULTS }, null, 2)}\n`,
    "utf8",
  );
  await writeFile(
    resolve(EVIDENCE, "admin-ui.md"),
    [
      "# E2c · `/admin` 界面验证（真浏览器 + 真后端）",
      "",
      `- 账号：\`${ADMIN_EMAIL}\`（提权）/ \`${PLAIN_EMAIL}\`（普通），跑完已清`,
      `- 前端 ${FRONTEND} · 后端 ${API}`,
      "",
      ...RESULTS.map((item) => `- ${item.ok ? "✅" : "❌"} ${item.label}${item.detail ? ` —— ${item.detail}` : ""}`),
      "",
    ].join("\n"),
    "utf8",
  );

  console.log(`\n断言 ${RESULTS.length - failed.length}/${RESULTS.length} 通过`);
  if (failed.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error("失败：", error.message);
  process.exit(1);
});
