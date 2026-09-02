import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../../api/client";
import { testEnvelope } from "../../test/accessFixtures";
import { modelDeploymentRevisionFixture, modelDeploymentStatusFixture } from "../../test/modelDeploymentFixtures";
import { ModelDeploymentsPage } from "./ModelDeploymentsPage";

afterEach(() => vi.restoreAllMocks());

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={["/admin/model-deployments?namespace=fs2-models&tenant_id=tenant-fixture"]}><ModelDeploymentsPage /></MemoryRouter></QueryClientProvider>);
}

describe("ModelDeployment list", () => {
  it("keeps durable desired state distinct from the independently observed phase", async () => {
    vi.spyOn(adminApi, "modelDeployments").mockResolvedValue(testEnvelope({ items: [modelDeploymentRevisionFixture], next_after: null }));
    vi.spyOn(adminApi, "modelDeploymentStatus").mockResolvedValue(testEnvelope(modelDeploymentStatusFixture));
    renderPage();

    const row = await screen.findByRole("row", { name: /qwen-live/ });
    expect(await within(row).findByText("Ready")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    expect(within(row).getByText("0 / 4")).toBeInTheDocument();
    expect(within(row).getByText("Hot · already serving")).toBeInTheDocument();
    expect(within(row).getByText("Assigned L2 · ≤120 seconds")).toBeInTheDocument();
    expect(within(row).getByText("Qualified L2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Draft model deployment" })).toBeInTheDocument();
  });

  it("renders a missing read capability as unavailable rather than an empty fleet", async () => {
    vi.spyOn(adminApi, "modelDeployments").mockRejectedValue(new AdminApiError(
      "not found",
      404,
      "request-model-deployment",
      "not_found",
    ));
    renderPage();

    expect(await screen.findByText("Dynamic model configuration is unavailable")).toBeInTheDocument();
    expect(screen.getByText(/No empty or zero state is inferred/)).toBeInTheDocument();
    expect(screen.queryByText(/0 desired deployments/)).not.toBeInTheDocument();
  });
});
