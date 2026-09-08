import { afterEach, describe, expect, it, vi } from "vitest";
import { appsApi } from "./appsClient";
import { testEnvelope } from "../test/accessFixtures";

afterEach(() => vi.unstubAllGlobals());
describe("Apps same-origin transport", () => {
  function fetcher() {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(testEnvelope({})), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetch);
    return fetch;
  }
  it("binds every app request to its own ID and drops credential-like context", async () => {
    const fetch = fetcher();
    await appsApi.runs(
      "app/a",
      new URLSearchParams({
        from: "2026-09-08T00:00:00Z",
        to: "2026-09-08T01:00:00Z",
        token: "must-not-propagate",
      }),
      { status: "failed", principal_id: "owner-one" },
    );
    const url = new URL(fetch.mock.calls[0][0], "https://example.invalid");
    expect(url.pathname).toBe("/admin/api/v1/apps/app%2Fa/runs");
    expect(url.searchParams.get("status")).toBe("failed");
    expect(url.searchParams.get("principal_id")).toBe("owner-one");
    expect(url.searchParams.has("token")).toBe(false);
    expect(fetch.mock.calls[0][1]).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
    });
  });
  it("PATCHes versioned runtime settings without an infrastructure action", async () => {
    const fetch = fetcher();
    const update = { expected_app_revision: 3, display_name: "Changed name" };
    await appsApi.updateSettings("app-one", update, new URLSearchParams());
    expect(fetch).toHaveBeenCalledWith(
      "/admin/api/v1/apps/app-one/settings",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify(update),
      }),
    );
    expect(fetch).toHaveBeenCalledOnce();
  });
  it("creates an independently identified app from an explicit source", async () => {
    const fetch = fetcher();
    const body = {
      model_ref: "qwen3-8b",
      display_name: "Second app",
      source_app_id: "source-one",
    };
    await appsApi.create(body);
    expect(fetch).toHaveBeenCalledWith(
      "/admin/api/v1/apps",
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) }),
    );
  });
});
