import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../api/client";
import { SessionContext } from "../auth/SessionContext";
import { testSession } from "../test/accessFixtures";
import { AppShell } from "./AppShell";
import { browserFixture } from "../test/browserFixtures";

afterEach(() => vi.restoreAllMocks());

function renderShell(entry = "/admin") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider
        value={{
          session: testSession,
          logout: async () => undefined,
          loggingOut: false,
          logoutError: null,
        }}
      >
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/admin" element={<AppShell />}>
              <Route index element={<p>Overview content</p>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("application shell context state", () => {
  it("never labels a failed context request as live and exposes a correlated retry", async () => {
    vi.spyOn(adminApi, "scientificCapabilities").mockRejectedValue(
      new AdminApiError(
        "scientific capability endpoint is absent",
        404,
        "request-capability",
        "not_found",
      ),
    );
    vi.spyOn(adminApi, "context").mockRejectedValue(
      new AdminApiError(
        "no server-authorized cluster context is configured",
        503,
        "request-context",
        "invalid_context",
      ),
    );
    renderShell();

    expect(
      await screen.findByText("Cluster context is unavailable."),
    ).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Live")).not.toBeInTheDocument();
    expect(screen.getByText("Request request-context")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Overview content")).toBeInTheDocument();
  });

  it("hides unavailable scientific navigation without affecting the rest of the portal", async () => {
    vi.spyOn(adminApi, "context").mockResolvedValue(
      structuredClone(browserFixture("/admin/api/v1/context")) as never,
    );
    vi.spyOn(adminApi, "scientificCapabilities").mockRejectedValue(
      new AdminApiError(
        "scientific capability endpoint is absent",
        404,
        "request-capability",
        "not_found",
      ),
    );
    renderShell();

    expect(await screen.findByText("Overview content")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Scientific/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Apps" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Users" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Capacity" })).toBeInTheDocument();
  });

  it("names the Apps landing page when the console is served with a trailing slash", async () => {
    vi.spyOn(adminApi, "context").mockResolvedValue(
      structuredClone(browserFixture("/admin/api/v1/context")) as never,
    );
    vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(
      structuredClone(
        browserFixture("/admin/api/v1/scientific-capabilities"),
      ) as never,
    );
    renderShell("/admin/");

    expect(await screen.findByText("Overview content")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Apps");
    expect(screen.getByText("Nebius / Apps")).toBeInTheDocument();
    expect(
      await screen.findByText(/^Cluster context checked /),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Updated /)).not.toBeInTheDocument();
  });
});
