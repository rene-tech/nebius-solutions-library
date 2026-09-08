import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { capacitySummaryApi } from "../../api/capacitySummaryClient";
import type { CapacitySummary } from "../../api/capacitySummaryTypes";
import { testEnvelope } from "../../test/accessFixtures";
import { CapacityPage } from "./CapacityPage";

const observed = (value: number, unit = "gpus") => ({
  value,
  unit,
  state: "available" as const,
  source: "kubernetes",
  reason: null,
});
const unavailable = {
  value: null,
  unit: "gpus",
  state: "unavailable" as const,
  source: "kubernetes",
  reason: "Pod inventory incomplete",
};
const data: CapacitySummary = {
  observed_at: "2026-09-08T12:00:00Z",
  pools: [
    {
      pool_id: "h100-reserved",
      gpu_type: "H100",
      capacity_type: "regular",
      resource_name: "nvidia.com/gpu",
      nodes: 2,
      ready_nodes: 2,
      not_ready_nodes: 0,
      cordoned_nodes: 0,
      total_gpus: observed(16),
      allocated_gpus: observed(2),
      schedulable_free_gpus: observed(14),
      starting_workers: observed(0, "workers"),
      ready_workers: observed(2, "workers"),
      configured_min_nodes: 2,
      configured_max_nodes: 2,
      configured_additional_nodes: {
        ...observed(0, "nodes"),
        state: "estimated",
        reason: "Configuration only; provider supply not guaranteed.",
      },
    },
  ],
  gpu_utilization_percent: observed(10, "percent"),
  loaded_idle_gpus: unavailable,
  pending_customer_runs: observed(1, "runs"),
  oldest_pending_seconds: observed(90, "seconds"),
  pending_batch_shards: observed(4, "workloads"),
  starting_gpu_workers: observed(0, "workers"),
  notes: ["Node headroom is not a provider capacity guarantee."],
};
afterEach(() => vi.restoreAllMocks());

it("separates physical GPU allocation, measured activity and logical versus shard queues", async () => {
  vi.spyOn(capacitySummaryApi, "get").mockResolvedValue(testEnvelope(data));
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter>
        <CapacityPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const row = await screen.findByRole("row", { name: /h100-reserved/ });
  expect(within(row).getByText("14 gpus")).toBeInTheDocument();
  expect(screen.getByText("Pending customer runs")).toBeInTheDocument();
  expect(screen.getByText("Pending scheduler workloads")).toBeInTheDocument();
  expect(screen.getByTitle("Pod inventory incomplete")).toHaveTextContent("—");
  expect(
    screen.getByText("Node headroom is not a provider capacity guarantee."),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "Advanced diagnostics" }),
  ).toHaveAttribute(
    "href",
    expect.stringContaining("/admin/advanced/capacity"),
  );
});
