import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "./client";

const response = {
  meta: {
    schema_version: "fs2.admin-api/v1",
    generated_at: "2026-08-30T08:00:00Z",
    context: {
      project: "fixture-project",
      cluster: "fixture-cluster",
      region: "fixture-region",
      from_at: "2026-08-30T07:00:00Z",
      to_at: "2026-08-30T08:00:00Z",
      timezone: "UTC",
    },
    sources: [],
    warnings: [],
  },
  data: { items: [], total: 0 },
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("same-origin admin API boundary", () => {
  it("forwards only allow-listed shared context and bounded server-owned parameters", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(response), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const params = new URLSearchParams({
      project: "fixture-project",
      cluster: "fixture-cluster",
      region: "fixture-region",
      status: "hot",
      token: "must-not-leave-browser",
    });
    await adminApi.models(params);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/admin/api/v1/models?project=fixture-project&cluster=fixture-cluster&region=fixture-region&limit=200");
    expect(url).not.toContain("token");
    expect(url).not.toContain("status");
    expect(init.credentials).toBe("same-origin");
  });

  it("sends only explicit bounded model and operation filters supported by the live backend", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(response), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const context = new URLSearchParams({ project: "fixture-project", token: "must-not-flow" });

    await adminApi.models(context, { state: "loading", search: "GLM 5.2", limit: 999 });
    await adminApi.operations(context, {
      tenantId: "tenant-a",
      modelId: "glm-5-2",
      principalId: "agent-a",
      apiKeyPrefix: "fs2_pat_123",
      status: "failed",
      errorCode: "runtime_failed",
      cursor: "opaque-cursor",
      limit: 500,
    });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/admin/api/v1/models?project=fixture-project&limit=256&state=loading&search=GLM+5.2",
      "/admin/api/v1/operations?project=fixture-project&limit=200&cursor=opaque-cursor&tenant_id=tenant-a&model_id=glm-5-2&principal_id=agent-a&api_key_prefix=fs2_pat_123&status=failed&error_code=runtime_failed",
    ]);
    expect(fetchMock.mock.calls.map(([url]) => String(url))).not.toEqual(expect.arrayContaining([expect.stringContaining("must-not-flow")]));
  });

  it("drops values beyond each backend query bound instead of causing avoidable 422 responses", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(response), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const context = new URLSearchParams({
      project: "p".repeat(129),
      cluster: "c".repeat(129),
      region: "r".repeat(65),
      timezone: "t".repeat(65),
    });

    await adminApi.models(context, { search: "s".repeat(129) });
    await adminApi.operations(context, {
      cursor: "c".repeat(513),
      tenantId: "t".repeat(121),
      modelId: "m".repeat(129),
      principalId: "p".repeat(201),
      apiKeyPrefix: "k".repeat(65),
      errorCode: "e".repeat(65),
    });
    await adminApi.observability(context, {
      modelId: "m".repeat(129),
      operationId: "o".repeat(37),
    });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/admin/api/v1/models?limit=200",
      "/admin/api/v1/operations?limit=100",
      "/admin/api/v1/observability",
    ]);
  });

  it("parses both RFC admin problems and gateway authentication errors", async () => {
    const requestId = "9ad7e7fc-0b9e-4457-a7db-6803267ba456";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({
      error: { type: "authentication_error", message: "admin authentication is required" },
    }), { status: 401 })).mockResolvedValueOnce(new Response(JSON.stringify({
      detail: "operator policy does not permit this request",
      code: "permission_denied",
      request_id: requestId,
    }), { status: 403 })));

    const authentication = await adminApi.createSession("invalid-token").catch((error: unknown) => error);
    expect(authentication).toMatchObject({
      message: "admin authentication is required",
      status: 401,
      code: "authentication_error",
    });
    expect(authentication).toBeInstanceOf(AdminApiError);

    const permission = await adminApi.overview(new URLSearchParams()).catch((error: unknown) => error);
    expect(permission).toMatchObject({
      message: "operator policy does not permit this request",
      status: 403,
      code: "permission_denied",
      requestId,
    });
  });

  it("rejects a payload outside the typed envelope", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {} }), { status: 200 })));
    await expect(adminApi.overview(new URLSearchParams())).rejects.toThrow("incompatible envelope");
  });

  it("exchanges a bootstrap token only in the authorization header and never persists or logs it", async () => {
    const transient = "bootstrap-value-" + "x".repeat(40);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", fetchMock);

    await adminApi.createSession(transient, "00000000-0000-0000-0000-000000000001");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/admin/api/v1/session");
    expect(url).not.toContain(transient);
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${transient}`);
    expect(init.body).toBe(JSON.stringify({ principal_id: "00000000-0000-0000-0000-000000000001" }));
    expect(String(init.body)).not.toContain(transient);
    expect(storageSpy).not.toHaveBeenCalled();
    expect(consoleSpy).not.toHaveBeenCalled();
  });

  it("uses the sealed key lifecycle routes without putting policy data in the URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await adminApi.issueKey({
      name: "agent runtime",
      principal_id: "agent-a",
      tenant_id: "tenant-a",
      scopes: ["inference.invoke", "mcp.invoke"],
      models: ["qwen3-8b"],
      request_budget: 100,
      gpu_seconds_budget: 500,
      max_concurrency: 2,
      rate_limit_requests: 10,
      rate_window_seconds: 60,
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/admin/api/v1/keys");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(String(init.body))).toMatchObject({ principal_id: "agent-a", models: ["qwen3-8b"] });
  });

  it("logs out with the same-origin 204 session contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await adminApi.deleteSession();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/admin/api/v1/session");
    expect(init.method).toBe("DELETE");
    expect(init.credentials).toBe("same-origin");
  });

  it("forwards only bounded observability selectors and never unrelated URL data", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const params = new URLSearchParams({
      project: "fixture-project",
      token: "must-not-flow",
      principal_id: "private-principal",
    });
    await adminApi.observability(params, {
      modelId: "qwen3-8b",
      operationId: "10f61fc4-4211-4bb8-a058-b11a8c078520",
    });
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/admin/api/v1/observability?project=fixture-project&model_id=qwen3-8b&operation_id=10f61fc4-4211-4bb8-a058-b11a8c078520");
    expect(url).not.toContain("token");
    expect(url).not.toContain("principal");
  });

  it("uses the sealed Configuration workflow routes with proposal data only in POST bodies", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(response), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const proposal = { base_etag: "a".repeat(64), desired: { schema_version: "fs2.admin-configuration/v1", pools: {}, models: {} } } as never;

    await adminApi.configurationDiff(proposal);
    await adminApi.validateConfiguration(proposal);
    await adminApi.planConfiguration(proposal);
    await adminApi.reconcileConfiguration({ plan_id: "11111111-1111-4111-8111-111111111111", base_etag: "a".repeat(64) });
    await adminApi.reconciliationStatus("11111111-1111-4111-8111-111111111111");
    await adminApi.rollbackConfiguration({ target_revision: 1, base_etag: "a".repeat(64) });

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/admin/api/v1/configuration:diff",
      "/admin/api/v1/configuration:validate",
      "/admin/api/v1/configuration:plan",
      "/admin/api/v1/configuration:reconcile",
      "/admin/api/v1/configuration/reconciliations/11111111-1111-4111-8111-111111111111",
      "/admin/api/v1/configuration:rollback",
    ]);
    for (const [, init] of fetchMock.mock.calls.filter(([, init]) => (init as RequestInit).method === "POST")) {
      expect((init as RequestInit).credentials).toBe("same-origin");
      expect(String((init as RequestInit).body)).not.toContain("credential");
    }
  });
});
