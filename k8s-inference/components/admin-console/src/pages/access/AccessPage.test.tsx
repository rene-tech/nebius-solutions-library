import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../../api/client";
import type { OperatorRole } from "../../api/accessTypes";
import { SessionContext } from "../../auth/SessionContext";
import { tenantPrincipal, testEnvelope, testKey, testPrincipal, testSession } from "../../test/accessFixtures";
import { AccessPage } from "./AccessPage";

afterEach(() => vi.restoreAllMocks());

function renderPage(role: OperatorRole = "admin") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const principal = { ...testPrincipal, role };
  render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: { ...testSession, principal }, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={["/admin/access"]}>
          <Routes>
            <Route path="/admin/access" element={<AccessPage />} />
            <Route path="/admin/audit" element={<p>Audit destination</p>} />
          </Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Access page", () => {
  it("gives access form controls concise labels independent of options and help text", async () => {
    vi.spyOn(adminApi, "principals").mockResolvedValue(testEnvelope({ items: [tenantPrincipal] }));
    vi.spyOn(adminApi, "keys").mockResolvedValue(testEnvelope({ items: [testKey] }));
    renderPage();
    await screen.findByText("Agent A key");

    fireEvent.click(screen.getByRole("button", { name: "Add principal" }));
    expect(screen.getByLabelText("Kind", { exact: true })).toHaveValue("human");
    expect(screen.getByLabelText("Role", { exact: true })).toHaveValue("viewer");
    const subjectPattern = new RegExp(`^(?:${screen.getByLabelText("Subject").getAttribute("pattern")})$`, "v");
    const tenantPattern = new RegExp(`^(?:${within(screen.getByRole("dialog")).getByLabelText("Tenant").getAttribute("pattern")})$`, "v");
    expect(subjectPattern.test("customer-a:rene@example.org/team")).toBe(true);
    expect(subjectPattern.test("customer with spaces")).toBe(false);
    expect(tenantPattern.test("tenant-academic")).toBe(true);
    expect(tenantPattern.test("tenant/academic")).toBe(false);
    fireEvent.change(screen.getByLabelText("Role", { exact: true }), { target: { value: "operator" } });
    expect(screen.getByRole("combobox", { name: "Role" })).toHaveValue("operator");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    expect(screen.getByLabelText("Role", { exact: true })).toHaveValue(tenantPrincipal.role);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    fireEvent.click(screen.getByRole("button", { name: "Create API key" }));
    expect(screen.getByLabelText("Principal", { exact: true })).toHaveValue(tenantPrincipal.subject);
    expect(screen.getByLabelText("Allowed models", { exact: true })).toHaveValue("*");
    expect(screen.getByRole("textbox", { name: "Allowed models" }))
      .toHaveAccessibleDescription("Comma-separated model IDs, or * for all models.");
  });

  it("reveals a newly issued key once and drops it when navigation unmounts the page", async () => {
    const transient = "issued-test-" + "k".repeat(48);
    vi.spyOn(adminApi, "principals").mockResolvedValue(testEnvelope({ items: [tenantPrincipal] }));
    vi.spyOn(adminApi, "keys").mockResolvedValue(testEnvelope({ items: [testKey] }));
    const issue = vi.spyOn(adminApi, "issueKey").mockResolvedValue(testEnvelope({ key: testKey, secret: transient }));
    const storageSpy = vi.spyOn(Storage.prototype, "setItem");
    const consoleSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);
    renderPage();

    await screen.findByText("Agent A key");
    fireEvent.click(screen.getByRole("button", { name: "Create API key" }));
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "new agent key" } });
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));

    await waitFor(() => expect(issue).toHaveBeenCalledOnce());
    expect(await screen.findByDisplayValue(transient)).toBeInTheDocument();
    expect(storageSpy).not.toHaveBeenCalled();
    expect(window.sessionStorage.length).toBe(0);
    expect(window.localStorage.length).toBe(0);
    expect(window.location.href).not.toContain(transient);
    expect(consoleSpy).not.toHaveBeenCalledWith(expect.stringContaining(transient));

    fireEvent.click(screen.getByRole("link", { name: "View access audit" }));
    expect(await screen.findByText("Audit destination")).toBeInTheDocument();
    expect(screen.queryByDisplayValue(transient)).not.toBeInTheDocument();
  });

  it("removes mutation controls for a viewer while preserving accounting visibility", async () => {
    vi.spyOn(adminApi, "principals").mockResolvedValue(testEnvelope({ items: [tenantPrincipal] }));
    vi.spyOn(adminApi, "keys").mockResolvedValue(testEnvelope({ items: [testKey] }));
    renderPage("viewer");
    expect(await screen.findByText("Agent A key")).toBeInTheDocument();
    expect(screen.getByText("4 operations")).toBeInTheDocument();
    expect(screen.getByText(/Viewer access is read-only/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create API key" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add principal" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rotate" })).not.toBeInTheDocument();
  });

  it("keeps principal data visible when the key projection fails", async () => {
    vi.spyOn(adminApi, "principals").mockResolvedValue(testEnvelope({ items: [tenantPrincipal] }));
    vi.spyOn(adminApi, "keys").mockRejectedValue(new AdminApiError("key adapter unavailable", 503, "request-test"));
    renderPage();
    expect(await screen.findByText("Agent A")).toBeInTheDocument();
    expect(await screen.findByText("This view is unavailable")).toBeInTheDocument();
    expect(screen.getByText("key adapter unavailable")).toBeInTheDocument();
    expect(screen.getByText("Request request-test")).toBeInTheDocument();
  });

  it("does not report zero GPU usage when any visible key lacks accounting", async () => {
    const unavailableKey = structuredClone(testKey);
    unavailableKey.usage.estimated_gpu_seconds = {
      value: null,
      unit: "gpu-seconds",
      state: "unavailable",
      reason: "admission accounting is unavailable",
    };
    vi.spyOn(adminApi, "principals").mockResolvedValue(testEnvelope({ items: [tenantPrincipal] }));
    vi.spyOn(adminApi, "keys").mockResolvedValue(testEnvelope({ items: [unavailableKey] }));
    renderPage();

    await screen.findByText("Agent A key");
    const gpuCard = screen.getByText("GPU usage").closest("div");
    expect(gpuCard).toHaveTextContent("—");
    expect(gpuCard).toHaveTextContent("GPU accounting is unavailable");
    expect(screen.getByTitle("admission accounting is unavailable")).toHaveTextContent("—");
  });
});
