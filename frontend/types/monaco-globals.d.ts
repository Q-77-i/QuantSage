/**
 * Monaco 的 AMD loader 挂在 `window` 上的三样东西（M4c）。
 *
 * 资源走 `public/monaco/vs`、由 loader 在浏览器里按需加载（见
 * `components/strategies/code-editor.tsx`），**我们不 import 包本体**，所以这三处全局
 * 得自己声明。类型来自 `monaco-editor` 包——**仅类型**，编译期擦除，不进产物。
 */

declare global {
  interface Window {
    /** `loader.js` 注入的 AMD `require`（带一个 `config` 方法） */
    require?: {
      (deps: string[], onLoad: () => void): void;
      config(options: { paths: Record<string, string> }): void;
    };
    /** `vs/editor/editor.main` 加载完成后挂上来的命名空间 */
    monaco?: typeof import("monaco-editor");
    /** worker 地址由它决定；必须在 `editor.main` 之前设置 */
    MonacoEnvironment?: {
      getWorkerUrl?: (moduleId: string, label: string) => string;
    };
  }
}

export {};
