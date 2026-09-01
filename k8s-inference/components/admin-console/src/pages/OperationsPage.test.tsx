import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminEnvelope, AdminOperationList } from "../api/types";
import { adminApi } from "../api/client";
import { SessionContext } from "../auth/SessionContext";
import { testSession } from "../test/accessFixtures";
import { browserFixture } from "../test/browserFixtures";
import { OperationsPage } from "./OperationsPage";

afterEach(() => vi.restoreAllMocks());

function liveOperations(): AdminEnvelope<AdminOperationList> {
  return structuredClone(browserFixture("/admin/api/v1/operations")) as AdminEnvelope<AdminOperationList>;
}

function renderPage(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: testSession, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={[entry]}><OperationsPage /></MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("Operations page live contract", () => {
  it("uses bounded backend filters and follows opaque server pagination", async () => {
    const first = liveOperations();
    first.data.next_cursor = "next-opaque-cursor";
    const last = liveOperations();
    const operations = vi.spyOn(adminApi, "operations")
      .mockResolvedValueOnce(first)
      .mockResolvedValue(last);

    renderPage("/admin/operations?project=project-live&tenant=tenant-a&model=qwen3-8b&principal=agent-a&status=failed&error=runtime_failed&token=must-not-flow");

    expect(await screen.findByText("chat.completion")).toBeInTheDocument();
    await waitFor(() => expect(operations).toHaveBeenCalledOnce());
    const [context, filters] = operations.mock.calls[0];
    expect(context.toString()).toBe("project=project-live");
    expect(filters).toMatchObject({
      tenantId: "tenant-a",
      modelId: "qwen3-8b",
      principalId: "agent-a",
      status: "failed",
      errorCode: "runtime_failed",
      limit: 100,
    });

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(operations).toHaveBeenCalledTimes(2));
    expect(operations.mock.calls[1][1]).toMatchObject({ cursor: "next-opaque-cursor" });
  });

  it("ignores unrecognized operation states instead of sending them", async () => {
    const operations = vi.spyOn(adminApi, "operations").mockResolvedValue(liveOperations());
    renderPage("/admin/operations?status=definitely-not-a-state");

    expect(await screen.findByText("Invalid filter ignored")).toBeInTheDocument();
    await waitFor(() => expect(operations).toHaveBeenCalledOnce());
    expect(operations.mock.calls[0][1]?.status).toBeUndefined();
  });
});
