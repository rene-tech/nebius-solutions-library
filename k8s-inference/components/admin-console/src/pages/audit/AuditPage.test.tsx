import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "../../api/client";
import { SessionContext } from "../../auth/SessionContext";
import { testAudit, testEnvelope, testSession } from "../../test/accessFixtures";
import { AuditPage } from "./AuditPage";

afterEach(() => vi.restoreAllMocks());

function renderPage(entry = "/admin/audit") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: testSession, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={[entry]}><AuditPage /></MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Audit page", () => {
  it("renders append-only lifecycle fields and filters locally without sending free-form text", async () => {
    const audit = vi.spyOn(adminApi, "audit").mockResolvedValue(testEnvelope({ items: [testAudit] }));
    renderPage();
    expect(await screen.findByText("token.issue")).toBeInTheDocument();
    expect(screen.getByText("admin@example.test")).toBeInTheDocument();
    expect(screen.getByText(/principal_id: agent-a/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "no-match" } });
    expect(screen.queryByText("token.issue")).not.toBeInTheDocument();
    expect(audit).toHaveBeenCalledWith(undefined, 200, expect.any(AbortSignal));
    expect(audit).toHaveBeenCalledTimes(1);
  });

  it("falls back to all outcomes when an unrecognized URL filter is supplied", async () => {
    vi.spyOn(adminApi, "audit").mockResolvedValue(testEnvelope({ items: [testAudit] }));
    renderPage("/admin/audit?outcome=untrusted-value");
    expect(await screen.findByText("token.issue")).toBeInTheDocument();
    expect(screen.getByLabelText("Outcome")).toHaveValue("all");
  });
});
