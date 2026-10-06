import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // 只跑纯函数单测：组件与端到端留给浏览器人工走查（P1 不值当引组件测试框架）
    include: ["lib/**/*.test.ts"],
    environment: "node",
  },
});
