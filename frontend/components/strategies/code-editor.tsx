"use client";

import { useTheme } from "next-themes";
import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";

import { defineQuantsageThemes, themeName } from "@/lib/monaco-theme";
import { findingsToMarkers } from "@/lib/strategy-form";
import type { Finding } from "@/lib/types";

/**
 * Monaco 封装（M4c）。
 *
 * **资源走本地**：`/monaco/vs` 由 `scripts/sync-monaco.mjs` 从 `node_modules/monaco-editor/min/vs`
 * 拷来（`predev` / `prebuild` 自动跑，见脚本头注释）。因此这里**不 import monaco 包本体**，
 * 而是用官方的 AMD loader 按需加载——资产不经过打包器，next 配置一行不用改，
 * 也不会有任何 CDN 请求（与弃用 `next/font/google` 同一条理由）。
 *
 * 组件必须**动态引入且关掉 SSR**（`next/dynamic` + `ssr: false`）：loader 是浏览器脚本，
 * 服务端渲染时既没有 `window` 也没有 `document`。
 *
 * 标注走 `setModelMarkers`：`findings` 是 prop，变化即刷新（同时承担「清空」）；
 * 点面板某条跳行是命令式的，走 `ref` 上的 `revealLine`。
 */

/** Monaco 本地资源路径。改这里要同时改 `scripts/sync-monaco.mjs` 的目标目录 */
const MONACO_BASE = "/monaco/vs";

/** 标注的 owner：同一 owner 的标注会被整批替换，正是我们要的语义 */
const MARKER_OWNER = "strategy-check";

type Monaco = typeof import("monaco-editor");

export interface CodeEditorHandle {
  /** 把光标挪到某一行并聚焦（findings 面板点一条 → 跳到那行） */
  revealLine(line: number): void;
}

interface CodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  findings: Finding[];
  /** 模板预览用：模板是服务端真源，不能就地改，要先「另存为我的」 */
  readOnly?: boolean;
  ref?: Ref<CodeEditorHandle>;
}

/**
 * 全局只加载一次：`window.require` 是单例，重复 `require.config` 会打架。
 * 缓存的是 Promise 本身（不是结果），并发挂载的两处会等到同一个加载。
 */
let monacoPromise: Promise<Monaco> | null = null;

function loadMonaco(): Promise<Monaco> {
  monacoPromise ??= new Promise<Monaco>((resolve, reject) => {
    // worker **不用配置**：0.57 的 `editor.main` 自己赋值 `self.MonacoEnvironment`（blob worker
    // 里 importScripts 同目录的 `assets/*.worker-*.js`），预设会被它覆盖掉、纯属无效代码。
    // 那些 worker 文件就在本目录内，同样不产生任何外部请求。
    const script = document.createElement("script");
    script.src = `${MONACO_BASE}/loader.js`;
    script.async = true;
    script.onload = () => {
      const loader = window.require;
      if (!loader) {
        reject(new Error("Monaco loader 已加载但没有挂上 window.require"));
        return;
      }
      loader.config({ paths: { vs: MONACO_BASE } });
      loader(["vs/editor/editor.main"], () => {
        const monaco = window.monaco;
        if (!monaco) {
          reject(new Error("vs/editor/editor.main 加载完成但没有挂上 window.monaco"));
          return;
        }
        // 主题与全站 token 同源（见 lib/monaco-theme.ts）：加载完成即注册，只做一次
        defineQuantsageThemes(monaco);
        resolve(monaco);
      });
    };
    script.onerror = () =>
      reject(new Error(`加载 ${MONACO_BASE}/loader.js 失败——本地资源没同步，跑一次 npm install`));
    document.head.appendChild(script);
  });
  return monacoPromise;
}

export function CodeEditor({ value, onChange, findings, readOnly = false, ref }: CodeEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<import("monaco-editor").editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<Monaco | null>(null);
  /** 最近一次由**本组件**发出的值：外部改 value 才 setValue，自己打字不能被打断 */
  const emittedRef = useRef(value);
  /** onDidChangeModelContent 的订阅要拿最新回调，用 ref 传，避免重建编辑器 */
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  const [error, setError] = useState<string | null>(null);
  const { resolvedTheme } = useTheme();

  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const monaco = await loadMonaco();
        if (disposed || !containerRef.current) return;
        monacoRef.current = monaco;
        const editor = monaco.editor.create(containerRef.current, {
          value: emittedRef.current,
          language: "python",
          theme: themeName(document.documentElement.classList.contains("dark")),
          automaticLayout: true, // 左右分栏拖动时容器会变宽，交给它自己量
          minimap: { enabled: false },
          fontSize: 13,
          lineHeight: 20,
          scrollBeyondLastLine: false,
          tabSize: 4,
          renderWhitespace: "none",
          readOnly,
        });
        editorRef.current = editor;
        editor.onDidChangeModelContent(() => {
          const next = editor.getValue();
          // 程序性 `setValue`（切换策略 / 载入模板）也会触发这个事件：它与「我们最后
          // 发出/推入的那份」相同，直接放行会让载入立刻变成「未保存」。相同即回声，跳过
          if (next === emittedRef.current) return;
          emittedRef.current = next;
          onChangeRef.current(next);
        });
      } catch (cause) {
        if (!disposed) setError(cause instanceof Error ? cause.message : String(cause));
      }
    })();
    return () => {
      disposed = true;
      editorRef.current?.dispose();
      editorRef.current = null;
    };
    // 只建一次：readOnly / theme 的后续变化由各自的 effect 处理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 外部换了一份源码（切策略、载入模板）才写回编辑器；自己的输入不回灌，否则光标会跳
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor || value === emittedRef.current) return;
    emittedRef.current = value;
    editor.setValue(value);
  }, [value]);

  useEffect(() => {
    editorRef.current?.updateOptions({ readOnly });
  }, [readOnly]);

  useEffect(() => {
    monacoRef.current?.editor.setTheme(themeName(resolvedTheme === "dark"));
  }, [resolvedTheme]);

  useEffect(() => {
    const monaco = monacoRef.current;
    const model = editorRef.current?.getModel();
    if (!monaco || !model) return;
    monaco.editor.setModelMarkers(
      model,
      MARKER_OWNER,
      findingsToMarkers(findings, model.getLineCount()).map((marker) => ({
        severity:
          marker.severity === "error"
            ? monaco.MarkerSeverity.Error
            : monaco.MarkerSeverity.Warning,
        message: marker.message,
        // 整行波浪线：从行首到行尾（列是 1-based，末列含换行符，取 maxColumn 少一位）
        startLineNumber: marker.line,
        startColumn: 1,
        endLineNumber: marker.line,
        endColumn: Math.max(1, model.getLineMaxColumn(marker.line) - 1),
      })),
    );
  }, [findings]);

  useImperativeHandle(ref, () => ({
    revealLine(line: number) {
      const editor = editorRef.current;
      if (!editor) return;
      const target = Math.min(Math.max(1, line), editor.getModel()?.getLineCount() ?? 1);
      editor.revealLineInCenter(target);
      editor.setPosition({ lineNumber: target, column: 1 });
      editor.focus();
    },
  }));

  if (error) {
    return (
      <div
        role="alert"
        className="flex h-[420px] items-center justify-center rounded-[var(--radius)] border border-destructive/40 bg-destructive/10 px-4 text-sm text-destructive"
      >
        {error}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      data-testid="code-editor"
      className="h-[420px] overflow-hidden rounded-[var(--radius)] border border-border"
    />
  );
}
