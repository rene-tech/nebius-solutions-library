import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, AdminApiError } from "../../api/client";
import type { OperatorSession } from "../../api/accessTypes";
import type { ScientificCapabilities, ScientificRunDetail } from "../../api/scientificTypes";
import type { AdminEnvelope } from "../../api/types";
import { SessionContext } from "../../auth/SessionContext";
import { tenantPrincipal, testSession } from "../../test/accessFixtures";
import { browserFixture } from "../../test/browserFixtures";
import { ScientificRunDetailPage } from "./ScientificRunDetailPage";

afterEach(() => vi.restoreAllMocks());

function detailFixture(): AdminEnvelope<ScientificRunDetail> {
  return structuredClone(browserFixture("/admin/api/v1/scientific-runs/run-rfdiffusion-0001")) as AdminEnvelope<ScientificRunDetail>;
}

function capabilitiesFixture(): AdminEnvelope<ScientificCapabilities> {
  return structuredClone(browserFixture("/admin/api/v1/scientific-capabilities")) as AdminEnvelope<ScientificCapabilities>;
}

/** A queued run the operator may still cancel, unlike the completed fixture run. */
function cancellableDetail(): AdminEnvelope<ScientificRunDetail> {
  const detail = detailFixture();
  detail.data.run.status = "running";
  detail.data.run.cancellation = {
    state: "not-requested",
    requested_at: null,
    requested_by: null,
    reason: null,
    mode: "terminate-attempt",
    grace_seconds: null,
    can_cancel: true,
  };
  return detail;
}

