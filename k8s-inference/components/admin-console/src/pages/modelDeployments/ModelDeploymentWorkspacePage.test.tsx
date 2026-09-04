import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OperatorRole } from "../../api/accessTypes";
import { adminApi, AdminApiError } from "../../api/client";
import type { ModelDeploymentConfigurationOption } from "../../api/modelDeploymentTypes";
import { SessionContext } from "../../auth/SessionContext";
import { testEnvelope, testPrincipal, testSession } from "../../test/accessFixtures";
import {
  modelDeploymentAppliedFixture,
  modelDeploymentMutationCapabilitiesFixture,
  modelDeploymentPendingFixture,
  modelDeploymentPlanFixture,
  modelDeploymentRevisionFixture,
  modelDeploymentSpecFixture,
  modelDeploymentStatusFixture,
  modelDeploymentValidationFixture,
} from "../../test/modelDeploymentFixtures";
import { ModelDeploymentWorkspacePage } from "./ModelDeploymentWorkspacePage";

afterEach(() => vi.restoreAllMocks());

function renderPage(role: OperatorRole = "admin") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const session = { ...testSession, principal: { ...testPrincipal, role } };
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={["/admin/model-deployments/qwen-live?namespace=fs2-models&tenant_id=tenant-fixture"]}>
          <Routes><Route path="/admin/model-deployments/:deploymentName" element={<ModelDeploymentWorkspacePage />} /></Routes>
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

function renderCreatePage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SessionContext.Provider value={{ session: testSession, logout: async () => undefined, loggingOut: false, logoutError: null }}>
        <MemoryRouter initialEntries={["/admin/model-deployments/new?namespace=fs2-models&tenant_id=tenant-fixture"]}>
          <ModelDeploymentWorkspacePage create />
        </MemoryRouter>
      </SessionContext.Provider>
    </QueryClientProvider>,
  );
}

function mockReadSurface() {
  vi.spyOn(adminApi, "modelDeployment").mockResolvedValue(testEnvelope(modelDeploymentRevisionFixture));
  vi.spyOn(adminApi, "modelDeploymentStatus").mockResolvedValue(testEnvelope(modelDeploymentStatusFixture));
  vi.spyOn(adminApi, "modelDeploymentHistory").mockResolvedValue(testEnvelope({
    items: [
      modelDeploymentRevisionFixture,
      { ...modelDeploymentRevisionFixture, revision: 1, action: "create", previous_revision: null },
    ],
    next_before_revision: null,
  }));
  vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(testEnvelope(modelDeploymentMutationCapabilitiesFixture));
}

