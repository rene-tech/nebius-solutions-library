import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminEnvelope, AdminModelList } from "../api/types";
import { adminApi } from "../api/client";
import { browserFixture } from "../test/browserFixtures";
import { ModelsPage } from "./ModelsPage";

afterEach(() => vi.restoreAllMocks());

function liveModels(): AdminEnvelope<AdminModelList> {
  return structuredClone(browserFixture("/admin/api/v1/models")) as AdminEnvelope<AdminModelList>;
}

function renderPage(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}><ModelsPage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Models page live contract", () => {
  it("sends validated server-side filters and visibly explains degraded unknown state", async () => {
    const response = liveModels();
    const model = response.data.items[0];
    response.data = { items: [{
      ...model,
      runtime: {
        ...model.runtime,
        state: "unknown",
        reason: "a required observed-state source is unavailable or stale",
        desired_replicas: null,
        ready_replicas: null,
        observed_at: null,
      },
    }], total: 1 };
    response.meta.sources = [{
      id: "kubernetes",
      state: "unavailable",
      observed_at: null,
      age_seconds: null,
      reason: "Kubernetes API request timed out",
    }];
    const models = vi.spyOn(adminApi, "models").mockResolvedValue(response);

    renderPage("/admin/models?project=project-live&state=unknown&search=Qwen&token=must-not-flow");

    const row = await screen.findByRole("row", { name: /Qwen3 8B/ });
    expect(within(row).getByText("a required observed-state source is unavailable or stale")).toBeInTheDocument();
    expect(within(row).getByText(model.identity.support_state)).toBeInTheDocument();
    expect(screen.getByText("Partial data")).toBeInTheDocument();
    await waitFor(() => expect(models).toHaveBeenCalled());
    const [context, filters] = models.mock.calls[0];
    expect(context.toString()).toBe("project=project-live");
    expect(filters).toEqual({ limit: 256, search: "Qwen", state: "unknown" });
  });

  it("keeps catalog support and cluster enablement distinct from runtime state", async () => {
    const response = liveModels();
    const model = response.data.items[0];
    response.data = { items: [{
      ...model,
      identity: { ...model.identity, support_state: "catalog-only", enabled: false },
      runtime: { ...model.runtime, state: "unsupported", reason: "catalog compatibility is not qualified" },
    }], total: 1 };
    vi.spyOn(adminApi, "models").mockResolvedValue(response);

    renderPage("/admin/models");

    const row = await screen.findByRole("row", { name: /Qwen3 8B/ });
    expect(within(row).getByText("unsupported")).toBeInTheDocument();
    expect(within(row).getByText("catalog-only")).toBeInTheDocument();
    expect(within(row).getByText("Disabled in this cluster")).toBeInTheDocument();
  });
});
