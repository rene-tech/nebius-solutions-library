import { describe, expect, it } from "vitest";
import { resolveAdminWindow } from "./AdminTimeWindow";

describe("synchronized admin window", () => {
  it("keeps custom historical bounds fixed and reflects them in the control", () => {
    const params = new URLSearchParams({
      from: "2026-09-07T01:00:00Z",
      to: "2026-09-07T02:00:00Z",
    });
    const result = resolveAdminWindow(
      params,
      Date.parse("2026-09-08T12:00:00Z"),
    );
    expect(result.range).toBe("custom");
    expect(result.live).toBe(false);
    expect(result.params.toString()).toBe(params.toString());
  });
  it("uses one exact rolling window and carries relative intent only in navigation", () => {
    const result = resolveAdminWindow(
      new URLSearchParams({
        window: "24",
        project: "example",
        from: "old",
        to: "old",
        token: "excluded",
      }),
      Date.parse("2026-09-08T12:00:00Z"),
    );
    expect(result.params.get("from")).toBe("2026-09-07T12:00:00.000Z");
    expect(result.params.get("to")).toBe("2026-09-08T12:00:00.000Z");
    expect(result.params.has("window")).toBe(false);
    expect(result.navigation.get("window")).toBe("24");
    expect(result.navigation.has("token")).toBe(false);
  });
  it("a malformed relative window cannot produce an invalid request", () => {
    const result = resolveAdminWindow(
      new URLSearchParams({ window: "NaN" }),
      Date.parse("2026-09-08T12:00:00Z"),
    );
    expect(result.range).toBe("1");
    expect(result.params.get("from")).toBe("2026-09-08T11:00:00.000Z");
  });
});
