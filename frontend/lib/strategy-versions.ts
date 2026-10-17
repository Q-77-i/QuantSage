/**
 * 策略版本抽屉的纯函数（5d）：把服务端 `difflib.unified_diff` 的文本切成可着色的行。
 *
 * 服务端产生的是标准 unified diff（`--- a` / `+++ b` / `@@` / `+` / `-` / 空格）。
 * 这里不重新计算 diff，只做**展示分类**；空 diff 返回空数组，调用方显示「与当前一致」。
 */

export type DiffLineKind = "add" | "remove" | "hunk" | "context";

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
}

export interface DiffStats {
  added: number;
  removed: number;
}

export function diffLines(diff: string): DiffLine[] {
  if (!diff.trim()) return [];
  return diff.split("\n").map((text) => {
    if (text.startsWith("+++") || text.startsWith("---") || text.startsWith("@@")) {
      return { kind: "hunk", text };
    }
    if (text.startsWith("+")) return { kind: "add", text };
    if (text.startsWith("-")) return { kind: "remove", text };
    return { kind: "context", text };
  });
}

export function diffStats(lines: DiffLine[]): DiffStats {
  return {
    added: lines.filter((line) => line.kind === "add").length,
    removed: lines.filter((line) => line.kind === "remove").length,
  };
}

export function versionLabel(version: number): string {
  return `v${version}`;
}
