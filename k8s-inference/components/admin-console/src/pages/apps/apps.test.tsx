import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appsApi } from "../../api/appsClient";
import type { AppRun, AppSettings, AppSummary } from "../../api/appsTypes";
import { adminApi, AdminApiError } from "../../api/client";
import type { AdminEnvelope, AdminOperationList } from "../../api/types";
import type { ScientificRunDetail } from "../../api/scientificTypes";
import { SessionContext } from "../../auth/SessionContext";
import { testEnvelope, testSession } from "../../test/accessFixtures";
import { browserFixture } from "../../test/browserFixtures";
import {
  modelDeploymentRevisionFixture,
  modelDeploymentMutationCapabilitiesFixture,
  modelDeploymentStatusFixture,
} from "../../test/modelDeploymentFixtures";
import { AppsPage } from "./AppsPage";
import { AppDetailPage } from "./AppDetailPage";
import { AppRunDetail, appRunNeedsRefresh } from "./AppRunDetail";
import { AppSettingsTab } from "./AppSettingsTab";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});
const app: AppSummary = {
  app_id: "app-one",
  display_name: "Research Qwen",
  model_ref: "qwen3-8b",
  public_model_id: "qwen-research",
  execution_mode: "serving",
  namespace: "fs2-models",
  deployment_name: "qwen-live",
  academic_required: false,
  enabled: true,
  status: "Ready",
  status_reason: null,
  created_at: "2026-09-08T00:00:00Z",
  updated_at: "2026-09-08T00:00:00Z",
  capabilities: {
    duplicate: true,
    reusable_workers: true,
    live_settings: true,
  },
  logical_run_count: 27,
  last_used_at: "2026-09-08T02:00:00Z",
};
function operation() {
  return structuredClone(
    (
      browserFixture(
        "/admin/api/v1/operations",
      ) as AdminEnvelope<AdminOperationList>
    ).data.items[0],
  );
}
function science(): ScientificRunDetail {
  return structuredClone(
    (
      browserFixture(
        "/admin/api/v1/scientific-runs/run-rfdiffusion-0001",
      ) as AdminEnvelope<ScientificRunDetail>
    ).data,
  );
}
function renderPage(
  element: React.ReactElement,
  path = "/admin/apps/app-one/runs",
  route = "/admin/apps/:appId/:tab",
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SessionContext.Provider
        value={{
          session: testSession,
          logout: async () => undefined,
          loggingOut: false,
          logoutError: null,
        }}
      >
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path={route} element={element} />
          </Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Apps identity and tabs", () => {
  it("keeps two apps for one model separate, shows counts not rates and unknown not zero", async () => {
    vi.spyOn(appsApi, "list").mockResolvedValue(
      testEnvelope({
        items: [
          app,
          {
            ...app,
            app_id: "app-two",
            display_name: "Team Qwen",
            public_model_id: "qwen-team",
            logical_run_count: null,
            last_used_at: null,
          },
        ],
        next_cursor: null,
      }),
    );
    renderPage(<AppsPage />, "/admin/apps", "/admin/apps");
    expect(
      await screen.findByRole("link", { name: "Research Qwen" }),
    ).toHaveAttribute("href", expect.stringContaining("/app-one/runs"));
    expect(screen.getByRole("link", { name: "Team Qwen" })).toHaveAttribute(
      "href",
      expect.stringContaining("/app-two/runs"),
    );
    const row = screen.getByRole("link", { name: "Team Qwen" }).closest("tr")!;
    expect(within(row).getByText("—")).toBeInTheDocument();
    expect(within(row).getByText("Not observed")).toBeInTheDocument();
    expect(screen.getByText("27")).toBeInTheDocument();
    expect(screen.queryByText("27/s")).not.toBeInTheDocument();
  });
  it("routes all six tabs by app ID and passes run filters to the bound API", async () => {
    vi.spyOn(appsApi, "detail").mockResolvedValue(testEnvelope(app));
    const runs = vi
      .spyOn(appsApi, "runs")
      .mockResolvedValue(testEnvelope({ items: [], next_cursor: null }));
    renderPage(<AppDetailPage />);
    expect(
      await screen.findByRole("navigation", { name: "App sections" }),
    ).toBeInTheDocument();
    for (const label of [
      "Runs",
      "Metrics",
      "App Logs",
      "Containers",
      "Usage",
      "Settings",
    ])
      expect(screen.getByRole("link", { name: label })).toHaveAttribute(
        "href",
        expect.stringContaining("/apps/app-one/"),
      );
    fireEvent.change(screen.getByLabelText("Run state"), {
      target: { value: "failed" },
    });
    await waitFor(() =>
      expect(runs).toHaveBeenLastCalledWith(
        "app-one",
        expect.any(URLSearchParams),
        expect.objectContaining({ status: "failed" }),
        expect.any(AbortSignal),
      ),
    );
    fireEvent.change(screen.getByLabelText("Run user"), {
      target: { value: "research-owner" },
    });
    await waitFor(() =>
      expect(runs).toHaveBeenLastCalledWith(
        "app-one",
        expect.any(URLSearchParams),
        expect.objectContaining({ principal_id: "research-owner" }),
        expect.any(AbortSignal),
      ),
    );
  });
  it("embeds plaintext correlated logs rather than interpreting messages as HTML", async () => {
    vi.spyOn(appsApi, "detail").mockResolvedValue(testEnvelope(app));
    const logs = vi.spyOn(appsApi, "logs").mockResolvedValue(
      testEnvelope({
        app_id: app.app_id,
        items: [
          {
            at: "2026-09-08T02:00:00Z",
            message: "<b>model ready</b>",
            namespace: "fs2-models",
            pod: "pod-one",
            container: "runtime",
            level: "info",
            run_id: null,
          },
        ],
        next_cursor: null,
        source: "loki",
        state: "available",
        reason: null,
        truncated: false,
      }),
    );
    renderPage(<AppDetailPage />, "/admin/apps/app-one/logs");
    expect(await screen.findByText("<b>model ready</b>")).toBeInTheDocument();
    expect(screen.queryByText("model ready")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search logs"), {
      target: { value: "loading" },
    });
    await waitFor(() =>
      expect(logs).toHaveBeenLastCalledWith(
        "app-one",
        expect.any(URLSearchParams),
        expect.objectContaining({ search: "loading" }),
        expect.any(AbortSignal),
      ),
    );
  });
});

