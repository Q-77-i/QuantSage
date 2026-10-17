import { describe, expect, it } from "vitest";

import { buildCsp } from "./csp";

describe("buildCsp", () => {
  it("production uses the nonce and strict-dynamic", () => {
    const csp = buildCsp({ nonce: "abc123", production: true });

    expect(csp).toContain(
      "script-src 'self' 'nonce-abc123' 'strict-dynamic' 'unsafe-eval' blob:",
    );
    expect(csp).not.toContain("script-src 'self' 'unsafe-inline'");
  });

  it("development keeps unsafe-inline for HMR", () => {
    const csp = buildCsp({ production: false });

    expect(csp).toContain("script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:");
    expect(csp).not.toContain("nonce-");
  });

  it("keeps the shared boundaries", () => {
    const csp = buildCsp({ nonce: "n", production: true });

    for (const directive of [
      "default-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "worker-src 'self' blob:",
      "frame-ancestors 'none'",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "connect-src 'self'",
    ]) {
      expect(csp).toContain(directive);
    }
  });
});
