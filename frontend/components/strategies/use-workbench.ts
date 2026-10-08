"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, describeError } from "@/lib/api";
import {
  buildRunRequest,
  formValues,
  hasBlockingFindings,
  paramFields,
  paramsFromForm,
  readRunFailure,
  type ParamValue,
  type RunFailure,
} from "@/lib/strategy-form";
import type {
  Finding,
  StrategyCheck,
  StrategyMeta,
  StrategySaved,
  StrategySummary,
  StrategyTemplate,
} from "@/lib/types";
import type { RunFormState } from "./run-bar";

/**
 * 工作台的取数与动作（M4c）。页面只负责渲染，状态与流程都在这里。
 *
 * 三处要点：
 *
 * * **标注与表单同源**：编辑器每次改动（防抖 400ms）打一次 `/strategies/check`，响应里的
 *   `findings` 喂标注与面板、`meta` 喂参数表单——不会有「标注说改了、表单还是旧的」；
 * * **检查中不闪骨架**：AST 检查是毫秒级的纯函数，保留上一次结果即可（闪一下比不刷还吵）；
 * * **未保存不入库**：改代码只动本地状态；「保存并运行」才 `PUT`→`POST /backtest`，
 *   这样「跑的是库里那条源码」这条契约在 UI 上是可见的（按钮文案就叫保存并运行）。
 */

const CHECK_DEBOUNCE_MS = 400;

/** 编辑器里当前打开的是什么 */
export type CurrentDoc =
  | { kind: "strategy"; id: string; name: string; updatedAt: string }
  | { kind: "template"; key: string; name: string }
  | { kind: "new"; name: string };

const NEW_SKELETON = `PARAMS = {"window": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "均线窗口"}}
USES_EVENTS = False


def on_bar(ctx, p):
    closes = [bar.close for bar in ctx.history]
    if len(closes) < p["window"]:
        return []
    return []
`;

