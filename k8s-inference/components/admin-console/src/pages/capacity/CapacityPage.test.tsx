import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "../../api/client";
import { capacityFixture } from "../../test/capacityObservabilityFixtures";
import { testEnvelope } from "../../test/accessFixtures";
import { CapacityPage } from "./CapacityPage";

afterEach(() => vi.restoreAllMocks());

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={["/admin/capacity"]}><CapacityPage /></MemoryRouter></QueryClientProvider>);
}

describe("Capacity page", () => {
  it("renders heterogeneous pools and preserves estimated zero and unavailable health", async () => {
    vi.spyOn(adminApi, "capacity").mockResolvedValue(testEnvelope(capacityFixture));
    renderPage();

    const burstRow = await screen.findByRole("row", { name: /burst-blackwell/ });
    expect(within(burstRow).getByText("accelerator-288gb")).toBeInTheDocument();
    expect(within(burstRow).getByText("preemptible")).toBeInTheDocument();
    expect(burstRow.querySelector(".measurement--estimated")).toHaveTextContent("0 gpus estimated");
    expect(within(burstRow).getByTitle("explicit GPU health evidence is unavailable")).toHaveTextContent("—");

    expect(screen.getAllByText("regular").length).toBeGreaterThan(0);
    expect(screen.getAllByText("unknown").length).toBeGreaterThan(0);
    expect(screen.getByText("accelerator.example/gpu")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Provider-neutral GPU node pools" })).toHaveAttribute("tabindex", "0");
  });

  it("shows verified empty queue/autoscaler states as zero-object results", async () => {
    const empty = structuredClone(capacityFixture);
    empty.kueue.cluster_queues = [];
    empty.kueue.local_queues = [];
    empty.kueue.workloads = [];
    vi.spyOn(adminApi, "capacity").mockResolvedValue(testEnvelope(empty));
    renderPage();

    expect(await screen.findByText("0 cluster queues observed.")).toBeInTheDocument();
    expect(screen.getByText("0 local queues observed.")).toBeInTheDocument();
    expect(screen.getByText("0 pending or recent workloads observed.")).toBeInTheDocument();
    expect(screen.getByText("0 KEDA ScaledObjects observed.")).toBeInTheDocument();
  });

  it("does not turn an unavailable inventory into an observed zero", async () => {
    const unavailable = structuredClone(capacityFixture);
    unavailable.node_pools = { state: "unavailable", reason: "Kubernetes node inventory is unavailable", items: [] };
    vi.spyOn(adminApi, "capacity").mockResolvedValue(testEnvelope(unavailable));
    renderPage();

    expect(await screen.findByText("Projection unavailable")).toBeInTheDocument();
    expect(screen.getByText("Kubernetes node inventory is unavailable")).toBeInTheDocument();
    expect(screen.queryByText("0 node pools observed in this context.")).not.toBeInTheDocument();
  });
});
