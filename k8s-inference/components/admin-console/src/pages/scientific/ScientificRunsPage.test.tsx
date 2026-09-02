import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "../../api/client";
import type { AdminEnvelope } from "../../api/types";
import type { ScientificRunList } from "../../api/scientificTypes";
import { SessionContext } from "../../auth/SessionContext";
import { testSession } from "../../test/accessFixtures";
import { browserFixture } from "../../test/browserFixtures";
import { ScientificRunsPage } from "./ScientificRunsPage";

afterEach(() => vi.restoreAllMocks());

function fixture<T = never>(path: string): T {
  return structuredClone(browserFixture(path)) as T;
}

function prepareApi() {
  vi.spyOn(adminApi, "scientificRuns").mockResolvedValue(fixture("/admin/api/v1/scientific-runs"));
  vi.spyOn(adminApi, "scientificModels").mockResolvedValue(fixture("/admin/api/v1/scientific-models"));
}

function renderPage(entry = "/admin/scientific-runs") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: testSession, logout: async () => undefined, loggingOut: false, logoutError: null }}>
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

    const blocked = screen.getByRole("row", { name: /Neoantigen complex ranking/ });
    expect(within(blocked).getByText(/academic · blocked/)).toBeInTheDocument();
    expect(within(blocked).getByText((_, element) => element?.classList.contains("scientific-alternative") === true && element.textContent?.includes("OpenFold3") === true)).toHaveTextContent("it is not represented as native AlphaFold3");
    expect(within(blocked).getAllByText("unavailable").length).toBeGreaterThan(0);

    const cancelled = screen.getByRole("row", { name: /PD-L1 binder refinement/ });
    expect(within(cancelled).getByText(/estimated/)).toBeInTheDocument();
    expect(within(cancelled).getByText(/academic · verified/)).toBeInTheDocument();

    const bindCraft = screen.getByRole("row", { name: /BindCraft \(native PyRosetta\).*candidate/ });
    expect(within(bindCraft).getByText((_, element) => element?.classList.contains("scientific-alternative") === true && element.textContent?.includes("Open binder workflow") === true)).toHaveTextContent("not represented as native BindCraft/PyRosetta");
    expect(within(bindCraft).getByText(/GPU snapshot unsupported/)).toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
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
});
