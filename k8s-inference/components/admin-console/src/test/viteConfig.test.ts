import { readFileSync } from "node:fs";
import { describe, expect, test } from "vitest";
import { ADMIN_BUILD_SOURCEMAPS } from "../../vite.config";

describe("production disclosure boundary", () => {
  test("does not publish source maps", () => {
    expect(ADMIN_BUILD_SOURCEMAPS).toBe(false);
  });

  test("keeps the direct static service browser policy complete", () => {
    const nginx = readFileSync(new URL("../../nginx.conf", import.meta.url), "utf8");
    for (const header of [
      "Content-Security-Policy",
      "Permissions-Policy",
      "Referrer-Policy",
      "Strict-Transport-Security",
      "X-Content-Type-Options",
      "X-Frame-Options",
    ]) {
      expect(nginx).toContain(`add_header ${header}`);
    }
  });
});
