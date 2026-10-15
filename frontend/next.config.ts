import type { NextConfig } from "next";

/**
 * 安全响应头（P3-E8a，SPEC §8.2）。
 *
 * 与后端 `app/core/security.py` 的 `SECURITY_HEADERS` 是同一组里减去 CSP 的那四条：
 * **前端不加 CSP**——Next 15 + Monaco + ECharts 要 nonce/hash 铺开才不裂，本地 http 部署下
 * 收益极低（SPEC §8.2 已写明这条取舍，不再两处各讲一遍）。
 *
 * 打印页（`/print/[id]`）由服务端 Playwright 打开，不是 iframe，`X-Frame-Options: DENY`
 * 不影响它。
 */
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "geolocation=(), camera=(), microphone=()" },
];

const nextConfig: NextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
