import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../api/client";
import { SessionContext } from "../auth/SessionContext";
import { testSession } from "../test/accessFixtures";
import { AppShell } from "./AppShell";

afterEach(() => vi.restoreAllMocks());

function renderShell() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: testSession, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={["/admin"]}>
          <Routes><Route path="/admin" element={<AppShell />}><Route index element={<p>Overview content</p>} /></Route></Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("application shell context state", () => {
  it("never labels a failed context request as live and exposes a correlated retry", async () => {
    vi.spyOn(adminApi, "context").mockRejectedValue(new AdminApiError(
      "no server-authorized cluster context is configured",
      503,
      "request-context",
      "invalid_context",
    ));
    renderShell();

    expect(await screen.findByText("Cluster context is unavailable.")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Live")).not.toBeInTheDocument();
    expect(screen.getByText("Request request-context")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByText("Overview content")).toBeInTheDocument();
  });
});
