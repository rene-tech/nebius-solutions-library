import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "../api/client";
import { SessionContext } from "../auth/SessionContext";
import { ModelDetailPage } from "../pages/ModelDetailPage";
import { ModelsPage } from "../pages/ModelsPage";
import { OperationDetailPage } from "../pages/OperationDetailPage";
import { OperationsPage } from "../pages/OperationsPage";
import { OverviewPage } from "../pages/OverviewPage";
import { AcademicAssetsPage } from "../pages/academic/AcademicAssetsPage";
import { AccessPage } from "../pages/access/AccessPage";
import { AuditPage } from "../pages/audit/AuditPage";
import { CapacityPage } from "../pages/capacity/CapacityPage";
import { ConfigurationPage } from "../pages/configuration/ConfigurationPage";
import { ObservabilityPage } from "../pages/observability/ObservabilityPage";
import { ScientificRunDetailPage } from "../pages/scientific/ScientificRunDetailPage";
import { ScientificRunsPage } from "../pages/scientific/ScientificRunsPage";
import { testSession } from "../test/accessFixtures";
import { browserFixture } from "../test/browserFixtures";

afterEach(() => vi.restoreAllMocks());

function fixture(path: string): never {
  return structuredClone(browserFixture(path)) as never;
}

function prepareLiveApi() {
  vi.spyOn(adminApi, "overview").mockResolvedValue(fixture("/admin/api/v1/overview"));
  vi.spyOn(adminApi, "models").mockResolvedValue(fixture("/admin/api/v1/models"));
  vi.spyOn(adminApi, "model").mockResolvedValue(fixture("/admin/api/v1/models/qwen3-8b"));
  vi.spyOn(adminApi, "operations").mockResolvedValue(fixture("/admin/api/v1/operations"));
  vi.spyOn(adminApi, "operation").mockResolvedValue(fixture("/admin/api/v1/operations/10f61fc4-4211-4bb8-a058-b11a8c078520"));
  vi.spyOn(adminApi, "scientificRuns").mockResolvedValue(fixture("/admin/api/v1/scientific-runs"));
  vi.spyOn(adminApi, "scientificRun").mockResolvedValue(fixture("/admin/api/v1/scientific-runs/run-rfdiffusion-0001"));
  vi.spyOn(adminApi, "scientificModels").mockResolvedValue(fixture("/admin/api/v1/scientific-models"));
  vi.spyOn(adminApi, "academicAssets").mockResolvedValue(fixture("/admin/api/v1/academic-assets"));
  vi.spyOn(adminApi, "principals").mockResolvedValue(fixture("/admin/api/v1/principals"));
  vi.spyOn(adminApi, "keys").mockResolvedValue(fixture("/admin/api/v1/keys"));
  vi.spyOn(adminApi, "audit").mockResolvedValue(fixture("/admin/api/v1/audit"));
  vi.spyOn(adminApi, "capacity").mockResolvedValue(fixture("/admin/api/v1/capacity"));
  vi.spyOn(adminApi, "observability").mockResolvedValue(fixture("/admin/api/v1/observability"));
  vi.spyOn(adminApi, "configuration").mockResolvedValue(fixture("/admin/api/v1/configuration"));
}

function renderRoute(path: string, route: string, element: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: testSession, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={[path]}>
          <Routes><Route path={route} element={element} /></Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("live-shaped admin route responses", () => {
  it.each([
    ["overview", "/admin", "/admin", <OverviewPage />, "Inference at a glance"],
    ["models", "/admin/models", "/admin/models", <ModelsPage />, "Qwen3 8B"],
    ["model detail", "/admin/models/qwen3-8b", "/admin/models/:modelId", <ModelDetailPage />, "Why this model is hot:"],
    ["operations", "/admin/operations", "/admin/operations", <OperationsPage />, "chat.completion"],
    ["operation detail", "/admin/operations/10f61fc4-4211-4bb8-a058-b11a8c078520", "/admin/operations/:operationId", <OperationDetailPage />, "Usage evidence"],
    ["scientific runs", "/admin/scientific-runs", "/admin/scientific-runs", <ScientificRunsPage />, "Scientific run ledger"],
    ["scientific run detail", "/admin/scientific-runs/run-rfdiffusion-0001", "/admin/scientific-runs/:runId", <ScientificRunDetailPage />, "Stages and attempts"],
    ["academic assets", "/admin/academic-assets", "/admin/academic-assets", <AcademicAssetsPage />, "Licensed academic asset readiness"],
    ["access", "/admin/access", "/admin/access", <AccessPage />, "Scoped API keys"],
    ["capacity", "/admin/capacity", "/admin/capacity", <CapacityPage />, "Capacity, queues and elastic supply"],
    ["observability", "/admin/observability", "/admin/observability", <ObservabilityPage />, "Health, signals and verified tools"],
    ["configuration", "/admin/configuration", "/admin/configuration", <ConfigurationPage />, "Configuration planning"],
    ["audit", "/admin/audit", "/admin/audit", <AuditPage />, "Append-only administrative events"],
  ])("renders the %s route from the backend contract", async (_name, path, route, element, expected) => {
    prepareLiveApi();
    renderRoute(path, route, element);
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });
});
