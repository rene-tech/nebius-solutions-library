import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../../api/client";
import type { ScientificCapabilities, ScientificModelPolicy, ScientificModelPolicyList } from "../../api/scientificTypes";
import type { AdminEnvelope } from "../../api/types";
import { SessionContext } from "../../auth/SessionContext";
import { tenantPrincipal, testPrincipal, testSession } from "../../test/accessFixtures";
import { browserFixture } from "../../test/browserFixtures";
import { rfdiffusionCappedPolicy, scientificModelReadinessFixture } from "../../test/scientificFixtures";
import { draftUpdate, policyBlocker, ScientificModelPolicyPanel } from "./ScientificModelPolicyPanel";

afterEach(() => vi.restoreAllMocks());

function fixture<T = never>(path: string): T {
  return structuredClone(browserFixture(path)) as T;
}

function capabilities(overrides: Partial<ScientificCapabilities> = {}): ScientificCapabilities {
  return { ...fixture<AdminEnvelope<ScientificCapabilities>>("/admin/api/v1/scientific-capabilities").data, ...overrides };
}

function renderPanel(options: {
  capabilities?: ScientificCapabilities;
  session?: typeof testSession;
  scopeTenantId?: string;
} = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const session = options.session ?? testSession;
  const canOperate = session.principal.role !== "viewer";
  const view = render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={["/admin/scientific-runs?project=p1"]}>
          <main>
            <ScientificModelPolicyPanel
              canOperate={canOperate}
              capabilities={options.capabilities ?? capabilities()}
              capabilitiesPending={false}
              context={new URLSearchParams({ project: "p1" })}
              readiness={scientificModelReadinessFixture.items}
              scopeTenantId={options.scopeTenantId}
            />
          </main>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

describe("scientific model dispatch policy panel", () => {
  it("shows desired and effective policy with live counts and never claims a resident runtime", async () => {
    const list = vi.spyOn(adminApi, "scientificModelPolicies").mockResolvedValue(fixture("/admin/api/v1/scientific-model-policies"));
    const { container } = renderPanel();

    const capped = await screen.findByRole("row", { name: /RFdiffusion/ });
    expect(within(capped).getByText("at-limit")).toBeInTheDocument();
    expect(within(capped).getByText(/1 active run\(s\) meet the cap of 1/)).toBeInTheDocument();
    expect(within(capped).getByText("Effective cap 1")).toBeInTheDocument();
    expect(within(capped).getByText(/Dispatching · cap 1 · r3/)).toBeInTheDocument();
    expect(within(capped).getByText("Customer PoC: one H100 run at a time.")).toBeInTheDocument();
    expect(within(capped).getByText(/operator-ada/)).toBeInTheDocument();
    expect(within(capped).getByRole("button", { name: "Edit policy" })).toBeInTheDocument();

    const paused = screen.getByRole("row", { name: /AlphaFold3 \(native\)/ });
    expect(within(paused).getByText("paused")).toBeInTheDocument();
    expect(within(paused).getByText(/running work drains and results still publish/)).toBeInTheDocument();

    const open = screen.getByRole("row", { name: /BoltzGen/ });
    expect(within(open).getByText("open")).toBeInTheDocument();
    expect(within(open).getByText("No policy row · open by default")).toBeInTheDocument();
    expect(within(open).getByText("Effective cap none (Kueue quota only)")).toBeInTheDocument();
    expect(within(open).getByRole("button", { name: "Set policy" })).toBeInTheDocument();

    expect(screen.getByText(/there is no always-hot resident scientific runtime/)).toBeInTheDocument();
    expect(screen.getByText("Scope all tenants")).toBeInTheDocument();
    expect(list).toHaveBeenCalledOnce();
    expect(list.mock.calls[0][0].toString()).toBe("project=p1");
    expect(list.mock.calls[0][1]).toBeUndefined();
    expect(screen.queryByText(/warm replica|snapshot restore|resident runtime available/i)).not.toBeInTheDocument();

    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it("sends a full replacement at the displayed revision and shows the refreshed durable row", async () => {
    vi.spyOn(adminApi, "scientificModelPolicies").mockResolvedValue(fixture("/admin/api/v1/scientific-model-policies"));
    const refreshed: AdminEnvelope<ScientificModelPolicy> = fixture("/admin/api/v1/scientific-model-policies/rfdiffusion");
    refreshed.data.desired = { ...refreshed.data.desired, revision: 4, paused: true, max_active_runs: 2, reason: "Maintenance window" };
    refreshed.data.effective = { state: "paused", paused: true, max_active_runs: 2, reason: "New dispatch for all tenants is paused by the all-tenants policy; running work drains and results still publish." };
    const set = vi.spyOn(adminApi, "setScientificModelPolicy").mockResolvedValue(refreshed);
    renderPanel();

    const row = await screen.findByRole("row", { name: /RFdiffusion/ });
    fireEvent.click(within(row).getByRole("button", { name: "Edit policy" }));
    const form = within(row).getByRole("form", { name: /Dispatch policy for RFdiffusion/ });
    fireEvent.click(within(form).getByLabelText("Pause new dispatch"));
    fireEvent.change(within(form).getByLabelText("Max active runs"), { target: { value: "2" } });
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "Maintenance window" } });
    fireEvent.click(within(form).getByRole("button", { name: "Apply policy" }));

    await waitFor(() => expect(set).toHaveBeenCalledOnce());
    expect(set.mock.calls[0][0]).toBe("rfdiffusion");
    expect(set.mock.calls[0][1]).toEqual({ expected_revision: 3, paused: true, max_active_runs: 2, reason: "Maintenance window" });
    expect(set.mock.calls[0][2].toString()).toBe("project=p1");
    expect(set.mock.calls[0][3]).toBeUndefined();
    const updated = await screen.findByRole("row", { name: /RFdiffusion/ });
    expect(within(updated).getByText(/Paused · cap 2 · r4/)).toBeInTheDocument();
    expect(within(updated).getByText("Policy applied.")).toBeInTheDocument();
    expect(within(updated).queryByRole("form")).not.toBeInTheDocument();
  });

  it("explains a stale revision without overwriting and rejects an out-of-bound cap before any request", async () => {
    vi.spyOn(adminApi, "scientificModelPolicies").mockResolvedValue(fixture("/admin/api/v1/scientific-model-policies"));
    const set = vi.spyOn(adminApi, "setScientificModelPolicy").mockRejectedValue(new AdminApiError(
      "scientific model policy revision changed; current revision is 5",
      409,
      "request-policy-409",
      "scientific_model_policy_stale",
    ));
    renderPanel();

    const row = await screen.findByRole("row", { name: /AlphaFold3 \(native\)/ });
    fireEvent.click(within(row).getByRole("button", { name: "Edit policy" }));
    const form = within(row).getByRole("form", { name: /Dispatch policy for AlphaFold3/ });
    fireEvent.change(within(form).getByLabelText("Max active runs"), { target: { value: "500" } });
    fireEvent.click(within(form).getByRole("button", { name: "Apply policy" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("between 1 and 64");
    expect(set).not.toHaveBeenCalled();

    fireEvent.change(within(form).getByLabelText("Max active runs"), { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Apply policy" }));
    await waitFor(() => expect(set).toHaveBeenCalledOnce());
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent("Policy changed elsewhere.");
    expect(alert).toHaveTextContent("current revision is 5");
    expect(alert).toHaveTextContent("request-policy-409");
    expect(within(row).getByText(/Paused · no cap · r1/)).toBeInTheDocument();
  });

  it("offers only published snapshot bundles and sends the chosen startup policy", async () => {
    const listed: AdminEnvelope<ScientificModelPolicyList> = fixture("/admin/api/v1/scientific-model-policies");
    const policy = listed.data.items.find((item) => item.model_id === "rfdiffusion")!;
    policy.startup_options = { "sample-structure": ["qualified-test-bundle"] };
    policy.desired.startup_policies = {};
    vi.spyOn(adminApi, "scientificModelPolicies").mockResolvedValue(listed);
    const refreshed: AdminEnvelope<ScientificModelPolicy> = fixture("/admin/api/v1/scientific-model-policies/rfdiffusion");
    const set = vi.spyOn(adminApi, "setScientificModelPolicy").mockResolvedValue(refreshed);
    renderPanel();
    const row = await screen.findByRole("row", { name: /RFdiffusion/ });
    fireEvent.click(within(row).getByRole("button", { name: "Edit policy" }));
    const select = within(row).getByRole("combobox", { name: "Startup for sample-structure" });
    expect(within(select).getAllByRole("option")).toHaveLength(3);
    fireEvent.change(select, { target: { value: "qualified-test-bundle" } });
    fireEvent.click(within(row).getByRole("button", { name: "Apply policy" }));
    await waitFor(() => expect(set).toHaveBeenCalledOnce());
    expect(set.mock.calls[0][1].startup_policies).toEqual({
      "sample-structure": { backend: "cuda-criu", bundle_id: "qualified-test-bundle" },
    });
  });

  it("keeps a viewer read-only and scopes a tenant operator to its own tenant", async () => {
    const list = vi.spyOn(adminApi, "scientificModelPolicies").mockResolvedValue(fixture("/admin/api/v1/scientific-model-policies"));
    renderPanel({ session: { ...testSession, principal: { ...testPrincipal, role: "viewer" } } });
    await screen.findByRole("row", { name: /RFdiffusion/ });
    expect(screen.getAllByText("Operator role required to change dispatch policy.").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /policy/ })).not.toBeInTheDocument();

    list.mockClear();
    renderPanel({ session: { ...testSession, principal: { ...tenantPrincipal, tenant_id: "tenant-oncology" } }, scopeTenantId: "tenant-oncology" });
    await waitFor(() => expect(list).toHaveBeenCalledOnce());
    expect(list.mock.calls[0][1]).toBe("tenant-oncology");
    expect(screen.getByText("Scope tenant tenant-oncology")).toBeInTheDocument();
  });

  it("does not call an absent policy backend and says why", async () => {
    const list = vi.spyOn(adminApi, "scientificModelPolicies");
    renderPanel({ capabilities: capabilities({ model_policy: { available: false, reason: "no durable scientific model policy repository is bound to this build" } }) });
    expect(await screen.findByText("Scientific model dispatch policy is not enabled")).toBeInTheDocument();
    expect(screen.getByText("no durable scientific model policy repository is bound to this build")).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
  });

  it("validates drafts and blockers as pure functions", () => {
    expect(policyBlocker(undefined, true)).toBe("This build does not publish a scientific model policy command.");
    expect(policyBlocker(capabilities(), false)).toBe("Operator role required to change dispatch policy.");
    expect(policyBlocker(capabilities(), true)).toBeNull();
    expect(draftUpdate(rfdiffusionCappedPolicy, {
      paused: false, maxActiveRuns: "", reason: "",
      startupPolicies: { "sample-structure": { backend: "cuda-criu", bundle_id: "protenix-r2" } },
    }).startup_policies).toEqual({ "sample-structure": { backend: "cuda-criu", bundle_id: "protenix-r2" } });
    expect(draftUpdate(rfdiffusionCappedPolicy, { paused: false, maxActiveRuns: " 3 ", reason: "  " })).toEqual({
      expected_revision: 3,
      paused: false,
      max_active_runs: 3,
      reason: null,
    });
    expect(draftUpdate(rfdiffusionCappedPolicy, { paused: true, maxActiveRuns: "", reason: "hold" })).toEqual({
      expected_revision: 3,
      paused: true,
      max_active_runs: null,
      reason: "hold",
    });
    expect(() => draftUpdate(rfdiffusionCappedPolicy, { paused: false, maxActiveRuns: "0", reason: "" })).toThrow(/between 1 and 64/);
    expect(() => draftUpdate(rfdiffusionCappedPolicy, { paused: false, maxActiveRuns: "1.5", reason: "" })).toThrow(/whole number/);
    expect(() => draftUpdate(rfdiffusionCappedPolicy, { paused: false, maxActiveRuns: "", reason: "x".repeat(301) })).toThrow(/300/);
  });
});

describe("scientific model policy list contract", () => {
  it("carries desired and effective policy separately for every model in the readiness fixture", () => {
    const list = fixture<AdminEnvelope<ScientificModelPolicyList>>("/admin/api/v1/scientific-model-policies").data;
    expect(list.scope_tenant_id).toBeNull();
    expect(new Set(list.items.map((item) => item.model_id))).toEqual(new Set(scientificModelReadinessFixture.items.map((item) => item.model_id)));
    for (const item of list.items) {
      expect(item.enforcement.preemptive).toBe(false);
      expect(item.enforcement.resident_runtime).toBe("none-batch-jobs-only");
      expect(item.effective.paused).toBe(item.effective.state === "paused");
      if (item.desired.revision === 0) expect(item.desired.paused).toBe(false);
    }
  });
});
