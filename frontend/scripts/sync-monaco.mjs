/**
 * 把 Monaco 的本地资源同步到 `public/monaco/`（M4c）。
 *
 * 为什么是拷贝而不是 CDN：与弃用 `next/font/google` 同一条理由——构建与运行都不该依赖外网。
 * 为什么是 `min/vs` 而不是 npm 包本身：`monaco-editor` 解包 100MB+（含 esm / dev / 全语言），
 * 而我们只需要 AMD 版的最小运行时；`vs/` 由页面上的 loader 按需加载语言贡献。
 *
 * 为什么资产不入库：5MB 级的 vendored JS 进仓库，与「数据与工具链不入库」的既有取向相反。
 * 代价是 clone 后必须 `npm install`——`predev` / `prebuild` 两个钩子会自动跑本脚本，
 * 所以正常路径上不必记得它。`public/monaco/` 在 `frontend/.gitignore` 里。
 *
 * 幂等：`public/monaco/.version` 记着同步过的版本号，一致就跳过（不重复拷 5MB）。
 */

import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DEST = resolve(ROOT, "public/monaco");
const STAMP = resolve(DEST, ".version");

async function monacoVersion() {
  // 直接读磁盘上的 package.json（不走 `require`：monaco 的 exports 映射未必放行该子路径），
  // 版本号是「同步是否过期」的唯一判据
  const raw = await readFile(resolve(ROOT, "node_modules/monaco-editor/package.json"), "utf8");
  return JSON.parse(raw).version;
}

async function main() {
  const version = await monacoVersion();
  if (existsSync(STAMP) && (await readFile(STAMP, "utf8")).trim() === version) {
    console.log(`monaco ${version} 已同步，跳过`);
    return;
  }

  const source = resolve(ROOT, "node_modules/monaco-editor/min/vs");
  if (!existsSync(source)) {
    console.error(
      `找不到 ${source}。先跑 npm install（monaco-editor 是它的唯一来源）。`,
    );
    process.exit(1);
  }

  await rm(DEST, { recursive: true, force: true });
  await mkdir(DEST, { recursive: true });
  await cp(source, resolve(DEST, "vs"), { recursive: true });
  await writeFile(STAMP, `${version}\n`);
  console.log(`monaco ${version} 已同步到 public/monaco/vs`);
}

await main();