describe("App run publication", () => {
  it("uses one original operation and keeps polling until scientific results publish", async () => {
    const detail = science();
    detail.run.status = "succeeded";
    detail.semantic_validation.status = "not-run";
    detail.artifacts = [];
    const pending: AppRun = {
      app_id: app.app_id,
      operation: { ...operation(), status: "succeeded" },
      scientific: detail,
    };
    const published = structuredClone(pending);
    published.scientific!.semantic_validation.status = "passed";
    const run = vi
      .spyOn(appsApi, "run")
      .mockResolvedValueOnce(testEnvelope(pending))
      .mockResolvedValue(testEnvelope(published));
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderPage(
      <AppRunDetail appId={app.app_id} runId={pending.operation.id} />,
    );
    expect(await screen.findByText("Finalizing results")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5100);
    });
    vi.useRealTimers();
    expect(
      await screen.findByText(/Publication complete · polling stopped/),
    ).toBeInTheDocument();
    expect(run).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Finalizing results")).not.toBeInTheDocument();
    expect(appRunNeedsRefresh(published)).toBe(false);
  });
  it.each(["failed", "cancelled"] as const)(
    "does not wait for publication on %s scientific runs",
    (status) => {
      const detail = science();
      detail.run.status = status;
      detail.semantic_validation.status = "not-run";
      expect(
        appRunNeedsRefresh({
          app_id: app.app_id,
          operation: operation(),
          scientific: detail,
        }),
      ).toBe(false);
    },
  );
  it("retains published artifact download href and hash", async () => {
    const detail = science();
    detail.run.status = "succeeded";
    detail.semantic_validation.status = "passed";
    detail.artifacts = [
      {
        artifact_id: "artifact-one",
        name: "result.cif",
        role: "output",
        semantic_type: "structure",
        state: "available",
        sha256: "a".repeat(64),
        size_bytes: {
          value: 12,
          unit: "bytes",
          evidence: "measured",
          source: "artifacts",
          reason: null,
        },
        media_type: "chemical/x-cif",
        created_at: null,
        download: {
          available: true,
          href: "/admin/api/v1/scientific-runs/run-one/artifacts/artifact-one/content",
          reason: null,
        },
      },
    ];
    vi.spyOn(appsApi, "run").mockResolvedValue(
      testEnvelope({
        app_id: app.app_id,
        operation: operation(),
        scientific: detail,
      }),
    );
    renderPage(<AppRunDetail appId={app.app_id} runId="run-one" />);
    expect(
      await screen.findByRole("link", { name: "Download result.cif" }),
    ).toHaveAttribute("href", detail.artifacts[0].download.href);
    expect(screen.getByText("a".repeat(64))).toBeInTheDocument();
  });
});