describe("ModelDeployment workspace", () => {
  it("requires an explicit qualified-model selection and seeds the exact server default", async () => {
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(testEnvelope(modelDeploymentMutationCapabilitiesFixture));
    renderCreatePage();

    expect(screen.getByRole("heading", { name: "Untitled model deployment" })).toBeInTheDocument();
    const model = await screen.findByRole("combobox", { name: "Qualified model" });
    await waitFor(() => expect(model).toBeEnabled());
    expect(within(model).getByRole("option", { name: "qwen3-8b" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Validate draft" })).toBeDisabled();
    expect(screen.getByLabelText("Model reference")).toBeDisabled();

    fireEvent.change(model, { target: { value: "qwen3-8b" } });

    expect(screen.getByRole("heading", { name: "qwen3-8b-live" })).toBeInTheDocument();
    expect(screen.getByLabelText("Namespace")).toHaveValue("fs2-models");
    expect(screen.getByLabelText("Model reference")).toHaveValue("qwen3-8b");
    expect(screen.getByLabelText("Tenant ID")).toHaveValue("tenant-fixture");
    expect(screen.getByLabelText("Runtime profile")).toHaveValue(modelDeploymentSpecFixture.runtime.profile);
    expect(screen.getByRole("checkbox", { name: "Use reserved-h100" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Use preemptible-h100" })).toBeChecked();
    expect(screen.getByLabelText("Hot floor")).toHaveValue(modelDeploymentSpecFixture.availability.minReplicas);
    expect(screen.getByLabelText("Fast-start mode")).toHaveValue("Fixed");
    expect(screen.getByLabelText("Fast-start mode")).toBeEnabled();
    expect(screen.getByLabelText("Fast-start level")).toHaveValue("L3");
    expect(screen.getByLabelText("Fast-start level")).toBeEnabled();
    fireEvent.click(screen.getByText("Operator mechanism details"));
    const mechanism = screen.getByLabelText("Cold-start mechanism");
    expect(mechanism).toBeEnabled();
    expect(within(mechanism).getAllByRole("option").map((option) => option.getAttribute("value"))).toEqual([
      "",
      "conventional",
      "regional-cache",
      "host-memory-residency",
    ]);
    expect(within(mechanism).getByRole("option", { name: "Default conventional loader" })).toBeInTheDocument();
    fireEvent.change(mechanism, { target: { value: "regional-cache" } });
    expect(screen.getByLabelText("Cache tier")).toHaveValue("SharedFilesystem");
    expect(screen.getByRole("button", { name: "Validate draft" })).toBeEnabled();
    expect(screen.getByText(/No observed state or history yet/)).toBeInTheDocument();
  });

  it("selects reserved hot and preemptible burst pools without collapsing the placement policy", async () => {
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(testEnvelope(modelDeploymentMutationCapabilitiesFixture));
    const plan = vi.spyOn(adminApi, "planModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPlanFixture));
    renderCreatePage();

    const model = await screen.findByRole("combobox", { name: "Qualified model" });
    await waitFor(() => expect(model).toBeEnabled());
    fireEvent.change(model, { target: { value: "qwen3-8b" } });

    const reserved = screen.getByRole("checkbox", { name: "Use reserved-h100" });
    const preemptible = screen.getByRole("checkbox", { name: "Use preemptible-h100" });
    expect(reserved).toBeChecked();
    expect(preemptible).toBeChecked();

    fireEvent.click(preemptible);
    expect(reserved).toBeChecked();
    expect(preemptible).not.toBeChecked();
    fireEvent.click(preemptible);
    expect(reserved).toBeChecked();
    expect(preemptible).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Preview render plan" }));

    await waitFor(() => expect(plan).toHaveBeenCalledWith(expect.objectContaining({
      spec: expect.objectContaining({
        placement: expect.objectContaining({ poolRefs: ["reserved-h100", "preemptible-h100"] }),
      }),
    })));
  });

  it("enforces the exact server-authoritative pool set and capacity for an explicit mechanism", async () => {
    const capabilities = structuredClone(modelDeploymentMutationCapabilitiesFixture);
    const option = capabilities.configuration_options[0]!;
    option.pool_choices.forEach((choice) => { choice.maximum_replicas = 2; });
    const hostMemory = option.fast_start_mechanism_choices.find(
      (choice) => choice.mechanism === "host-memory-residency",
    )!;
    hostMemory.pool_refs = ["reserved-h100"];
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(testEnvelope(capabilities));
    const plan = vi.spyOn(adminApi, "planModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPlanFixture));
    renderCreatePage();

    const model = await screen.findByRole("combobox", { name: "Qualified model" });
    await waitFor(() => expect(model).toBeEnabled());
    fireEvent.change(model, { target: { value: "qwen3-8b" } });
    fireEvent.click(screen.getByText("Operator mechanism details"));
    fireEvent.change(screen.getByLabelText("Cold-start mechanism"), {
      target: { value: "host-memory-residency" },
    });

    expect(screen.getByRole("checkbox", { name: "Use reserved-h100" })).toBeChecked();
    const incompatiblePool = screen.getByRole("checkbox", { name: "Use preemptible-h100" });
    expect(incompatiblePool).not.toBeChecked();
    expect(incompatiblePool).toBeDisabled();
    expect(screen.getByLabelText("Replica ceiling")).toHaveValue(2);

    fireEvent.click(screen.getByRole("button", { name: "Preview render plan" }));
    await waitFor(() => expect(plan).toHaveBeenCalledWith(expect.objectContaining({
      spec: expect.objectContaining({
        placement: expect.objectContaining({ poolRefs: ["reserved-h100"] }),
        availability: expect.objectContaining({ maxReplicas: 2 }),
        cache: expect.objectContaining({ mechanism: "host-memory-residency" }),
      }),
    })));
  });

  it("preserves operator policy while replacing qualified model material on an explicit switch", async () => {
    const qwen = modelDeploymentMutationCapabilitiesFixture.configuration_options[0]!;
    const cosmos: ModelDeploymentConfigurationOption = {
      ...structuredClone(qwen),
      model_ref: "cosmos3-nano",
      suggested_name: "cosmos3-nano-live",
      default_spec: {
        ...structuredClone(qwen.default_spec),
        modelRef: "cosmos3-nano",
        artifact: { revision: "cosmos-r1", manifestDigest: `sha256:${"1".repeat(64)}`, storageRef: null },
        runtime: {
          profile: "cosmos-runtime",
          image: `registry.example.invalid/cosmos@sha256:${"2".repeat(64)}`,
          templateRef: { name: "cosmos-template", digest: `sha256:${"3".repeat(64)}` },
        },
        placement: { poolRefs: ["preemptible-h100"], acceleratorsPerReplica: 2, topologyPolicy: "SingleNode" },
        availability: { ...qwen.default_spec.availability, minReplicas: 0, maxReplicas: 1 },
        cache: { tier: "Disabled", snapshotPreference: "Never", snapshotRef: null },
        exposure: { openAI: false, openAIAliases: [], mcp: false, mcpToolName: null },
        policy: { ...qwen.default_spec.policy, visibility: "Tenant", allowedPrincipalIds: [] },
      },
    };
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(testEnvelope({
      ...modelDeploymentMutationCapabilitiesFixture,
      configuration_options: [qwen, cosmos],
    }));
    vi.spyOn(adminApi, "planModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPlanFixture));
    renderCreatePage();

    const model = await screen.findByRole("combobox", { name: "Qualified model" });
    await waitFor(() => expect(model).toBeEnabled());
    fireEvent.change(model, { target: { value: qwen.model_ref } });
    fireEvent.change(screen.getByLabelText("Hot floor"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("Replica ceiling"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Priority class"), { target: { value: "standard" } });
    fireEvent.change(screen.getByLabelText("OpenAI aliases"), { target: { value: "operator-alias" } });
    fireEvent.change(screen.getByLabelText("MCP tool name"), { target: { value: "operator_tool" } });
    fireEvent.change(screen.getByLabelText("Visibility"), { target: { value: "Private" } });
    fireEvent.change(screen.getByLabelText("Allowed principals"), { target: { value: "operator-principal" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview render plan" }));
    expect(await screen.findByRole("heading", { name: "Render plan" })).toBeInTheDocument();

    fireEvent.change(model, { target: { value: cosmos.model_ref } });

    expect(screen.getByRole("heading", { name: "cosmos3-nano-live" })).toBeInTheDocument();
    expect(screen.getByLabelText("Model reference")).toHaveValue("cosmos3-nano");
    expect(screen.getByLabelText("Artifact revision")).toHaveValue("cosmos-r1");
    expect(screen.getByLabelText("Runtime profile")).toHaveValue("cosmos-runtime");
    expect(screen.getByRole("checkbox", { name: "Use preemptible-h100" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Use reserved-h100" })).not.toBeChecked();
    expect(screen.getByLabelText("Hot floor")).toHaveValue(1);
    expect(screen.getByLabelText("Replica ceiling")).toHaveValue(3);
    expect(screen.getByLabelText("Priority class")).toHaveValue("standard");
    expect(screen.getByLabelText("OpenAI aliases")).toHaveValue("operator-alias");
    expect(screen.getByLabelText("MCP tool name")).toHaveValue("operator_tool");
    expect(screen.getByLabelText("Visibility")).toHaveValue("Private");
    expect(screen.getByLabelText("Allowed principals")).toHaveValue("operator-principal");
    expect(screen.queryByRole("heading", { name: "Render plan" })).not.toBeInTheDocument();
  });

  it("fails a create draft closed when qualified configuration cannot be read", async () => {
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockRejectedValue(new AdminApiError("capabilities unavailable", 503, "request-create"));
    renderCreatePage();

    const model = await screen.findByRole("combobox", { name: "Qualified model" });
    expect(model).toBeDisabled();
    expect(model).toHaveValue("");
    expect(await screen.findByText(/Qualified model choices are unavailable/)).toBeInTheDocument();
    expect(screen.getByLabelText("Model reference")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Validate draft" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Preview render plan" })).toBeDisabled();
  });

  it("keeps a create draft disabled when the server advertises zero complete configurations", async () => {
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(testEnvelope({
      ...modelDeploymentMutationCapabilitiesFixture,
      configuration_options: [],
    }));
    renderCreatePage();

    expect(await screen.findByText(/advertises no complete qualified model configurations/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Qualified model" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Validate draft" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Preview render plan" })).toBeDisabled();
  });

  it("supports typed edit, validation, render-plan, status and history with capability-gated actions", async () => {
    mockReadSurface();
    const validate = vi.spyOn(adminApi, "validateModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentValidationFixture));
    const plan = vi.spyOn(adminApi, "planModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPlanFixture));
    renderPage();

    expect(await screen.findByRole("heading", { name: "qwen-live" })).toBeInTheDocument();
    expect(screen.getByLabelText("Model reference")).toBeDisabled();
    expect(screen.getByLabelText("Runtime profile")).toBeDisabled();
    expect(screen.getByLabelText("Hot floor")).toHaveValue(0);
    expect(screen.getByLabelText("Replica ceiling")).toHaveValue(4);
    expect(screen.getByLabelText("Snapshot preference")).toHaveValue("Prefer");
    expect(screen.getByLabelText("Fast-start mode")).toHaveValue("Fixed");
    expect(screen.getByLabelText("Fast-start level")).toHaveValue("L3");
    expect(screen.getByLabelText("OpenAI aliases")).toHaveValue("qwen3-8b");
    expect(screen.getByLabelText("Allowed principals")).toHaveValue("research-agent");

    fireEvent.change(screen.getByLabelText("Cooldown (seconds)"), { target: { value: "240" } });
    fireEvent.change(screen.getByLabelText("Fast-start mode"), { target: { value: "Automatic" } });
    fireEvent.change(screen.getByLabelText("Minimum fast-start level"), { target: { value: "L1" } });
    fireEvent.change(screen.getByLabelText("Maximum fast-start level"), { target: { value: "L4" } });
    expect(screen.getByText("Best target ≤30 seconds; may assign the highest qualified level below L1.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("When the target is unavailable"), { target: { value: "RequireTarget" } });
    expect(screen.getByText("Best target ≤30 seconds; will not assign below L1.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("When the target is unavailable"), { target: { value: "AllowLowerLevel" } });
    fireEvent.click(screen.getByRole("button", { name: "Validate draft" }));
    await waitFor(() => expect(validate).toHaveBeenCalledWith(expect.objectContaining({
      name: "qwen-live",
      namespace: "fs2-models",
      base_etag: modelDeploymentRevisionFixture.etag,
      spec: expect.objectContaining({
        availability: expect.objectContaining({ cooldownSeconds: 240 }),
        fastStart: { mode: "Automatic", minimumLevel: "L1", maximumLevel: "L4", fallbackPolicy: "AllowLowerLevel" },
      }),
    })));
    expect(await screen.findByText("No validation issues were published.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Preview render plan" }));
    await waitFor(() => expect(plan).toHaveBeenCalledOnce());
    expect(await screen.findByRole("heading", { name: "Render plan" })).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /Deployment qwen-live/ })).toBeInTheDocument();
    for (const action of ["Apply", "Drain", "Rollback", "Reconcile"]) expect(screen.getByRole("button", { name: action })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toHaveAttribute("title", modelDeploymentMutationCapabilitiesFixture.hard_delete.reason);

    expect(screen.getByRole("heading", { name: "Ready" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Fast start" })).toBeInTheDocument();
    expect(screen.getAllByText("Hot · already serving").length).toBeGreaterThan(0);
    expect(screen.getByText("Fixed · L3 · Ready within 60 seconds")).toBeInTheDocument();
    expect(screen.getByText("Assigned target")).toBeInTheDocument();
    expect(screen.getByText("Observed p50")).toBeInTheDocument();
    expect(screen.getByText("Observed p95")).toBeInTheDocument();
    expect(screen.getByText("91.2 s")).toBeInTheDocument();
    expect(screen.getByText("112.7 s")).toBeInTheDocument();
    expect(screen.getByText(/requested L3 path is not qualified/)).toBeInTheDocument();
    const track = screen.getByLabelText("Model deployment lifecycle phases");
    for (const phase of ["Admitted", "Node pending", "Localizing", "Runtime starting", "Warming", "Ready", "Cached", "Cold", "Draining", "Failed", "Infrastructure required"]) {
      expect(within(track).getByText(phase)).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { name: "Revision history" })).toBeInTheDocument();
  });

  it("labels a short benchmark cohort exploratory and exposes its attempt, failure and path reason", async () => {
    mockReadSurface();
    const exploratory = structuredClone(modelDeploymentStatusFixture);
    exploratory.observation!.status.fastStart = {
      requestedLevel: "L2",
      assignedLevel: "Off",
      effectiveLevel: "Off",
      qualifiedLevel: "Off",
      selectedIdentityDigest: `sha256:${"c".repeat(64)}`,
      effectiveIdentityDigest: `sha256:${"c".repeat(64)}`,
      qualification: {
        state: "Fallback",
        reason: "RequestedLevelUnqualified",
        message: "Requested L2 is not qualified; Off is assigned.",
      },
      modelStart: {
        sampleCount: 3,
        failedCount: 0,
        latestSeconds: 92.1,
        latestObservedAt: "2026-09-02T07:31:00Z",
        p50Seconds: 90.4,
        p95Seconds: 92.1,
      },
      pools: [{
        poolRef: "preemptible-h100",
        acceleratorClass: "nvidia-h100-sxm5-80gb",
        qualifiedLevel: "Off",
        reason: "InsufficientBenchmarkSamples",
        mechanisms: ["shared-cache"],
        selectedMechanism: "shared-cache",
        selectedIdentityDigest: `sha256:${"c".repeat(64)}`,
        selectedCompatibilityTupleDigest: `sha256:${"a".repeat(64)}`,
        modelStart: {
          sampleCount: 3,
          failedCount: 0,
          latestSeconds: 92.1,
          latestObservedAt: "2026-09-02T07:31:00Z",
          p50Seconds: 90.4,
          p95Seconds: 92.1,
        },
        paths: [{
          mechanism: "shared-cache",
          identityDigest: `sha256:${"c".repeat(64)}`,
          compatibilityTupleDigest: `sha256:${"a".repeat(64)}`,
          qualifiedLevel: "Off",
          reason: "InsufficientBenchmarkSamples",
          modelStart: {
            sampleCount: 3,
            failedCount: 0,
            latestSeconds: 92.1,
            latestObservedAt: "2026-09-02T07:31:00Z",
            p50Seconds: 90.4,
            p95Seconds: 92.1,
          },
        }, {
          mechanism: "local-snapshot",
          identityDigest: `sha256:${"d".repeat(64)}`,
          compatibilityTupleDigest: `sha256:${"b".repeat(64)}`,
          qualifiedLevel: "Off",
          reason: "BenchmarkFailuresPresent",
          modelStart: {
            sampleCount: 3,
            failedCount: 1,
            latestSeconds: 58.3,
            latestObservedAt: "2026-09-02T07:31:00Z",
            p50Seconds: 57.1,
            p95Seconds: 58.3,
          },
        }],
        retainedPaths: [{
          mechanism: "shared-cache",
          identityState: "LegacyUnbound",
          identityDigest: null,
          compatibilityTupleDigest: `sha256:${"e".repeat(64)}`,
          observedPoolRef: "preemptible-h100",
          observedCapacityType: "preemptible",
          reason: "LegacyIdentityUnbound",
          mismatches: [{ code: "LegacyUnbound", field: "$.identity" }],
          receiptDigests: [`sha256:${"f".repeat(64)}`],
        }],
      }],
    };
    vi.mocked(adminApi.modelDeploymentStatus).mockResolvedValue(testEnvelope(exploratory));
    renderPage();

    const fastStart = await screen.findByRole("region", { name: "Fast start" });
    expect(within(fastStart).getByText("Observed p50")).toBeInTheDocument();
    expect(within(fastStart).getByText("Observed p95")).toBeInTheDocument();
    expect(within(fastStart).queryByText("Qualified p95")).not.toBeInTheDocument();
    expect(within(fastStart).getByText("Evidence attempts").parentElement).toHaveTextContent("3");
    expect(within(fastStart).getByText("Failed attempts").parentElement).toHaveTextContent("0");
    const poolEvidence = screen.getByLabelText("Per-pool fast-start evidence");
    expect(poolEvidence).toHaveTextContent("Evidence Exploratory");
    expect(poolEvidence).toHaveTextContent("Path shared-cache · identity");
    expect(poolEvidence).toHaveTextContent("evidence Exploratory");
    expect(poolEvidence).toHaveTextContent("Path local-snapshot · identity");
    expect(poolEvidence).toHaveTextContent("evidence Measured · failures present");
    expect(within(poolEvidence).getAllByText("InsufficientBenchmarkSamples")).toHaveLength(2);
    expect(within(poolEvidence).getByText("BenchmarkFailuresPresent")).toBeInTheDocument();
    expect(poolEvidence).toHaveTextContent("Retained LegacyUnbound evidence");
    expect(poolEvidence).toHaveTextContent("LegacyUnbound $.identity");
    expect(poolEvidence).toHaveTextContent(`Selected identity sha256:${"c".repeat(64)}`);
  });

  it("shows ModelExpress binding separately from customer qualification and keeps transfer evidence unavailable", async () => {
    mockReadSurface();
    const configured = structuredClone(modelDeploymentStatusFixture);
    configured.observation!.status.fastStart!.mechanisms = {
      modelexpress: {
        state: "Configured",
        configDigest: `sha256:${"9".repeat(64)}`,
        deploymentMode: "managed",
        endpoint: "fs2-modelexpress.fs2-modelexpress.svc.cluster.local:8001",
        metadataBackend: "kubernetes",
        runtimeAdapter: "vllm",
        clientPackageVersion: "0.5.1",
        coordinatorNetworkType: "pod-selector",
        coordinatorNamespace: "fs2-modelexpress",
        coordinatorPodLabels: { "fs2-serve.nebius.ai/component": "modelexpress-server" },
        coordinatorCidrs: [],
        poolRefs: ["reserved-h100", "preemptible-h100"],
        poolTransports: {
          "reserved-h100": {
            mode: "nixl-rdma",
            rdmaResourceName: "example.com/rdma_shared_device_a",
            rdmaResourceQuantity: 8,
            nixlBackend: "UCX",
            rdmaNicPin: "auto",
          },
          "preemptible-h100": {
            mode: "fallback",
            rdmaResourceName: null,
            rdmaResourceQuantity: 1,
            nixlBackend: "UCX",
            rdmaNicPin: "auto",
          },
        },
        configurationObserved: true,
        telemetryState: "Unavailable",
        selectedPath: null,
        transferredBytes: null,
        transferSeconds: null,
        fallbackReason: null,
      },
    };
    vi.mocked(adminApi.modelDeploymentStatus).mockResolvedValue(testEnvelope(configured));
    renderPage();

    const fastStart = (await screen.findByRole("heading", { name: "Fast start" })).closest("section")!;
    fireEvent.click(within(fastStart).getByText("Operator mechanism details"));
    expect(screen.getByText("Configured · configuration observed")).toBeInTheDocument();
    expect(screen.getByText(/managed · kubernetes/)).toBeInTheDocument();
    expect(screen.getByText("vllm · 0.5.1")).toBeInTheDocument();
    expect(screen.getByText(/reserved-h100: nixl-rdma · UCX · example.com\/rdma_shared_device_a × 8/)).toBeInTheDocument();
    expect(screen.getByText(/preemptible-h100: fallback · UCX · no RDMA resource/)).toBeInTheDocument();
    expect(screen.getByText(/fs2-modelexpress · fs2-serve.nebius.ai\/component=modelexpress-server/)).toBeInTheDocument();
    expect(screen.getByText("Unavailable · no per-deployment upstream path record")).toBeInTheDocument();
    expect(screen.getAllByText("L2 · Ready within 2 minutes").length).toBeGreaterThan(0);
  });

  it("shows unavailable observations explicitly and never turns absent replica values into zero", async () => {
    mockReadSurface();
    vi.spyOn(adminApi, "modelDeploymentStatus").mockResolvedValue(testEnvelope({
      ...modelDeploymentStatusFixture,
      state: "unavailable",
      observation: null,
      reason: "the model controller has not published a status observation",
    }));
    renderPage();

    expect(await screen.findByText("Observed runtime state is unavailable")).toBeInTheDocument();
    expect(screen.getByText(/No replica, cache, readiness or publication value is inferred/)).toBeInTheDocument();
    expect(screen.queryByText("Ready replicas", { selector: "dt" })).not.toBeInTheDocument();
  });

  it("renders legacy desired state without inventing a fast-start assignment or qualification", async () => {
    const legacyRevision = structuredClone(modelDeploymentRevisionFixture);
    delete legacyRevision.spec.fastStart;
    const legacyStatus = structuredClone(modelDeploymentStatusFixture);
    delete legacyStatus.observation!.status.fastStart;
    vi.spyOn(adminApi, "modelDeployment").mockResolvedValue(testEnvelope(legacyRevision));
    vi.spyOn(adminApi, "modelDeploymentStatus").mockResolvedValue(testEnvelope(legacyStatus));
    vi.spyOn(adminApi, "modelDeploymentHistory").mockResolvedValue(testEnvelope({ items: [legacyRevision], next_before_revision: null }));
    vi.spyOn(adminApi, "modelDeploymentCapabilities").mockResolvedValue(testEnvelope(modelDeploymentMutationCapabilitiesFixture));
    renderPage();

    expect(await screen.findByText("Legacy policy.")).toBeInTheDocument();
    expect(screen.getByText(/controller has not published fast-start evidence/)).toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0);
  });

  it("role-gates planning and has no accessibility violations in the viewer workflow", async () => {
    mockReadSurface();
    const { container } = renderPage("viewer");

    await screen.findByRole("heading", { name: "qwen-live" });
    expect(screen.getByRole("button", { name: "Validate draft" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Preview render plan" })).toBeDisabled();
    for (const action of ["Apply", "Drain", "Rollback", "Reconcile", "Delete"]) expect(screen.getByRole("button", { name: action })).toBeDisabled();
    expect(screen.getByText(/Viewer mode/)).toBeInTheDocument();
    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("applies an accepted plan with the full proposal and surfaces its verified write receipt", async () => {
    mockReadSurface();
    vi.spyOn(adminApi, "planModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPlanFixture));
    const apply = vi.spyOn(adminApi, "applyModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentAppliedFixture));
    renderPage("operator");

    await screen.findByRole("heading", { name: "qwen-live" });
    fireEvent.click(screen.getByRole("button", { name: "Preview render plan" }));
    expect(await screen.findByRole("heading", { name: "Render plan" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(apply).toHaveBeenCalledOnce());
    expect(apply).toHaveBeenCalledWith({
      preview_id: modelDeploymentPlanFixture.preview_id,
      proposed_etag: modelDeploymentPlanFixture.proposed_etag,
      proposal: {
        name: modelDeploymentRevisionFixture.name,
        namespace: modelDeploymentRevisionFixture.namespace,
        base_etag: modelDeploymentRevisionFixture.etag,
        spec: modelDeploymentRevisionFixture.spec,
      },
      idempotency_key: expect.stringMatching(/^model-deployment-apply-.{8,}$/),
    });
    expect(await screen.findByRole("heading", { name: "Revision r3 projected" })).toBeInTheDocument();
    expect(screen.getByText("Desired revision 3")).toBeInTheDocument();
    expect(screen.getByText("Applied and verified")).toBeInTheDocument();
    expect(screen.getByText("314")).toBeInTheDocument();
    expect(screen.getByText("model-deployment-uid-fixture")).toBeInTheDocument();
    expect(screen.getByText("Desired and observed revisions differ.")).toBeInTheDocument();
  });

  it("keeps a durable pending apply distinct from projection and retries the exact ETag", async () => {
    mockReadSurface();
    vi.spyOn(adminApi, "planModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPlanFixture));
    vi.spyOn(adminApi, "applyModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPendingFixture));
    const reconcile = vi.spyOn(adminApi, "reconcileModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentAppliedFixture));
    renderPage();

    await screen.findByRole("heading", { name: "qwen-live" });
    fireEvent.click(screen.getByRole("button", { name: "Preview render plan" }));
    await screen.findByRole("heading", { name: "Render plan" });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(await screen.findByRole("heading", { name: "Revision r3 pending projection" })).toBeInTheDocument();
    expect(screen.getByText(modelDeploymentPendingFixture.reason!)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry Kubernetes projection" });
    expect(retry).toBeEnabled();
    expect(screen.getByRole("button", { name: "Drain" })).toBeDisabled();
    fireEvent.click(retry);
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith("qwen-live", { expected_etag: modelDeploymentPendingFixture.revision.etag }));
    expect(await screen.findByRole("heading", { name: "Revision r3 projected" })).toBeInTheDocument();
  });

  it("confirms drain, reuses its idempotency key after a transient failure, and records success", async () => {
    mockReadSurface();
    const drain = vi.spyOn(adminApi, "drainModelDeployment")
      .mockRejectedValueOnce(new AdminApiError("writer temporarily unavailable", 503, "request-one"))
      .mockResolvedValueOnce(testEnvelope(modelDeploymentAppliedFixture));
    renderPage();

    await screen.findByRole("heading", { name: "qwen-live" });
    fireEvent.click(screen.getByRole("button", { name: "Drain" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm drain" }));
    expect(await screen.findByText("writer temporarily unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm drain" }));
    await waitFor(() => expect(drain).toHaveBeenCalledTimes(2));

    const firstPayload = drain.mock.calls[0]?.[1];
    const secondPayload = drain.mock.calls[1]?.[1];
    expect(firstPayload).toEqual({
      base_etag: modelDeploymentRevisionFixture.etag,
      idempotency_key: expect.stringMatching(/^model-deployment-drain-.{8,}$/),
    });
    expect(secondPayload?.idempotency_key).toBe(firstPayload?.idempotency_key);
    expect(await screen.findByRole("heading", { name: "Revision r3 projected" })).toBeInTheDocument();
  });

  it("rolls back only to a loaded earlier revision with the current base ETag", async () => {
    mockReadSurface();
    const rollback = vi.spyOn(adminApi, "rollbackModelDeployment").mockResolvedValue(testEnvelope({
      ...modelDeploymentAppliedFixture,
      revision: { ...modelDeploymentAppliedFixture.revision, action: "rollback" },
    }));
    renderPage();

    await screen.findByRole("heading", { name: "qwen-live" });
    expect(screen.getByLabelText("Rollback target revision")).toHaveValue("1");
    fireEvent.click(screen.getByRole("button", { name: "Rollback" }));
    expect(screen.getByText(/This commits revision r1 as a new desired revision/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm rollback" }));
    await waitFor(() => expect(rollback).toHaveBeenCalledWith("qwen-live", {
      target_revision: 1,
      base_etag: modelDeploymentRevisionFixture.etag,
      idempotency_key: expect.stringMatching(/^model-deployment-rollback-.{8,}$/),
    }));
  });

  it("fails closed when mutation capabilities cannot be read", async () => {
    mockReadSurface();
    vi.mocked(adminApi.modelDeploymentCapabilities).mockRejectedValue(new AdminApiError("capabilities unavailable", 503, "request-two"));
    renderPage();

    await screen.findByRole("heading", { name: "qwen-live" });
    expect(await screen.findByText(/Mutation capabilities are unavailable/)).toBeInTheDocument();
    for (const action of ["Apply", "Drain", "Rollback", "Reconcile", "Delete"]) expect(screen.getByRole("button", { name: action })).toBeDisabled();
  });

  it("does not re-offer a stored mechanism when its authoritative option is unavailable", async () => {
    mockReadSurface();
    vi.mocked(adminApi.modelDeployment).mockResolvedValue(testEnvelope({
      ...modelDeploymentRevisionFixture,
      spec: {
        ...modelDeploymentRevisionFixture.spec,
        cache: { ...modelDeploymentRevisionFixture.spec.cache, mechanism: "host-memory-residency" },
      },
    }));
    vi.mocked(adminApi.modelDeploymentCapabilities).mockRejectedValue(
      new AdminApiError("capabilities unavailable", 503, "request-mechanism"),
    );
    renderPage();

    await screen.findByRole("heading", { name: "qwen-live" });
    const mechanism = screen.getByLabelText("Cold-start mechanism");
    expect(mechanism).toBeDisabled();
    expect(mechanism).toHaveValue("host-memory-residency");
    expect(within(mechanism).getByRole("option", { name: "host-memory-residency" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/currently stored mechanism is unavailable/);
  });

  it("honors each advertised action capability independently", async () => {
    mockReadSurface();
    vi.mocked(adminApi.modelDeploymentCapabilities).mockResolvedValue(testEnvelope({
      ...modelDeploymentMutationCapabilitiesFixture,
      drain: { enabled: false, reason: "drain is paused during controller maintenance" },
    }));
    vi.spyOn(adminApi, "planModelDeployment").mockResolvedValue(testEnvelope(modelDeploymentPlanFixture));
    renderPage();

    await screen.findByRole("heading", { name: "qwen-live" });
    fireEvent.click(screen.getByRole("button", { name: "Preview render plan" }));
    await screen.findByRole("heading", { name: "Render plan" });
    expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Drain" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Drain" })).toHaveAttribute("title", "drain is paused during controller maintenance");
    expect(screen.getByRole("button", { name: "Rollback" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reconcile" })).toBeEnabled();
  });
});
