import { afterEach, describe, expect, it, vi } from "vitest";
import { requestDebugApi } from "./requestDebugClient";
import { testEnvelope } from "../test/accessFixtures";

afterEach(() => vi.unstubAllGlobals());

describe("request debug authorized read transport", () => {
  function fetcher() {
    const fetch = vi.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify(testEnvelope({})), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    return fetch;
  }

  it("uses summary-only App list with the exact operation/window/cursor and same-origin session", async () => {
    const fetch = fetcher();
    const signal = new AbortController().signal;
    await requestDebugApi.list(
      "app/a",
      new URLSearchParams({
        from: "2026-09-09T00:00:00Z",
        to: "2026-09-09T01:00:00Z",
        token: "not-propagated",
      }),
      { operation_id: "op-one", cursor: "cursor-one" },
      signal,
    );
    const url = new URL(fetch.mock.calls[0][0], "https://example.invalid");
    expect(url.pathname).toBe("/admin/api/v1/apps/app%2Fa/requests");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      from: "2026-09-09T00:00:00Z",
      to: "2026-09-09T01:00:00Z",
      operation_id: "op-one",
      cursor: "cursor-one",
      limit: "50",
    });
    expect(fetch.mock.calls[0][1]).toMatchObject({
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      signal,
    });
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("fetches one encoded App exchange only through the explicit detail method", async () => {
    const fetch = fetcher();
    await requestDebugApi.detail(
      "app-one",
      "exchange/id",
      new URLSearchParams(),
    );
    expect(fetch.mock.calls[0][0]).toBe(
      "/admin/api/v1/apps/app-one/requests/exchange%2Fid",
    );
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("keeps unattributed global requests available without inventing an App", async () => {
    const fetch = fetcher();
    await requestDebugApi.list(undefined, new URLSearchParams());
    await requestDebugApi.detail(
      undefined,
      "exchange-one",
      new URLSearchParams(),
    );
    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      "/admin/api/v1/requests?limit=50",
      "/admin/api/v1/requests/exchange-one",
    ]);
  });
});
