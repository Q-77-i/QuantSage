/**
 * 策略工作台的纯函数层（M4c）。组件只负责渲染，判断都在这层，便于单测。
 *
 * 三件事：
 *   * **findings → 编辑器标注**（行号钳制、severity 映射）；
 *   * **`PARAMS` schema → 表单状态与运行请求体**（值域**不在这里重复造**——后端已校验，
 *     前端只把 min/max 当提示显示；提交后由后端 422 给中文话术）；
 *   * 运行失败时的**结构化信息还原**（422 体里的 `findings` 要能回到面板与编辑器）。
 */

import type {
  BacktestRequest,
  Finding,
  ParamSpec,
  PitMode,
  StrategyMeta,
} from "./types";

// ── findings ───────────────────────────────────────────────────────────────

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

/** error 在前、warning 在后，组内按行号——面板与「哪几条会拦运行」的阅读顺序一致 */
export function sortFindings(findings: Finding[]): Finding[] {
  const rank = { error: 0, warning: 1 } as const;
  return [...findings].sort(
    (left, right) => rank[left.severity] - rank[right.severity] || left.line - right.line,
  );
}

// ── 参数表单 ───────────────────────────────────────────────────────────────

/** 输入框里一律是字符串（忠实反映用户敲的内容）；开关是布尔 */
export type ParamValue = string | boolean;

export interface ParamField {
  key: string;
  label: string;
  type: ParamSpec["type"];
  min: number | null;
  max: number | null;
}

/** `PARAMS` schema → 渲染顺序的字段表（键序即声明顺序） */
export function paramFields(meta: StrategyMeta | null): ParamField[] {
  if (!meta) return [];
  return Object.entries(meta.params).map(([key, spec]) => ({
    key,
    label: spec.label || key,
    type: spec.type,
    min: spec.min,
    max: spec.max,
  }));
}

/**
 * 表单初值：`saved`（库里存的上次取值）优先，缺的用 schema 的 `default` 填满。
 *
 * 库里存的值可能是**改过 schema 之前**留下的（键已不存在）——只取 schema 里有的键，
 * 多余的静默丢弃（与后端「未给的用 default 填满」同向）。
 */
export function formValues(
  meta: StrategyMeta | null,
  saved: Record<string, number | boolean> | null = null,
): Record<string, ParamValue> {
  if (!meta) return {};
  const values: Record<string, ParamValue> = {};
  for (const [key, spec] of Object.entries(meta.params)) {
    const stored = saved?.[key];
    if (spec.type === "bool") {
      values[key] = typeof stored === "boolean" ? stored : Boolean(spec.default);
    } else {
      values[key] = String(typeof stored === "number" ? stored : spec.default);
    }
  }
  return values;
}

/**
 * 表单值 → 请求体的 `params`。**只做类型搬运，不做值域判断**（后端管）：
 * 数字输入原样转 `Number`（空串留给后端报「参数 X 需要数字」），开关直传。
 */
export function paramsFromForm(
  meta: StrategyMeta | null,
  values: Record<string, ParamValue>,
): Record<string, number | boolean> {
  if (!meta) return {};
  const params: Record<string, number | boolean> = {};
  for (const [key, spec] of Object.entries(meta.params)) {
    const raw = values[key];
    if (spec.type === "bool") {
      params[key] = Boolean(raw);
    } else {
      params[key] = raw === "" || raw === undefined ? Number.NaN : Number(raw);
    }
  }
  return params;
}

// ── 运行请求 ───────────────────────────────────────────────────────────────

export interface RunForm {
  /** 用户策略 id（先保存才能跑） */
  strategyId: string;
  symbol: string;
  /** 空串 = 交给后端缺省（按 `USES_EVENTS` 选事件语料起点或第一根 bar） */
  start: string;
  end: string;
  pitMode: PitMode;
}

/**
 * 运行请求体。**增量字段只在非空时带上**（后端的缺省语义依赖「字段缺失」，送空串会 422）；
 * `pit_mode` 只在策略消费事件时才给 `both`——不消费时两模式必然同结果，后端也不会白跑第二遍
 * （这里跟着收窄，是为了让请求体如实反映「这次要比什么」）。
 */
export function buildRunRequest(
  form: RunForm,
  meta: StrategyMeta | null,
  params: Record<string, number | boolean>,
): BacktestRequest {
  const pitMode: PitMode = meta?.uses_events ? form.pitMode : "pit";
  return {
    strategy: "user",
    strategy_id: form.strategyId,
    symbol: form.symbol.trim(),
    ...(form.start ? { start: form.start } : {}),
    ...(form.end ? { end: form.end } : {}),
    pit_mode: pitMode,
    params,
  };
}

// ── 运行失败的结构化还原 ───────────────────────────────────────────────────

export interface RunFailure {
  /** 一句话人话（后端 `detail`） */
  message: string;
  /** 422 闸门带的 findings（其它错误为空数组） */
  findings: Finding[];
  /** 沙箱终止的档位（`cpu` / `wall` / `memory` / `output` / `crash`）；其它错误为 null */
  kind: string | null;
}

/**
 * 从 `ApiError.payload` 里把结构化信息捞回来。
 *
 * 后端的三类失败给的东西不一样（见 SPEC §5 M4c 的错误码表）：422 闸门带 `findings`、
 * 沙箱终止带 `kind`、参数错只有 `detail`。面板要按类型分派，所以在这里统一成一种形状——
 * **认不出来的字段一律当没有**，别让一个畸形响应把整个面板带崩。
 */
export function readRunFailure(payload: unknown, message: string): RunFailure {
  const body = (payload ?? {}) as { findings?: unknown; kind?: unknown };
  const findings = Array.isArray(body.findings)
    ? (body.findings.filter(
        (item): item is Finding =>
          typeof item === "object" &&
          item !== null &&
          typeof (item as Finding).line === "number" &&
          typeof (item as Finding).message === "string" &&
          ((item as Finding).severity === "error" || (item as Finding).severity === "warning"),
      ) as Finding[])
    : [];
  return {
    message,
    findings,
    kind: typeof body.kind === "string" ? body.kind : null,
  };
}

/** 沙箱终止档位 → 一句人话（`kind` 是给 UI 分档用的机器码，界面不直接显示它） */
export function sandboxKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    cpu: "CPU 时间超限",
    wall: "运行时间超限",
    memory: "内存超限",
    output: "输出超限",
    crash: "沙箱异常退出",
  };
  return labels[kind] ?? "沙箱终止";
}
