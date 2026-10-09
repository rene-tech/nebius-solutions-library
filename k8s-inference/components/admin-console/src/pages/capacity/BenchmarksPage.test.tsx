import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { testEnvelope } from "../../test/accessFixtures";
import { BenchmarksPage } from "./BenchmarksPage";

afterEach(() => vi.restoreAllMocks());

function renderPage() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><BenchmarksPage /></MemoryRouter>
  </QueryClientProvider>);
}

it("shows an explicit empty state without invented timings", async () => {
  vi.spyOn(client, "envelopeRequest").mockResolvedValue(testEnvelope({ items: [] }));
  renderPage();
  expect(await screen.findByText("No benchmark campaigns have been submitted.")).toBeInTheDocument();
  expect(screen.queryByText("0.00 s")).not.toBeInTheDocument();
});

it("keeps unsupported trials in the denominator and unknown measurements distinct from zero", async () => {
  const unmeasured = { count: 0, median: null, min: null, max: null };
  vi.spyOn(client, "envelopeRequest").mockImplementation(async (path) => testEnvelope(
    path.endsWith("/campaigns") ? { items: [{ id: "one", name: "fleet", total: 6, succeeded: 3,
      running: 0, queued: 0, failed: 0, unsupported: 3, capacity_unavailable: 0 }] } : { profiles: [{
      model_id: "boltz2", workload_class: "short", cache_condition: "uncontrolled", hardware: null,
      valid_samples: 3, placement_evidence: "insufficient", metrics: {
        elapsed_seconds: { count: 3, median: 12, min: 10, max: 15 }, queue_seconds: unmeasured,
        startup_seconds: unmeasured, execution_seconds: unmeasured,
      }, outcomes: { succeeded: 3 },
    }] },
  ));
  renderPage();
  expect(await screen.findByText("12.00 s")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("3/6 passed");
  expect(screen.getByRole("status")).toHaveTextContent("3 unsupported");
  expect(screen.getAllByTitle("No measurement was recorded")).toHaveLength(3);
});
