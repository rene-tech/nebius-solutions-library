import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OperatorRole } from "../../api/accessTypes";
import { adminApi, AdminApiError } from "../../api/client";
import { SessionContext } from "../../auth/SessionContext";
import { testEnvelope, testPrincipal, testSession } from "../../test/accessFixtures";
import {
  awaitingStatus,
  completedStatus,
  configurationPlan,
  configurationRevision,
  proposedConfiguration,
  terraformHandoff,
} from "../../test/configurationFixtures";
import { ConfigurationPage } from "./ConfigurationPage";

afterEach(() => vi.restoreAllMocks());

function renderPage(role: OperatorRole = "admin") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const principal = { ...testPrincipal, role };
  render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: { ...testSession, principal }, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={["/admin/configuration"]}><ConfigurationPage /></MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

function changeCooldown(value = "301") {
  fireEvent.change(screen.getByLabelText("Cooldown"), { target: { value } });
}

describe("Configuration page", () => {
  it("keeps non-autoscaling fields read-only and role-gates planning and rollback", async () => {
    vi.spyOn(adminApi, "configuration").mockResolvedValue(testEnvelope(configurationRevision));
    const diff = vi.spyOn(adminApi, "configurationDiff").mockResolvedValue(testEnvelope(configurationPlan().diff));
    const validate = vi.spyOn(adminApi, "validateConfiguration").mockResolvedValue(testEnvelope(configurationPlan().validation));
    renderPage("viewer");

    expect(await screen.findByText("qwen3-8b")).toBeInTheDocument();
    expect(screen.getAllByText("Read-only · not applicable").length).toBeGreaterThan(1);
    expect(screen.getByText(/only minimum\/maximum replicas/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create Terraform handoff" })).not.toBeInTheDocument();
    expect(screen.getByText(/Administrator role required/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /apply/i })).not.toBeInTheDocument();

    changeCooldown();
    fireEvent.click(screen.getByRole("button", { name: "Review diff" }));
    await waitFor(() => expect(diff).toHaveBeenCalledWith({ base_etag: configurationRevision.etag, desired: proposedConfiguration() }));
    fireEvent.click(screen.getByRole("button", { name: "Validate proposal" }));
    await waitFor(() => expect(validate).toHaveBeenCalledOnce());
  });

  it("creates a secret-free one-time handoff and tracks receipt completion", async () => {
    vi.spyOn(adminApi, "configuration").mockResolvedValue(testEnvelope(configurationRevision));
    const plan = vi.spyOn(adminApi, "planConfiguration").mockResolvedValue(testEnvelope(configurationPlan()));
    const reconcile = vi.spyOn(adminApi, "reconcileConfiguration").mockResolvedValue(testEnvelope(awaitingStatus));
    vi.spyOn(adminApi, "reconciliationStatus")
      .mockResolvedValueOnce(testEnvelope(awaitingStatus))
      .mockResolvedValue(testEnvelope(completedStatus));
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    renderPage("operator");

    await screen.findByText("qwen3-8b");
    changeCooldown();
    fireEvent.click(screen.getByRole("button", { name: "Create Terraform handoff" }));
    await waitFor(() => expect(plan).toHaveBeenCalledOnce());
    expect(await screen.findByRole("textbox", { name: "Deterministic tfvars JSON" })).toHaveValue(terraformHandoff.tfvars_json);
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("textbox", { name: "Deterministic tfvars JSON" })).not.toBeInTheDocument();
    expect(storageSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Start Terraform handoff tracking" }));
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith({ plan_id: configurationPlan().plan_id, base_etag: configurationRevision.etag }));
    expect(await screen.findByRole("heading", { name: "AWAITING_TERRAFORM" })).toBeInTheDocument();
    await waitFor(() => expect(adminApi.reconciliationStatus).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "RECEIPT COMPLETE" })).toBeInTheDocument());
    expect(screen.getByText(/accepted atomically as revision 3/)).toBeInTheDocument();
  });

  it("fails closed on a concurrent ETag conflict until current state is refreshed", async () => {
    vi.spyOn(adminApi, "configuration").mockResolvedValue(testEnvelope(configurationRevision));
    vi.spyOn(adminApi, "planConfiguration").mockRejectedValue(new AdminApiError("configuration changed after this plan", 409, "request-conflict"));
    renderPage();

    await screen.findByText("qwen3-8b");
    changeCooldown();
    fireEvent.click(screen.getByRole("button", { name: "Create Terraform handoff" }));
    expect(await screen.findByText("Concurrent revision conflict.")).toBeInTheDocument();
    expect(screen.getByLabelText("Cooldown")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Start Terraform handoff tracking" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh current revision" }));
    await waitFor(() => expect(screen.getByLabelText("Cooldown")).toHaveValue(300));
  });

  it("never exposes tracking or reconciliation for an unsafe valid-plan handoff", async () => {
    vi.spyOn(adminApi, "configuration").mockResolvedValue(testEnvelope(configurationRevision));
    const unsafe = configurationPlan();
    unsafe.terraform.variables = { nested: { api_key: "must-not-render" } };
    unsafe.terraform.tfvars_json = JSON.stringify(unsafe.terraform.variables);
    vi.spyOn(adminApi, "planConfiguration").mockResolvedValue(testEnvelope(unsafe));
    const reconcile = vi.spyOn(adminApi, "reconcileConfiguration");
    renderPage();

    await screen.findByText("qwen3-8b");
    changeCooldown();
    fireEvent.click(screen.getByRole("button", { name: "Create Terraform handoff" }));
    expect(await screen.findByText(/forbidden secret-bearing key/)).toBeInTheDocument();
    expect(screen.queryByText("must-not-render")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Deterministic tfvars JSON" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start Terraform handoff tracking" })).not.toBeInTheDocument();
    expect(reconcile).not.toHaveBeenCalled();
  });

  it("never exposes tracking when a valid plan omits its required handoff", async () => {
    vi.spyOn(adminApi, "configuration").mockResolvedValue(testEnvelope(configurationRevision));
    const missing = configurationPlan();
    missing.terraform = { ...missing.terraform, required: false, state: "not-required" };
    vi.spyOn(adminApi, "planConfiguration").mockResolvedValue(testEnvelope(missing));
    const reconcile = vi.spyOn(adminApi, "reconcileConfiguration");
    renderPage();

    await screen.findByText("qwen3-8b");
    changeCooldown();
    fireEvent.click(screen.getByRole("button", { name: "Create Terraform handoff" }));
    expect(await screen.findByText(/did not include the required reviewed Terraform handoff/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Deterministic tfvars JSON" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start Terraform handoff tracking" })).not.toBeInTheDocument();
    expect(reconcile).not.toHaveBeenCalled();
  });

  it("shows unsupported-change rejection without tfvars or reconciliation", async () => {
    vi.spyOn(adminApi, "configuration").mockResolvedValue(testEnvelope(configurationRevision));
    const rejected = configurationPlan("rejected");
    rejected.terraform = terraformHandoff;
    vi.spyOn(adminApi, "planConfiguration").mockResolvedValue(testEnvelope(rejected));
    const reconcile = vi.spyOn(adminApi, "reconcileConfiguration");
    renderPage();

    await screen.findByText("qwen3-8b");
    changeCooldown();
    fireEvent.click(screen.getByRole("button", { name: "Create Terraform handoff" }));
    expect(await screen.findByText("configuration_change_not_applicable")).toBeInTheDocument();
    expect(screen.getByText("Rejected plans cannot enter reconciliation or produce tfvars.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Deterministic tfvars JSON" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start Terraform handoff tracking" })).not.toBeInTheDocument();
    expect(reconcile).not.toHaveBeenCalled();
  });

  it("allows only administrators to create a rollback handoff", async () => {
    vi.spyOn(adminApi, "configuration").mockResolvedValue(testEnvelope(configurationRevision));
    const rollbackPlan = configurationPlan();
    const rollback = vi.spyOn(adminApi, "rollbackConfiguration").mockResolvedValue(testEnvelope({ target_revision: 1, plan: rollbackPlan }));
    renderPage("admin");

    await screen.findByText("qwen3-8b");
    fireEvent.click(screen.getByRole("button", { name: "Create rollback handoff" }));
    await waitFor(() => expect(rollback).toHaveBeenCalledWith({ target_revision: 1, base_etag: configurationRevision.etag }));
    expect(await screen.findByRole("heading", { name: "Terraform handoff ready" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.getByRole("heading", { name: "Rollback plan to revision 1" })).toBeInTheDocument();
  });
});
