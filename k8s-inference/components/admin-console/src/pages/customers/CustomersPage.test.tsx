import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/client";
import type { CustomerList, Workbench } from "../../api/customerTypes";
import * as sessions from "../../auth/SessionContext";
import { testEnvelope, testSession } from "../../test/accessFixtures";
import { CustomerActions, WorkbenchUpgrade } from "./CustomerActions";
import { CustomersPage, customerWindowKey } from "./CustomersPage";

const workbench: Workbench = {
  id: "00000000-0000-0000-0000-000000000001", name: "LynxKite", tenant_id: "lynx", principal_ids: ["lynx"],
  management: "managed", endpoint_id: "aiendpoint-lynx", project_id: "project-test", bucket_name: "fs2-lynx-bucket",
  protected: true, protection_reason: "Owner hold", desired_release: null, revision: 1,
  observation: null, observation_state: "unavailable", operations: [],
};
const fleet: CustomerList = {
  items: [{ tenant_id: "lynx", profile: { display_name: "LynxKite", purpose: "customer", archived: false },
    users: [], requests: 18, active_keys: 1, last_request_at: null,
    model_usage: [{ model_id: "gromacs", requests: 18, succeeded: 17, failed: 1, in_progress: 0, users: 1,
      last_request_at: "2026-10-02T10:00:00Z" }], buckets: [], workbenches: [workbench] }],
  truncated: false, inventory_available: true, inventory_error: null, lifecycle_executor_available: false,
  attribution: "Accepted operations, not polls",
};

afterEach(() => vi.restoreAllMocks());
function page(detail = false) {
  vi.spyOn(sessions, "useSession").mockReturnValue({ session: testSession } as ReturnType<typeof sessions.useSession>);
  const query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={query}><MemoryRouter initialEntries={[
    detail ? "/admin/customers/lynx" : "/admin/customers",
  ]}><Routes><Route path="/admin/customers" element={<CustomersPage />} />
    <Route path="/admin/customers/:tenantId" element={<CustomersPage />} /></Routes></MemoryRouter></QueryClientProvider>);
}

describe("Customer workbenches", () => {
  it("does not abandon a slow live request on every shared clock tick", () => {
    const first = new URLSearchParams("from=first&to=second&project=customer");
    const next = new URLSearchParams("from=third&to=fourth&project=customer");
    expect(customerWindowKey(first, true, "168")).toBe(customerWindowKey(next, true, "168"));
    expect(customerWindowKey(first, false, "custom")).not.toBe(customerWindowKey(next, false, "custom"));
    expect(customerWindowKey(first, true, "1")).not.toBe(customerWindowKey(next, true, "168"));
  });
  it("shows actual model usage and unknown cloud state without inventing zero", async () => {
    vi.spyOn(api, "envelopeRequest").mockResolvedValue(testEnvelope(fleet));
    page();
    expect(await screen.findByRole("link", { name: "LynxKite" })).toHaveAttribute("href", expect.stringContaining("/customers/lynx"));
    expect(screen.getByText("gromacs")).toBeInTheDocument();
    expect(screen.getByText("LynxKite: Unknown")).toBeInTheDocument();
    expect(screen.getByText("18")).toBeInTheDocument();
  });
  it("protected instances never expose upgrade controls", async () => {
    vi.spyOn(api, "envelopeRequest").mockImplementation(async path => testEnvelope(
      path.startsWith("/workbench-inventory") ? { items: [], last_error: null } : { ...fleet, customer: fleet.items[0] },
    ) as never);
    page(true);
    expect(await screen.findByText("Protected — no changes")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upgrade LibreChat" })).not.toBeInTheDocument();
  });
  it("legacy local disk cannot be upgraded", () => {
    render(<QueryClientProvider client={new QueryClient()}><WorkbenchUpgrade
      value={{ ...workbench, protected: false }} releases={{ candidate: "image" }} enabled /></QueryClientProvider>);
    expect(screen.getByText(/Migration required:/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
  it("uses a durable command and explicit interruption acknowledgement", async () => {
    // Exercise the real HTTP serializer: mocking envelopeRequest hid a
    // double-encoded body that the live server correctly rejected with 422.
    const request = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(testEnvelope({ id: "operation", state: "queued" })),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    render(<QueryClientProvider client={new QueryClient()}><WorkbenchUpgrade
      value={{ ...workbench, protected: false, state_filesystem_id: "computefilesystem-test" }}
      releases={{ candidate: "image@sha256:abc" }} enabled /></QueryClientProvider>);
    const button = screen.getByRole("button", { name: "Upgrade LibreChat" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "candidate" } });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button);
    await waitFor(() => expect(request).toHaveBeenCalledOnce());
    const body = JSON.parse(String(request.mock.calls[0][1]?.body));
    expect(body).toMatchObject({ kind: "upgrade", target_release: "candidate", expected_revision: 1, confirm_interruption: true });
    expect(body.idempotency_key).toBeTruthy();
  });
  it("sends customer settings as an object on the wire, not a JSON string", async () => {
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) =>
      new Response(JSON.stringify(testEnvelope(options?.method === "PUT" ? {} : { items: [] })),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    render(<QueryClientProvider client={new QueryClient()}><CustomerActions customer={fleet.items[0]} /></QueryClientProvider>);
    fireEvent.click(screen.getByText("Customer settings and instance registration"));
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Research customer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save customer" }));
    await waitFor(() => expect(request.mock.calls.some(call => call[1]?.method === "PUT")).toBe(true));
    const mutation = request.mock.calls.find(call => call[1]?.method === "PUT");
    expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
      display_name: "Research customer", purpose: "customer", archived: false,
    });
  });
});
