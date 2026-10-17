import { describe, expect, it } from "vitest";

import { diffLines, diffStats, versionLabel } from "./strategy-versions";

describe("diffLines", () => {
  it("空 diff 不画行", () => {
    expect(diffLines("")).toEqual([]);
    expect(diffLines("   \n")).toEqual([]);
  });

  it("按 unified diff 的前缀分类", () => {
    const lines = diffLines(
      ["--- v2", "+++ v1", "@@ -1 +1 @@", "-old", "+new", " same"].join("\n"),
    );
    expect(lines.map((line) => line.kind)).toEqual([
      "hunk",
      "hunk",
      "hunk",
      "remove",
      "add",
      "context",
    ]);
    expect(lines[3].text).toBe("-old");
  });

  it("统计增删行", () => {
    const lines = diffLines(["--- a", "+++ b", "+a", "+b", "-c", " d"].join("\n"));
    expect(diffStats(lines)).toEqual({ added: 2, removed: 1 });
  });

  it("版本标签", () => {
    expect(versionLabel(3)).toBe("v3");
  });
});
