import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../../api/client";
import type { ScientificRunDetail } from "../../api/scientificTypes";
import type { AdminEnvelope } from "../../api/types";
import { browserFixture } from "../../test/browserFixtures";
import { ScientificRunDetailPage } from "./ScientificRunDetailPage";

afterEach(() => vi.restoreAllMocks());

function detailFixture(): AdminEnvelope<ScientificRunDetail> {
  return structuredClone(browserFixture("/admin/api/v1/scientific-runs/run-rfdiffusion-0001")) as AdminEnvelope<ScientificRunDetail>;
}

function renderPage(
  response: () => Promise<AdminEnvelope<ScientificRunDetail>> = () => Promise.resolve(detailFixture()),
) {
  vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(
    structuredClone(browserFixture("/admin/api/v1/scientific-capabilities")) as never,
  );
  vi.spyOn(adminApi, "scientificRun").mockImplementation(response);
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
    const admittedAttempt = within(gpuStage).getByRole("row", { name: /attempt-diffuse-2.*succeeded/ });
    expect(admittedAttempt).toHaveTextContent("Pool h100-capacity-block");
    expect(admittedAttempt).toHaveTextContent("flavor inference-h100-reserved-8x");
    expect(admittedAttempt).toHaveTextContent("Resource nvidia.com/gpu");

    expect(screen.getByRole("row", { name: /candidate-backbones.tar.zst.*output.*available/ })).toHaveTextContent("measured");
    expect(screen.getByRole("heading", { name: "Errors and retries" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Cancellation" })).toBeInTheDocument();
    expect(screen.getByText("This admin projection is read-only; this build does not publish a scientific cancellation command.")).toBeInTheDocument();
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

  it("renders loading and bounded endpoint errors", async () => {
    const pending = new Promise<AdminEnvelope<ScientificRunDetail>>(() => undefined);
    const first = renderPage(() => pending);
    expect(await screen.findByText("Loading scientific run detail…")).toBeInTheDocument();
    first.unmount();
    vi.restoreAllMocks();

    renderPage(() => Promise.reject(new AdminApiError(
      "Scientific controller reporting is unavailable.",
      503,
      "request-science-detail",
      "scientific_controller_unavailable",
    )));
    expect(await screen.findByRole("alert")).toHaveTextContent("Scientific controller reporting is unavailable.");
    expect(screen.getByRole("alert")).toHaveTextContent("request-science-detail");
  });

  it("does not render absent placement evidence as zero GPUs or CPU", async () => {
    const detail = detailFixture();
    detail.data.run.gpu_accounting.gpu_count = null;
    detail.data.stages[1].attempts[0].gpu_count = null;
    detail.data.stages[1].attempts[0].pod_count = null;
    detail.data.stages[1].attempts[0].node_count = null;
    renderPage(() => Promise.resolve(detail));

    expect(await screen.findByText(/GPU count unavailable/, { selector: ".metric-card__detail" })).toBeInTheDocument();
    const attempt = await screen.findByRole("row", { name: /attempt-diffuse-1/ });
    expect(attempt).toHaveTextContent("GPU count unavailable");
    expect(attempt).toHaveTextContent("pod count unavailable");
    expect(attempt).not.toHaveTextContent("CPU");
  });
});
