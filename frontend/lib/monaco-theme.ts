/**
 * Monaco 的 Quantsage 主题（M4c）——**与全站 token 同源**，不用 `vs` / `vs-dark`。
 *
 * 为什么不用内置主题：它们的编辑器底是 `#1E1E1E`，而我们的面板是 `#131722`（深）/ `#FFFFFF`
 * （浅），并排一眼能看出不是一套。本项目已经为「页面换色、组件没换」付过一次代价（图表库
 * 不读 CSS 变量那次），编辑器不能再踩一遍。
 *
 * **颜色分三层，别混**：
 *   1. UI 语义 token（`--ink-*` / `--brand` / `--destructive` / `--warn`）——`app/globals.css`
 *   2. 图表序列色（`--series-*`）
 *   3. **语法高亮**（本文件）——编辑器内部的着色，与 UI 语义无关：代码里的数字是琥珀色，
 *      不代表「警告」；`--warn` 的琥珀语义只活在面板与波浪线上
 *
 * 取值与对比度（WCAG 相对亮度公式实算，底色 = 主题的 `editor.background`）：
 *
 * | 用途 | 浅（底 `#FFFFFF`） | 比值 | 深（底 `#131722`） | 比值 |
 * |---|---|---|---|---|
 * | 关键字 | `#1C5CAB`（= `--brand`） | 6.63 | `#3987E5`（= 深 `--brand`） | 4.92 |
 * | 字符串 | `#0B7A5C` | 5.31 | `#26A69A`（= 深 `--down`） | 5.97 |
 * | 数字 / 常量 | `#8A5A00` | 5.93 | `#D9A441` | 7.96 |
 * | 注释 | `#8A9099`（= `--ink-3`） | 3.22 | `#6B7480`（= 深 `--ink-3`） | 3.78 |
 *
 * 正文类一律 ≥4.5，注释类 ≥3.0（弱化是它的职责）。
 */

import type * as Monaco from "monaco-editor";

export const THEME_LIGHT = "quantsage-light";
export const THEME_DARK = "quantsage-dark";

const LIGHT: Palette = {
  background: "#FFFFFF",
  foreground: "#16181A",
  lineNumber: "#8A9099",
  lineNumberActive: "#5A6069",
  selection: "#EDEDE7",
  lineHighlight: "#F7F7F4",
  guide: "rgba(22, 24, 26, 0.10)",
  cursor: "#1C5CAB",
  error: "#C0392B",
  warning: "#B45309",
  keyword: "#1C5CAB",
  string: "#0B7A5C",
  number: "#8A5A00",
  comment: "#8A9099",
};

const DARK: Palette = {
  background: "#131722",
  foreground: "#E8EAED",
  lineNumber: "#6B7480",
  lineNumberActive: "#9AA4B2",
  selection: "#1B2029",
  lineHighlight: "#1B2029",
  guide: "rgba(255, 255, 255, 0.10)",
  cursor: "#3987E5",
  error: "#EF5350",
  warning: "#E0A030",
  keyword: "#3987E5",
  string: "#26A69A",
  number: "#D9A441",
  comment: "#6B7480",
};

/** 两套调色板的公共形状（`as const` 的字面量类型不能互赋，故显式给一层） */
interface Palette {
  background: string;
  foreground: string;
  lineNumber: string;
  lineNumberActive: string;
  selection: string;
  lineHighlight: string;
  guide: string;
  cursor: string;
  error: string;
  warning: string;
  keyword: string;
  string: string;
  number: string;
  comment: string;
}

function define(
  monaco: typeof Monaco,
  name: string,
  base: "vs" | "vs-dark",
  palette: Palette,
): void {
  monaco.editor.defineTheme(name, {
    base,
    inherit: true,
    rules: [
      { token: "", foreground: palette.foreground.slice(1) },
      { token: "comment", foreground: palette.comment.slice(1), fontStyle: "italic" },
      { token: "string", foreground: palette.string.slice(1) },
      { token: "number", foreground: palette.number.slice(1) },
      { token: "keyword", foreground: palette.keyword.slice(1), fontStyle: "bold" },
      { token: "type", foreground: palette.keyword.slice(1) },
      { token: "delimiter", foreground: palette.foreground.slice(1) },
      { token: "operator", foreground: palette.foreground.slice(1) },
    ],
    colors: {
      "editor.background": palette.background,
      "editor.foreground": palette.foreground,
      "editorLineNumber.foreground": palette.lineNumber,
      "editorLineNumber.activeForeground": palette.lineNumberActive,
      "editor.selectionBackground": palette.selection,
      "editor.lineHighlightBackground": palette.lineHighlight,
      "editorIndentGuide.background1": palette.guide,
      "editorCursor.foreground": palette.cursor,
      // warning 的默认色是**绿色**，与本项目语义冲突，必须覆盖
      "editorError.foreground": palette.error,
      "editorWarning.foreground": palette.warning,
      "editorOverviewRuler.errorForeground": palette.error,
      "editorOverviewRuler.warningForeground": palette.warning,
      // 波浪线与标尺用同一支色，标注在浅深两套里都可辨
      "editorError.border": palette.error,
      "editorWarning.border": palette.warning,
    },
  });
}

/** 注册两个主题（幂等：Monaco 允许重复 define，后写的覆盖先写的） */
export function defineQuantsageThemes(monaco: typeof Monaco): void {
  define(monaco, THEME_LIGHT, "vs", LIGHT);
  define(monaco, THEME_DARK, "vs-dark", DARK);
}

export function themeName(isDark: boolean): string {
  return isDark ? THEME_DARK : THEME_LIGHT;
}
