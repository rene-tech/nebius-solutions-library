import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "../../api/client";
import { userApi } from "../../api/userClient";
import type { UserDetail, UserRow } from "../../api/userTypes";
import * as sessionModule from "../../auth/SessionContext";
import { testEnvelope, testKey, testSession } from "../../test/accessFixtures";
import { UserDetailPage } from "./UserDetailPage";
import { UsersPage } from "./UsersPage";

const missing = {
  value: null,
  unit: "gpu-seconds",
  state: "unavailable" as const,
  source: "postgres",
  reason: "No exclusive lifecycle coverage.",
};
const user: UserRow = {
  id: "68307168-35fc-5b11-b57d-3174e8158054",
  tenant_id: "tenant-a",
  principal_id: "research-owner",
  display_name: "Research Owner",
  kind: null,
  team: "Oncology",
  enabled: true,
  academic_eligible: null,
  app_ids: null,
  source: "existing-key-owner",
  created_at: "2026-09-08T09:00:00Z",
  updated_at: "2026-09-08T09:00:00Z",
  key_count: 2,
  active_key_count: 1,
  usage: {
    requests: 14,
    succeeded: 14,
    failed: 0,
    cancelled: 0,
    pending: 0,
    running: 0,
    scientific_requests: 14,
    last_request_at: "2026-09-08T09:30:00Z",
    scheduler_occupied_gpu_seconds: missing,
    active_gpu_seconds: missing,
    occupied_idle_gpu_seconds: missing,
    input_tokens: { ...missing, unit: "tokens" },
    output_tokens: { ...missing, unit: "tokens" },
    attribution: "All owner keys; accepted in selected window.",
    request_series: [
      { at: "2026-09-08T09:00:00Z", requests: 6 },
      { at: "2026-09-08T09:30:00Z", requests: 8 },
    ],
    bucket_seconds: 1800,
  },
};
const detail: UserDetail = {
  user,
  keys: [{ ...testKey, principal_id: user.principal_id }],
  apps: [
    {
      app_id: "41aa9a65-b0ce-478a-b937-0bb0940704d7",
      display_name: "Research app",
      public_model_id: "app-independent",
      academic_required: true,
    },
  ],
  policy_note: "User policy intersects key policy.",
};
const path = `/admin/users/${user.id}?from=2026-09-08T09%3A00%3A00Z&to=2026-09-08T10%3A00%3A00Z`;

afterEach(() => vi.restoreAllMocks());
function renderPage(isDetail = false, viewer = false) {
  vi.spyOn(sessionModule, "useSession").mockReturnValue({
    session: {
      ...testSession,
      principal: {
        ...testSession.principal,
        role: viewer ? "viewer" : "admin",
      },
    },
    status: "authenticated",
    signIn: vi.fn(),
    signOut: vi.fn(),
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof sessionModule.useSession>);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[isDetail ? path : "/admin/users?window=24"]}
      >
        <Routes>
          <Route path="/admin/users" element={<UsersPage />} />
          <Route path="/admin/users/:userId" element={<UserDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Inference Users", () => {
  it("lists inference owners and windowed history, not key issuers or console accounts", async () => {
    const list = vi
      .spyOn(userApi, "list")
      .mockResolvedValue(
        testEnvelope({
          items: [user],
          limit: 200,
          truncated: false,
          apps: detail.apps,
        }),
      );
    renderPage();
    expect(
      await screen.findByRole("link", { name: "Research Owner" }),
    ).toHaveAttribute(
      "href",
      expect.stringContaining(`/admin/users/${user.id}`),
    );
    expect(
      screen.getByText(/research-owner · not classified/),
    ).toBeInTheDocument();
    expect(screen.getByText("14 succeeded · 0 failed")).toBeInTheDocument();
    expect(
      screen.getByTitle("No exclusive lifecycle coverage."),
    ).toHaveTextContent("—");
    const query = list.mock.calls[0][0];
    expect(Date.parse(query.get("to")!) - Date.parse(query.get("from")!)).toBe(
      24 * 3600_000,
    );
  });

  it("saves academic and independent app settings without inventing legacy eligibility", async () => {
    vi.spyOn(userApi, "detail").mockResolvedValue(testEnvelope(detail));
    const update = vi
      .spyOn(userApi, "update")
      .mockResolvedValue(testEnvelope(user));
    renderPage(true);
    fireEvent.click(
      await screen.findByRole("button", { name: "User settings" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("Academic affiliation (informational)")).toHaveValue(
      "inherit",
    );
    fireEvent.change(within(dialog).getByLabelText("Academic affiliation (informational)"), {
      target: { value: "false" },
    });
    fireEvent.click(
      within(dialog).getByLabelText("Use existing API key app permissions"),
    );
    fireEvent.click(
      within(dialog).getByLabelText(/Research app · app-independent/),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Save user" }));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(
        user.id,
        expect.objectContaining({
          academic_eligible: false,
          app_ids: [detail.apps[0].app_id],
          kind: null,
        }),
        expect.any(URLSearchParams),
      ),
    );
  });

  it("creates a key for the exact owner and keeps the one-time secret out of storage", async () => {
    vi.spyOn(userApi, "detail").mockResolvedValue(testEnvelope(detail));
    const create = vi
      .spyOn(userApi, "createKey")
      .mockResolvedValue(
        testEnvelope({ key: testKey, secret: "test-one-time-value" }),
      );
    renderPage(true);
    fireEvent.click(
      await screen.findByRole("button", { name: "Create API key" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("Principal")).toHaveValue(
      user.principal_id,
    );
    fireEvent.change(within(dialog).getByLabelText("Key name"), {
      target: { value: "Customer key" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create key" }));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(
        user.id,
        expect.objectContaining({
          principal_id: user.principal_id,
          tenant_id: "tenant-a",
        }),
        expect.any(URLSearchParams),
      ),
    );
    expect(
      await screen.findByDisplayValue("test-one-time-value"),
    ).toBeInTheDocument();
    expect(localStorage.getItem("test-one-time-value")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "I have stored it" }));
    expect(
      screen.queryByDisplayValue("test-one-time-value"),
    ).not.toBeInTheDocument();
  });

  it("retains real chart values and hides mutation controls from viewers", async () => {
    vi.spyOn(userApi, "detail").mockResolvedValue(testEnvelope(detail));
    const update = vi.spyOn(adminApi, "updateKey");
    renderPage(true, true);
    expect(
      await screen.findByRole("img", { name: /Logical requests over time/ }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("8 requests").length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "User settings" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Create API key" }),
    ).not.toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });
});