describe("App settings runtime contract", () => {
  function setup() {
    const settings: AppSettings = {
      app_id: app.app_id,
      app_revision: 3,
      display_name: app.display_name,
      academic_required: false,
      execution_mode: "serving",
      serving: structuredClone(modelDeploymentRevisionFixture),
      scientific: null,
      capabilities: app.capabilities,
      unsupported_reason: null,
    };
    vi.spyOn(appsApi, "settings").mockResolvedValue(testEnvelope(settings));
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(
      testEnvelope(modelDeploymentMutationCapabilitiesFixture),
    );
    vi.spyOn(adminApi, "modelDeploymentStatus").mockResolvedValue(
      testEnvelope(modelDeploymentStatusFixture),
    );
    return settings;
  }
  it("saves through the app runtime API and preserves all untouched spec fields", async () => {
    const settings = setup();
    const save = vi
      .spyOn(appsApi, "updateSettings")
      .mockImplementation(async (_id, body) =>
        testEnvelope({
          ...settings,
          app_revision: 4,
          serving: { ...settings.serving!, spec: body.serving_spec! },
        }),
      );
    const terraform = vi.spyOn(adminApi, "reconcileConfiguration");
    renderPage(<AppSettingsTab app={app} />);
    const min = await screen.findByLabelText("Minimum ready workers");
    fireEvent.change(min, { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const body = save.mock.calls[0][1];
    expect(body.expected_app_revision).toBe(3);
    expect(body.serving_base_etag).toBe(settings.serving!.etag);
    expect(body.serving_spec).toEqual({
      ...settings.serving!.spec,
      availability: { ...settings.serving!.spec.availability, minReplicas: 1 },
    });
    expect(body.serving_spec!.availability).not.toHaveProperty(
      "startupTimeoutSeconds",
    );
    expect(terraform).not.toHaveBeenCalled();
  });
  it("shows configured GPU counts and keeps GPU snapshot choices for GPU workers", async () => {
    const settings = setup();
    settings.serving!.spec.placement.acceleratorsPerReplica = 8;
    const save = vi.spyOn(appsApi, "updateSettings");
    renderPage(<AppSettingsTab app={app} />);
    expect(
      await screen.findByLabelText("Resources per worker"),
    ).toHaveTextContent(
      "8 GPUs requested per worker. This is the configured request, not current allocation or utilization.",
    );
    expect(screen.getByLabelText("GPU snapshot")).toBeInTheDocument();
    expect(screen.queryByText(/No GPU is reserved/)).not.toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
  });
  it("shows exact CPU/RAM requests without a GPU selector and preserves hidden settings", async () => {
    const settings = setup();
    settings.serving!.spec.placement.acceleratorsPerReplica = 0;
    settings.serving!.spec.placement.cpuResources = {
      cpuMillis: 1500,
      memoryBytes: 268435456,
    };
    const original = structuredClone(settings.serving!.spec);
    const save = vi
      .spyOn(appsApi, "updateSettings")
      .mockImplementation(async (_id, body) =>
        testEnvelope({
          ...settings,
          app_revision: 4,
          serving: { ...settings.serving!, spec: body.serving_spec! },
        }),
      );
    renderPage(<AppSettingsTab app={app} />);
    expect(
      await screen.findByLabelText("Resources per worker"),
    ).toHaveTextContent(
      "CPU-only worker · 1.5 CPU cores and 256 MiB requested per worker. No GPU is reserved.",
    );
    expect(screen.queryByLabelText("GPU snapshot")).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "GPU snapshotting is not applicable to this CPU-only app.",
      ),
    ).toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Minimum ready workers"), {
      target: { value: "1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][1].serving_spec).toEqual({
      ...original,
      availability: { ...original.availability, minReplicas: 1 },
    });
    expect(settings.serving!.spec).toEqual(original);
  });
  it("allows explicit scale-to-zero while displaying missing benchmark evidence", async () => {
    const settings = setup();
    settings.serving!.spec.availability.minReplicas = 1;
    const capabilities = structuredClone(
      modelDeploymentMutationCapabilitiesFixture,
    );
    const option = capabilities.configuration_options[0]!;
    option.scale_to_zero_qualified = false;
    option.scale_to_zero_warning =
      "Scale-to-zero is not yet benchmark-qualified.";
    vi.mocked(adminApi.modelDeploymentCapabilities).mockResolvedValue(
      testEnvelope(capabilities),
    );
    const save = vi
      .spyOn(appsApi, "updateSettings")
      .mockResolvedValue(testEnvelope(settings));
    renderPage(<AppSettingsTab app={app} />);
    const min = await screen.findByLabelText("Minimum ready workers");
    expect(
      await screen.findByText(option.scale_to_zero_warning),
    ).toBeInTheDocument();
    fireEvent.change(min, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][1].serving_spec!.availability.minReplicas).toBe(
      0,
    );
  });
  it("edits the existing autoscaler cooldown without changing idle, startup or resource settings", async () => {
    const settings = setup();
    const original = structuredClone(settings.serving!.spec);
    const save = vi
      .spyOn(appsApi, "updateSettings")
      .mockResolvedValue(testEnvelope(settings));
    renderPage(<AppSettingsTab app={app} />);
    const cooldown = await screen.findByLabelText(
      "Autoscaler cooldown (seconds)",
    );
    expect(cooldown).toHaveValue(original.availability.cooldownSeconds);
    expect(cooldown).toHaveAttribute("min", "5");
    expect(cooldown).toHaveAttribute("max", "86400");
    fireEvent.change(cooldown, { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][1].serving_spec).toEqual({
      ...original,
      availability: { ...original.availability, cooldownSeconds: 120 },
    });
    expect(settings.serving!.spec).toEqual(original);
  });
  it("keeps a rejected draft visible and does not retry a revision conflict", async () => {
    setup();
    const save = vi
      .spyOn(appsApi, "updateSettings")
      .mockRejectedValue(
        new AdminApiError(
          "App changed; reload the revision",
          409,
          "request-conflict",
        ),
      );
    renderPage(<AppSettingsTab app={app} />);
    fireEvent.change(await screen.findByLabelText("App display name"), {
      target: { value: "My draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("App changed");
    expect(screen.getByLabelText("App display name")).toHaveValue("My draft");
    expect(save).toHaveBeenCalledOnce();
  });
});