function renderPage(
  response: () => Promise<AdminEnvelope<ScientificRunDetail>> = () => Promise.resolve(detailFixture()),
  options: { session?: OperatorSession; capabilities?: AdminEnvelope<ScientificCapabilities> } = {},
) {
  vi.spyOn(adminApi, "scientificCapabilities").mockResolvedValue(options.capabilities ?? capabilitiesFixture());
  vi.spyOn(adminApi, "scientificRun").mockImplementation(response);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: options.session ?? testSession, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={["/admin/scientific-runs/run-rfdiffusion-0001?project=p1"]}>
          <Routes><Route path="/admin/scientific-runs/:runId" element={<main><ScientificRunDetailPage /></main>} /></Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

describe("scientific run detail", () => {
  it("offers the authorized binary artifact download without exposing storage handles", async () => {
    const detail = detailFixture();
    const artifact = detail.data.artifacts.find((item) => item.role === "output");
    if (!artifact) throw new Error("Output artifact fixture is missing");
    artifact.download = {
      available: true,
      href: `/admin/api/v1/scientific-runs/${detail.data.run.id}/artifacts/${artifact.artifact_id}/content`,
      reason: null,
    };
    renderPage(() => Promise.resolve(detail));
    const row = await screen.findByRole("row", { name: /candidate-backbones.tar.zst.*output.*available/ });
    const download = within(row).getByRole("link", { name: "Download artifact" });
    expect(download).toHaveAttribute("href", artifact.download.href);
    expect(download).toHaveAttribute("download", artifact.name);
    expect(download.getAttribute("href")).not.toMatch(/token=|s3:\/\/|storage_handle/);
  });

  it("shows actionable pending placement and mixed-shard admission without claiming compute", async () => {
    const detail = cancellableDetail();
    detail.data.run.status = "admitted";
    detail.data.run.queue.shard_counts = { running: 2, pending: 1 };
    detail.data.run.queue.admission_reason = "Current diffuse shards: 2 running, 1 pending. Pending shards await their own admission.";
    const attempt = detail.data.stages[1].attempts[0];
    attempt.status = "queued";
    attempt.phase = "node_pending";
    attempt.phase_reason = "Waiting for a node: insufficient CPU including sidecars in the selected pool.";
    attempt.error = null;
    renderPage(() => Promise.resolve(detail));
    const notice = await screen.findByRole("region", { name: "Placement progress" });
    expect(notice).toHaveTextContent("insufficient CPU including sidecars");
    expect(notice).toHaveTextContent("Admission does not mean GPU computation has started");
    const row = screen.getByRole("row", { name: /attempt-diffuse-1/ });
    expect(row).toHaveTextContent("queued");
    expect(row).toHaveTextContent("node pending");
    expect(row).toHaveTextContent("No error");
    expect(screen.getByText(/Current diffuse shards: 2 running, 1 pending/)).toBeInTheDocument();
  });

  it("automatically refreshes an active run to its durable terminal result", async () => {
    let calls = 0;
    renderPage(() => Promise.resolve(++calls === 1 ? cancellableDetail() : detailFixture()));
    expect(await screen.findByRole("button", { name: "Request cancellation" })).toBeInTheDocument();
    await waitFor(() => expect(adminApi.scientificRun).toHaveBeenCalledTimes(2), { timeout: 6500 });
    expect(await screen.findByText("This run is terminal and can no longer be cancelled.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).not.toBeInTheDocument();
  }, 8000);

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
    expect(screen.getByText("This run is terminal and can no longer be cancelled.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).not.toBeInTheDocument();
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

  it("lets an operator confirm a cancel request and shows the refreshed durable state", async () => {
    const refreshed = cancellableDetail();
    refreshed.data.run.status = "cancelling";
    refreshed.data.run.cancellation = {
      ...refreshed.data.run.cancellation,
      state: "requested",
      requested_by: "tenant-operator",
      reason: "Cancellation was recorded in the append-only audit ledger.",
      can_cancel: false,
    };
    const cancel = vi.spyOn(adminApi, "cancelScientificRun").mockResolvedValue(refreshed);
    renderPage(() => Promise.resolve(cancellableDetail()), { session: { ...testSession, principal: tenantPrincipal } });

    fireEvent.click(await screen.findByRole("button", { name: "Request cancellation" }));
    expect(screen.getByRole("button", { name: "Keep running" })).toBeInTheDocument();
    expect(cancel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }));

    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(cancel.mock.calls[0][0]).toBe("run-rfdiffusion-0001");
    expect(cancel.mock.calls[0][1].get("project")).toBe("p1");
    const notice = await screen.findByText("Cancellation requested.");
    expect(notice.closest("[role=status]")).toHaveTextContent("terminates the active attempt");
    expect(screen.getByText("tenant-operator")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).not.toBeInTheDocument();
  });

  it("keeps the run running when the cancel request is rejected and explains the failure", async () => {
    vi.spyOn(adminApi, "cancelScientificRun").mockRejectedValue(new AdminApiError(
      "scientific run already reached a terminal status",
      409,
      "request-cancel-409",
      "scientific_run_terminal",
    ));
    renderPage(() => Promise.resolve(cancellableDetail()));

    fireEvent.click(await screen.findByRole("button", { name: "Request cancellation" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Cancellation was not recorded.");
    expect(alert).toHaveTextContent("request-cancel-409");
    expect(screen.getByRole("button", { name: "Confirm cancellation" })).toBeInTheDocument();
  });

  it("fails closed for viewers and for builds without a cancellation writer", async () => {
    const viewer = { ...testSession, principal: { ...tenantPrincipal, role: "viewer" as const } };
    const first = renderPage(() => Promise.resolve(cancellableDetail()), { session: viewer });
    expect(await screen.findByText("Operator role required to request cancellation.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).not.toBeInTheDocument();
    first.unmount();
    vi.restoreAllMocks();

    const capabilities = capabilitiesFixture();
    capabilities.data.run_control = { available: false, reason: "no scientific batch cancellation writer is bound to this build" };
    renderPage(() => Promise.resolve(cancellableDetail()), { capabilities });
    expect(await screen.findByText("no scientific batch cancellation writer is bound to this build")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).not.toBeInTheDocument();
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
