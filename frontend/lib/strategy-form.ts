/**
 * 策略工作台的纯函数层（M4c）。编辑器与页面组件只负责渲染，判断都在这层，便于单测。
 *
 * 当前只有一件事：**findings → 编辑器标注**。`PARAMS` schema → 表单状态与运行请求体
 * 随 M4c-2 的工作台一起来（届时本模块补齐）。
 */

import type { Finding } from "./types";

/** 编辑器标注（编辑器的 `IMarkerData` 由组件映射，这里只表达「哪一行、什么级别、说什么」） */
export interface EditorMarker {
  /** 1-based，与 `Finding.line` 同口径 */
  line: number;
  severity: "error" | "warning";
  message: string;
}

/**
 * `Finding[]` → 编辑器标注。
 *
 * 两条纪律：
 *   * **行号越界要钳到合法区间**——被编辑到一半的源码（后端检查的是上一次保存的文本、
 *     编辑器里已经是新的）会让行号落在文件之外，越界标注在编辑器里是不可见的静默失败；
 *   * **不合并同行的多条 finding**——同一条规则可以拆成多条命中（R5 的两种返回值形态就是），
 *     合并会把信息吃掉，编辑器本来就支持一行多个标记。
 */
export function findingsToMarkers(findings: Finding[], lineCount: number): EditorMarker[] {
  const last = Math.max(1, lineCount);
  return findings.map((finding) => ({
    line: Math.min(Math.max(1, finding.line), last),
    severity: finding.severity,
    message: `[${finding.rule}] ${finding.message}`,
  }));
}

/** 有没有拦运行的那一档（回测提交前的闸门判据；与后端 `has_errors` 同义） */
export function hasBlockingFindings(findings: Finding[]): boolean {
  return findings.some((finding) => finding.severity === "error");
}