export function useWorkbench() {
  const router = useRouter();

  const [strategies, setStrategies] = useState<StrategySummary[] | null>(null);
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  const [current, setCurrent] = useState<CurrentDoc | null>(null);
  const [code, setCode] = useState("");
  const [check, setCheck] = useState<StrategyCheck | null>(null);
  const [values, setValues] = useState<Record<string, ParamValue>>({});
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 保存/运行时后端给的结构化失败（422 的 findings、沙箱的 kind） */
  const [failure, setFailure] = useState<RunFailure | null>(null);
  const [runForm, setRunForm] = useState<RunFormState>({
    symbol: "600519",
    start: "",
    end: "",
    pitMode: "pit",
  });

  /** 当前文档「库里存着的参数」：切换文档时用它播种表单（用户改动优先于它） */
  const savedParams = useRef<Record<string, number | boolean>>({});
  /**
   * 最近一次**主动打开**的策略 id。
   *
   * 深链 effect 靠它去重：从列表点选另一条时 URL 会跟着换（`router.replace`），
   * 若只比对「当前载入的 id」，在那个换 URL 的空档里旧的 `?id=` 会把刚选的策略拽回去
   * （实测：点 B 之后又 GET 了一遍 A）。记「主动开过谁」就没有这个窗口。
   */
  const lastOpenRef = useRef<string | null>(null);
  /** 当前文档的参数名集合：形状变了才重新播种，避免每次检查覆盖用户刚敲的值 */
  const paramShape = useRef("");

  const loadList = useCallback(async () => {
    try {
      const [list, templateList] = await Promise.all([api.strategies(), api.strategyTemplates()]);
      setStrategies(list);
      setTemplates(templateList);
    } catch (cause) {
      setError(describeError(cause, "策略列表加载失败：后端未启动或网络不通。"));
      setStrategies([]);
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  // 编辑器内容 → 检查（防抖）。失败静默：检查是辅助信息，不该打断写代码
  useEffect(() => {
    if (!code) {
      setCheck(null);
      return;
    }
    const timer = setTimeout(() => {
      api
        .checkStrategy(code)
        .then((next) => setCheck(next))
        .catch(() => undefined);
    }, CHECK_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [code]);

  // 参数形状变了才重播种（新增/删除参数、换策略）；同一形状下用户输入优先
  const shape = useMemo(
    () => (check?.meta ? paramFields(check.meta).map((field) => field.key).join(",") : ""),
    [check],
  );
  useEffect(() => {
    if (!check) return;
    setValues((prev) => seedValues(check.meta, prev, savedParams.current, shape === paramShape.current));
    paramShape.current = shape;
  }, [check, shape]);

  const openStrategy = useCallback(
    async (id: string) => {
      lastOpenRef.current = id;
      setError(null);
      setFailure(null);
      try {
        const detail = await api.strategy(id);
        savedParams.current = detail.params;
        paramShape.current = ""; // 强制按新文档播种
        setCurrent({
          kind: "strategy",
          id: detail.id,
          name: detail.name,
          updatedAt: detail.updated_at,
        });
        setCode(detail.code);
        setDirty(false);
        // 选中的策略进地址栏：刷新与分享都回到同一条（`replace` 不留历史，来回点不堆栈）
        router.replace(`/strategies?id=${detail.id}`, { scroll: false });
      } catch (cause) {
        setError(describeError(cause, "策略加载失败。"));
      }
    },
    [router],
  );

  const openTemplate = useCallback(
    (key: string) => {
      const template = templates.find((item) => item.key === key);
      if (!template) return;
      savedParams.current = {};
      paramShape.current = "";
      setError(null);
      setFailure(null);
      setCurrent({ kind: "template", key, name: template.title });
      setCode(template.source);
      setDirty(false);
    },
    [templates],
  );

  const startNew = useCallback(() => {
    savedParams.current = {};
    paramShape.current = "";
    setError(null);
    setFailure(null);
    setCurrent({ kind: "new", name: "" });
    setCode(NEW_SKELETON);
    setDirty(true);
  }, []);

  /** 模板 → 可编辑副本：**保留源码与参数**，只把文档身份换成「新建」（保存时 POST） */
  const saveAsCopy = useCallback(() => {
    setCurrent((prev) => (prev ? { kind: "new", name: prev.name } : prev));
    setDirty(true);
  }, []);

  const rename = useCallback((name: string) => {
    setCurrent((prev) => (prev ? { ...prev, name } : prev));
    setDirty(true);
  }, []);

  const editCode = useCallback((next: string) => {
    setCode(next);
    setDirty(true);
  }, []);

  const editParams = useCallback((next: Record<string, ParamValue>) => {
    setValues(next);
    setDirty(true);
  }, []);

  /** 保存当前文档；**新建**走 POST、其余走 PUT。返回库里的那一行（含 findings） */
  const save = useCallback(async (): Promise<StrategySaved | null> => {
    if (!current) return null;
    const name = current.name.trim();
    if (!name) {
      setError("给策略起个名字再保存。");
      return null;
    }
    const params = paramsFromForm(check?.meta ?? null, values);
    const body = { name, code, params };
    const saved =
      current.kind === "strategy"
        ? await api.updateStrategy(current.id, body)
        : await api.createStrategy(body);

    savedParams.current = saved.params;
    setCurrent({ kind: "strategy", id: saved.id, name: saved.name, updatedAt: saved.updated_at });
    setDirty(false);
    lastOpenRef.current = saved.id; // 新建落库后 id 归位：这次 URL 变更不该再触发一次打开
    router.replace(`/strategies?id=${saved.id}`, { scroll: false });
    await loadList();
    return saved;
  }, [check, code, current, loadList, router, values]);

  const remove = useCallback(
    async (id: string) => {
      try {
        await api.deleteStrategy(id);
        if (current?.kind === "strategy" && current.id === id) {
          setCurrent(null);
          setCode("");
          setCheck(null);
        }
        await loadList();
      } catch (cause) {
        setError(describeError(cause, "删除失败。"));
      }
    },
    [current, loadList],
  );

  /** 「保存并运行」：先落库，闸门（error=0）过了才提交回测，跑完跳报告页 */
  const saveAndRun = useCallback(async () => {
    if (!current) return;
    setError(null);
    setFailure(null);
    setSaving(true);
    try {
      const saved = await save();
      if (!saved) return;

      if (hasBlockingFindings(saved.findings)) {
        setCheck({ findings: saved.findings, meta: check?.meta ?? null });
        setFailure({
          message: "策略没通过前视静态检查，已拦在运行前。",
          findings: saved.findings,
          kind: null,
        });
        return;
      }

      setSaving(false);
      setRunning(true);
      const meta = check?.meta ?? null;
      const { run_id } = await api.backtest(
        buildRunRequest(
          { strategyId: saved.id, ...runForm },
          meta,
          paramsFromForm(meta, values),
        ),
      );
      router.push(`/backtest?run=${run_id}`);
    } catch (cause) {
      if (cause instanceof ApiError) {
        setFailure(readRunFailure(cause.payload, cause.message));
      } else {
        setError(describeError(cause, "运行失败：后端未启动或网络不通。"));
      }
    } finally {
      setSaving(false);
      setRunning(false);
    }
  }, [check, current, runForm, router, save, values]);

  /** 标注与面板吃同一份 findings；运行时被拦的 findings 也并进来（同一份源码，两份来源） */
  const findings: Finding[] = useMemo(
    () => failure?.findings.length ? failure.findings : (check?.findings ?? []),
    [check, failure],
  );

  const meta: StrategyMeta | null = check?.meta ?? null;

  return {
    strategies,
    templates,
    current,
    code,
    findings,
    meta,
    values,
    dirty,
    saving,
    running,
    error,
    failure,
    runForm,
    setRunForm,
    /** 深链去重用的 ref 由页面读（`?id=` 与它的比对） */
    lastOpenRef,
    openStrategy,
    openTemplate,
    startNew,
    saveAsCopy,
    rename,
    editCode,
    editParams,
    saveAndRun,
    remove,
  };
}

/**
 * 参数表单播种：**形状没变就保留用户输入**，形状变了（换策略、增删参数）才按
 * 「库里存的值优先、缺的用 schema 默认值」重来。少了这层判断，每次防抖检查都会把
 * 用户刚敲的数字打回默认值。
 */
function seedValues(
  meta: StrategyMeta | null,
  previous: Record<string, ParamValue>,
  saved: Record<string, number | boolean>,
  sameShape: boolean,
): Record<string, ParamValue> {
  const base = formValues(meta, saved);
  if (!sameShape) return base;
  const merged: Record<string, ParamValue> = { ...base };
  for (const key of Object.keys(base)) {
    if (previous[key] !== undefined) merged[key] = previous[key];
  }
  return merged;
}
