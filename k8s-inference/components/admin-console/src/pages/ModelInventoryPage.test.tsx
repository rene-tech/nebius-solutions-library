import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { adminApi } from "../api/client";
import type { ModelInventory, ModelInventoryItem } from "../api/inventoryTypes";
import type { AdminEnvelope } from "../api/types";
import { browserFixture } from "../test/browserFixtures";
import { ModelInventoryPage } from "./ModelInventoryPage";

afterEach(() => vi.restoreAllMocks());

it("shows undeployed models beside cold services and batch-ready profiles", async () => {
  const base: ModelInventoryItem = {
    model_id: "genmol", display_name: "GenMol", family: "bionemo",
    availability: "not-deployed", reason: "Catalog only; not configured.", configured: false,
    serving_enabled: false, serving_state: null, batch_readiness: null,
    ready_replicas: null, desired_replicas: null, runtime_image_digest: null,
    gpu_snapshot: "not-reported", snapshot_reason: "No qualified GPU snapshot.", management_path: null,
  };
  const fixture = browserFixture("/admin/api/v1/models") as AdminEnvelope<unknown>;
  const response: AdminEnvelope<ModelInventory> = {
    meta: fixture.meta,
    data: {
      items: [base,
        { ...base, model_id: "cosmos3-nano", display_name: "Cosmos", availability: "cold", configured: true, serving_enabled: true, serving_state: "cold", ready_replicas: 0, desired_replicas: 0 },
        { ...base, model_id: "mosaic", display_name: "Mosaic", availability: "batch-ready", configured: true, batch_readiness: "qualified" },
      ],
      total: 3, configured: 2, not_deployed: 1, scientific_projection_available: true,
    },
  };
  vi.spyOn(adminApi, "modelInventory").mockResolvedValue(response);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><ModelInventoryPage /></MemoryRouter>
  </QueryClientProvider>);
  const missing = await screen.findByRole("row", { name: /GenMol/ });
  expect(within(missing).getByText("not-deployed")).toBeInTheDocument();
  expect(screen.getByText("2 configured / 3 known · 1 not deployed")).toBeInTheDocument();
  expect(screen.getByRole("row", { name: /Cosmos/ })).toHaveTextContent("0 / 0");
  expect(screen.getByRole("row", { name: /Mosaic/ })).toHaveTextContent("batch-ready");
  fireEvent.click(screen.getByRole("checkbox", { name: "Not deployed only" }));
  expect(screen.queryByRole("row", { name: /Cosmos/ })).not.toBeInTheDocument();
  expect(screen.getByRole("row", { name: /GenMol/ })).toBeInTheDocument();
});
