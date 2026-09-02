import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi } from "../../api/client";
import { browserFixture } from "../../test/browserFixtures";
import { ScientificRunDetailPage } from "./ScientificRunDetailPage";

afterEach(() => vi.restoreAllMocks());

function renderPage() {
  vi.spyOn(adminApi, "scientificRun").mockResolvedValue(structuredClone(browserFixture("/admin/api/v1/scientific-runs/run-rfdiffusion-0001")) as never);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/admin/scientific-runs/run-rfdiffusion-0001?project=p1"]}>
        <Routes><Route path="/admin/scientific-runs/:runId" element={<main><ScientificRunDetailPage /></main>} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("scientific run detail", () => {
  it("renders lifecycle, DAG attempts, artifacts, retry, cancellation, and correlated signals", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "CD8 binder backbone screen" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Phase durations" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "GPU idle by cause" })).toBeInTheDocument();
    expect(screen.getByText("Reconciliation", { exact: false })).toHaveTextContent("0 GPU-s measured");

    const gpuStage = screen.getByRole("heading", { name: "Diffuse candidate backbones" }).closest("li");
    expect(gpuStage).not.toBeNull();
    if (!gpuStage) throw new Error("GPU stage container is missing");
    expect(within(gpuStage).getByRole("row", { name: /attempt-diffuse-1.*preempted.*PREEMPTED/ })).toHaveTextContent("retryable");
    expect(within(gpuStage).getByRole("row", { name: /attempt-diffuse-2.*succeeded/ })).toBeInTheDocument();

    expect(screen.getByRole("row", { name: /candidate-backbones.tar.zst.*output.*available/ })).toHaveTextContent("measured");
    expect(screen.getByRole("heading", { name: "Errors and retries" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cancellation" })).toBeInTheDocument();
    expect(screen.getByText("This surface is read-only until the scientific operation API exposes an authorized cancellation command.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Request trace/ })).toHaveAttribute("href", "/admin/observability?operation_id=run-rfdiffusion-0001&signal=trace");
    expect(screen.getByRole("link", { name: /Correlated logs/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /GPU and queue metrics/ })).toBeInTheDocument();
  });

  it("has no automated accessibility violations", async () => {
    const { container } = renderPage();
    await screen.findByRole("heading", { name: "CD8 binder backbone screen" });
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
