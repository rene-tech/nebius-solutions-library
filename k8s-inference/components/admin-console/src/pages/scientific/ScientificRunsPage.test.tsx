import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../../api/client";
import type { AdminEnvelope } from "../../api/types";
import type { ScientificRunList } from "../../api/scientificTypes";
import { SessionContext } from "../../auth/SessionContext";
import { tenantPrincipal, testSession } from "../../test/accessFixtures";
import { browserFixture } from "../../test/browserFixtures";
import { ScientificRunsPage } from "./ScientificRunsPage";

afterEach(() => vi.restoreAllMocks());

function fixture<T = never>(path: string): T {
  return structuredClone(browserFixture(path)) as T;
}

function prepareApi() {
  vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(fixture("/admin/api/v1/scientific-capabilities"));
  vi.spyOn(adminApi, "scientificRuns").mockResolvedValue(fixture("/admin/api/v1/scientific-runs"));
  vi.spyOn(adminApi, "scientificModels").mockResolvedValue(fixture("/admin/api/v1/scientific-models"));
}

function renderPage(entry = "/admin/scientific-runs", session = testSession) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={[entry]}><main><ScientificRunsPage /></main></MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("scientific runs fixture contract", () => {
  it("shows run attribution, service decisions, access gates, exact tiers, and evidence-qualified GPU accounting", async () => {
    prepareApi();
    renderPage();

    const completed = await screen.findByRole("row", { name: /CD8 binder backbone screen/ });
    expect(within(completed).getByText("researcher-ada")).toBeInTheDocument();
    expect(within(completed).getByText("customer-batch")).toBeInTheDocument();
    expect(within(completed).getByText("model-artifact-local")).toBeInTheDocument();
    expect(within(completed).getAllByText("measured").length).toBeGreaterThanOrEqual(4);

    const alphaFold = screen.getByRole("row", { name: /Neoantigen complex ranking/ });
    expect(within(alphaFold).getByText(/academic · verified/)).toBeInTheDocument();
    expect(within(alphaFold).getByText(/Use Granted · execution Authorized/)).toBeInTheDocument();
    expect(within(alphaFold).getByText("No request-time licence receipt")).toBeInTheDocument();
    expect(within(alphaFold).getByText((_, element) => element?.classList.contains("scientific-alternative") === true && element.textContent?.includes("OpenFold3") === true)).toHaveTextContent("it is not represented as native AlphaFold3");
    expect(within(alphaFold).getAllByText("unavailable").length).toBeGreaterThan(0);

    const cancelled = screen.getByRole("row", { name: /PD-L1 binder refinement/ });
    expect(within(cancelled).getByText(/estimated/)).toBeInTheDocument();
    expect(within(cancelled).getByText(/academic · verified/)).toBeInTheDocument();

    const bindCraft = screen.getByRole("row", { name: /BindCraft \(native PyRosetta\).*candidate/ });
    expect(within(bindCraft).getByText((_, element) => element?.classList.contains("scientific-alternative") === true && element.textContent?.includes("Open binder workflow") === true)).toHaveTextContent("not represented as native BindCraft/PyRosetta");
    expect(within(bindCraft).getByText(/GPU snapshot unsupported/)).toBeInTheDocument();

    const boltzgen = screen.getByRole("row", { name: /BoltzGen.*candidate/ });
    expect(within(boltzgen).getByText("Deployed / pinned revision")).toBeInTheDocument();
    expect(within(boltzgen).getByText("Available upstream revision · unqualified")).toBeInTheDocument();
    expect(within(boltzgen).getByTitle("1".repeat(40))).toHaveTextContent("11111111111111…1111");
  });

  it("sends only validated scientific run filters and follows the opaque cursor", async () => {
    prepareApi();
    const runs = vi.mocked(adminApi.scientificRuns);
    const first = fixture<AdminEnvelope<ScientificRunList>>("/admin/api/v1/scientific-runs");
    runs.mockResolvedValueOnce({
      ...first,
      data: { ...first.data, next_cursor: "scientific-next" },
    });
    renderPage("/admin/scientific-runs?project=p1&tenant=oncology&model=rfdiffusion&run_status=failed&service_class=customer-batch&access_state=verified&token=must-not-flow");

    expect(await screen.findByText("Scientific run ledger")).toBeInTheDocument();
    await waitFor(() => expect(runs).toHaveBeenCalledOnce());
    expect(runs.mock.calls[0][0].toString()).toBe("project=p1");
    expect(runs.mock.calls[0][1]).toMatchObject({
      tenantId: "oncology",
      modelId: "rfdiffusion",
      status: "failed",
      serviceClass: "customer-batch",
      accessState: "verified",
      limit: 100,
    });

    fireEvent.click(await screen.findByRole("button", { name: "Next page" }));
    await waitFor(() => expect(runs).toHaveBeenCalledTimes(2));
    expect(runs.mock.calls[1][1]).toMatchObject({ cursor: "scientific-next" });
  });

  it("ignores invalid enum filters and has no automated accessibility violations", async () => {
    prepareApi();
    const { container } = renderPage("/admin/scientific-runs?run_status=made-up&service_class=urgent&access_state=secret");

    expect(await screen.findByText("Invalid filter ignored")).toBeInTheDocument();
    expect(vi.mocked(adminApi.scientificRuns).mock.calls[0][1]).toMatchObject({
      status: undefined,
      serviceClass: undefined,
      accessState: undefined,
    });
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it("keeps model readiness available when the run repository fails", async () => {
    vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(fixture("/admin/api/v1/scientific-capabilities"));
    vi.spyOn(adminApi, "scientificRuns").mockRejectedValue(new AdminApiError(
      "Scientific controller reporting is unavailable.",
      503,
      "request-science",
      "scientific_controller_unavailable",
    ));
    vi.spyOn(adminApi, "scientificModels").mockResolvedValue(fixture("/admin/api/v1/scientific-models"));
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Scientific controller reporting is unavailable.");
    expect(screen.getByRole("alert")).toHaveTextContent("request-science");
    expect(await screen.findByRole("heading", { name: "Scientific model readiness" })).toBeInTheDocument();
    expect(await screen.findByRole("row", { name: /RFdiffusion.*qualified/ })).toBeInTheDocument();
  });

  it("shows partial-source state even when a real endpoint returns no rows", async () => {
    const partial = fixture<AdminEnvelope<ScientificRunList>>("/admin/api/v1/scientific-runs");
    partial.data.items = [];
    partial.meta.sources = [{
      id: "scientific-artifacts",
      state: "unavailable",
      observed_at: null,
      age_seconds: null,
      reason: "artifact repository unavailable",
    }];
    vi.spyOn(adminApi, "scientificRuns").mockResolvedValue(partial);
    vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(fixture("/admin/api/v1/scientific-capabilities"));
    vi.spyOn(adminApi, "scientificModels").mockResolvedValue(fixture("/admin/api/v1/scientific-models"));
    renderPage();

    expect(await screen.findByText("Partial data")).toBeInTheDocument();
    expect(screen.getByText("scientific-artifacts: artifact repository unavailable")).toBeInTheDocument();
    expect(screen.getByText("No scientific runs match the selected context.")).toBeInTheDocument();
  });

  it("does not call an absent run backend and still renders model readiness", async () => {
    const capabilities = fixture<AdminEnvelope<import("../../api/scientificTypes").ScientificCapabilities>>("/admin/api/v1/scientific-capabilities");
    capabilities.data.run_history = { available: false, reason: "The durable scientific run reader is not configured." };
    capabilities.data.artifacts = { available: false, reason: "The artifact result reader is not configured." };
    vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(capabilities);
    const runs = vi.spyOn(adminApi, "scientificRuns");
    vi.spyOn(adminApi, "scientificModels").mockResolvedValue(fixture("/admin/api/v1/scientific-models"));

    renderPage();

    expect(await screen.findByText("Scientific run history is not enabled")).toBeInTheDocument();
    expect(screen.getByText("The durable scientific run reader is not configured.")).toBeInTheDocument();
    expect(await screen.findByRole("row", { name: /RFdiffusion.*qualified/ })).toBeInTheDocument();
    expect(runs).not.toHaveBeenCalled();
  });

  it("lets a tenant viewer use run history without requesting global model readiness", async () => {
    const capabilities = fixture<AdminEnvelope<import("../../api/scientificTypes").ScientificCapabilities>>("/admin/api/v1/scientific-capabilities");
    capabilities.data.model_readiness = {
      available: false,
      reason: "Scientific model readiness requires a global operator.",
    };
    vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(capabilities);
    const runs = vi.spyOn(adminApi, "scientificRuns").mockResolvedValue(fixture("/admin/api/v1/scientific-runs"));
    const models = vi.spyOn(adminApi, "scientificModels");

    renderPage(
      "/admin/scientific-runs",
      { ...testSession, principal: { ...tenantPrincipal, tenant_id: "tenant-oncology" } },
    );

    expect(await screen.findByRole("row", { name: /CD8 binder backbone screen/ })).toBeInTheDocument();
    expect(await screen.findByText("Scientific model readiness is not enabled")).toBeInTheDocument();
    expect(runs.mock.calls[0][1]).toMatchObject({ tenantId: "tenant-oncology" });
    expect(models).not.toHaveBeenCalled();
  });
});
