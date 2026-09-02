import { describe, expect, it } from "vitest";
import {
  modelDeploymentMutationCapabilitiesFixture,
  modelDeploymentSpecFixture,
  modelDeploymentStatusFixture,
} from "../test/modelDeploymentFixtures";
import {
  createEmptyModelDeploymentSpec,
  draftFromConfigurationOption,
  effectiveFastStartLevel,
  fastStartPolicySummary,
  fastStartTarget,
  localModelDeploymentProblem,
  normalizedFastStartStatus,
  observedValue,
  uniqueCsv,
} from "./modelDeployment";

describe("ModelDeployment draft helpers", () => {
  it("starts with an explicitly incomplete, accelerator-neutral draft", () => {
    const draft = createEmptyModelDeploymentSpec("tenant-a");
    expect(draft.tenantId).toBe("tenant-a");
    expect(draft.placement.poolRefs).toEqual([]);
    expect(draft.availability.minReplicas).toBe(0);
    expect(draft.cache.snapshotPreference).toBe("Never");
    expect(draft.fastStart).toEqual({ mode: "Fixed", level: "Off", fallbackPolicy: "AllowLowerLevel" });
    expect(localModelDeploymentProblem("new-model", "fs2-models", draft)).toBe("Model reference is required.");
  });

  it("matches the backend's fail-closed lifecycle, digest, snapshot and MCP invariants", () => {
    const valid = structuredClone(modelDeploymentSpecFixture);
    expect(localModelDeploymentProblem("qwen-live", "fs2-models", valid)).toBeNull();

    const draining = structuredClone(valid);
    draining.lifecycle.desiredState = "Draining";
    draining.availability.minReplicas = 1;
    expect(localModelDeploymentProblem("qwen-live", "fs2-models", draining)).toMatch(/zero hot floor/);

    const snapshot = structuredClone(valid);
    snapshot.cache.snapshotPreference = "Require";
    snapshot.cache.snapshotRef = null;
    expect(localModelDeploymentProblem("qwen-live", "fs2-models", snapshot)).toMatch(/snapshot reference/);

    const mcp = structuredClone(valid);
    mcp.exposure.mcpToolName = null;
    expect(localModelDeploymentProblem("qwen-live", "fs2-models", mcp)).toMatch(/enabled together/);

    const fixed = structuredClone(valid);
    fixed.fastStart = { mode: "Fixed" };
    expect(localModelDeploymentProblem("qwen-live", "fs2-models", fixed)).toMatch(/needs a level/);

    const automatic = structuredClone(valid);
    automatic.fastStart = { mode: "Automatic", minimumLevel: "L4", maximumLevel: "L1" };
    expect(localModelDeploymentProblem("qwen-live", "fs2-models", automatic)).toMatch(/minimum cannot exceed/);

    const legacy = structuredClone(valid);
    delete legacy.fastStart;
    expect(localModelDeploymentProblem("qwen-live", "fs2-models", legacy)).toBeNull();
  });

  it("deduplicates CSV policy fields and never formats unavailable counts as zero", () => {
    expect(uniqueCsv("pool-a, pool-b, pool-a, ")).toEqual(["pool-a", "pool-b"]);
    expect(observedValue(null)).toBe("Unavailable");
    expect(observedValue(undefined)).toBe("Unavailable");
    expect(observedValue(0)).toBe("0");
  });

  it("seeds a first qualified model from the exact server default", () => {
    const option = modelDeploymentMutationCapabilitiesFixture.configuration_options[0]!;
    const empty = createEmptyModelDeploymentSpec("tenant-fixture");

    expect(draftFromConfigurationOption(empty, option)).toEqual(option.default_spec);
    expect(empty.modelRef).toBe("");
  });

  it("replaces model-coupled material while preserving operator policy on a model switch", () => {
    const current = structuredClone(modelDeploymentSpecFixture);
    current.lifecycle.desiredState = "Disabled";
    current.availability = { ...current.availability, minReplicas: 0, maxReplicas: 7 };
    current.queue = { localQueue: "inference", priorityClass: "interactive", maxQueueSeconds: 321 };
    current.rollout = { strategy: "Recreate", maxUnavailable: 1, maxSurge: 0, progressDeadlineSeconds: 600 };
    current.exposure = { openAI: true, openAIAliases: ["operator-alias"], mcp: true, mcpToolName: "operator_tool" };
    current.policy = { ...current.policy, visibility: "Private", allowedPrincipalIds: ["operator-principal"] };
    const option = structuredClone(modelDeploymentMutationCapabilitiesFixture.configuration_options[0]!);
    option.model_ref = "cosmos3-nano";
    option.default_spec = {
      ...structuredClone(option.default_spec),
      modelRef: "cosmos3-nano",
      artifact: { revision: "cosmos-r1", manifestDigest: `sha256:${"1".repeat(64)}`, storageRef: null },
      runtime: {
        profile: "cosmos-runtime",
        image: `registry.example.invalid/cosmos@sha256:${"2".repeat(64)}`,
        templateRef: { name: "cosmos-template", digest: `sha256:${"3".repeat(64)}` },
      },
      placement: { poolRefs: ["reserved-h100"], acceleratorsPerReplica: 2, topologyPolicy: "SingleNode" },
      cache: { tier: "Disabled", snapshotPreference: "Never", snapshotRef: null },
    };

    const switched = draftFromConfigurationOption(current, option);

    expect(switched.modelRef).toBe("cosmos3-nano");
    expect(switched.artifact).toEqual(option.default_spec.artifact);
    expect(switched.runtime).toEqual(option.default_spec.runtime);
    expect(switched.placement).toEqual(option.default_spec.placement);
    expect(switched.cache).toEqual(option.default_spec.cache);
    expect(switched.fastStart).toEqual(current.fastStart);
    expect(switched.lifecycle).toEqual(current.lifecycle);
    expect(switched.availability).toEqual(current.availability);
    expect(switched.queue).toEqual(current.queue);
    expect(switched.rollout).toEqual(current.rollout);
    expect(switched.exposure).toEqual(current.exposure);
    expect(switched.policy).toEqual(current.policy);
    expect(current.modelRef).toBe("qwen3-8b");
    expect(option.default_spec.modelRef).toBe("cosmos3-nano");
  });

  it("falls back only for policy choices the selected option no longer permits", () => {
    const current = structuredClone(modelDeploymentSpecFixture);
    current.tenantId = "tenant-retired";
    current.queue = { localQueue: "queue-retired", priorityClass: "priority-retired", maxQueueSeconds: 432 };
    const option = modelDeploymentMutationCapabilitiesFixture.configuration_options[0]!;

    const switched = draftFromConfigurationOption(current, option);

    expect(switched.tenantId).toBe(option.default_spec.tenantId);
    expect(switched.queue).toEqual({
      localQueue: option.default_spec.queue.localQueue,
      priorityClass: option.default_spec.queue.priorityClass,
      maxQueueSeconds: 432,
    });
  });

  it("does not preserve an enabled zero hot floor across an unqualified switch", () => {
    const current = structuredClone(modelDeploymentSpecFixture);
    const option = structuredClone(modelDeploymentMutationCapabilitiesFixture.configuration_options[0]!);
    option.scale_to_zero_qualified = false;
    option.default_spec.availability.minReplicas = 1;

    const switched = draftFromConfigurationOption(current, option);

    expect(switched.lifecycle.desiredState).toBe("Enabled");
    expect(switched.availability.minReplicas).toBe(1);
    expect(switched.availability.maxReplicas).toBeGreaterThanOrEqual(1);
  });

  it("keeps customer targets separate from observed qualification and derives Hot only from a ready replica", () => {
    expect(fastStartTarget("L1")).toBe("≤300 seconds");
    expect(fastStartTarget("Off")).toBe("No start-time target");
    expect(fastStartPolicySummary({ mode: "Automatic", minimumLevel: "L1", maximumLevel: "L4" })).toBe("Automatic · L1–L4");
    expect(fastStartPolicySummary(undefined)).toBe("Not configured");
    expect(effectiveFastStartLevel(modelDeploymentStatusFixture)).toBe("Hot");
    expect(normalizedFastStartStatus(modelDeploymentStatusFixture)).toMatchObject({
      requestedLevel: "L3",
      assignedLevel: "L2",
      qualifiedLevel: "L2",
      lastObservedSeconds: 91.2,
    });

    const stale = structuredClone(modelDeploymentStatusFixture);
    stale.state = "stale";
    expect(effectiveFastStartLevel(stale)).toBe("L2");
  });

  it("accepts snake-case fast-start observations during backend migration", () => {
    const view = structuredClone(modelDeploymentStatusFixture);
    const status = view.observation!.status;
    status.replicas!.ready = 0;
    delete status.fastStart;
    status.fast_start = {
      requested_level: "L4",
      assigned_level: "L2",
      effective_level: "L2",
      qualified_level: "L2",
      target_seconds: 120,
      qualified_p95_seconds: 118.4,
    };

    expect(effectiveFastStartLevel(view)).toBe("L2");
    expect(normalizedFastStartStatus(view)).toMatchObject({
      requestedLevel: "L4",
      assignedLevel: "L2",
      qualifiedLevel: "L2",
      targetSeconds: 120,
      qualifiedP95Seconds: 118.4,
    });
  });

  it("normalizes the controller's nested qualification and evidence status", () => {
    const view = structuredClone(modelDeploymentStatusFixture);
    view.observation!.status.replicas!.ready = 0;
    view.observation!.status.fastStart = {
      requestedLevel: "L3",
      assignedLevel: "L2",
      effectiveLevel: "L2",
      qualifiedLevel: "L2",
      targetSeconds: 120,
      qualification: {
        state: "Fallback",
        reason: "RequestedLevelUnqualified",
        message: "Requested L3 is not qualified; L2 is assigned.",
      },
      modelStart: {
        sampleCount: 20,
        failedCount: 0,
        latestSeconds: 91.2,
        latestObservedAt: "2026-09-02T07:31:00Z",
        p50Seconds: 88.4,
        p95Seconds: 112.7,
      },
      capacityWait: { sampleCount: 20, latestSeconds: 42.5, latestObservedAt: "2026-09-02T07:31:00Z" },
      endToEnd: { sampleCount: 20, latestSeconds: 133.7, latestObservedAt: "2026-09-02T07:31:00Z" },
      pools: [{
        poolRef: "reserved-h100",
        acceleratorClass: "nvidia-h100-sxm5-80gb",
        qualifiedLevel: "L2",
        reason: "BenchmarkP95WithinTarget",
        mechanisms: ["shared-cache"],
        receiptDigests: [`sha256:${"9".repeat(64)}`],
      }],
      automatic: {
        reason: "Promoted",
        evaluatedAt: "2026-09-02T07:31:00Z",
        historyComplete: true,
        mechanismId: "shared-cache",
        pendingLevel: null,
        consecutiveWins: 0,
        shortWindowRequests: 12,
        shortWindowColdActivations: 2,
        longWindowRequests: 70,
        longWindowColdActivations: 10,
      },
    };

    expect(normalizedFastStartStatus(view)).toMatchObject({
      state: "Fallback",
      reason: "Requested L3 is not qualified; L2 is assigned.",
      lastObservedSeconds: 91.2,
      qualifiedP50Seconds: 88.4,
      qualifiedP95Seconds: 112.7,
      capacityWaitSeconds: 42.5,
      endToEndSeconds: 133.7,
      observedAt: "2026-09-02T07:31:00Z",
      mechanisms: {
        "reserved-h100": {
          qualifiedLevel: "L2",
          mechanisms: ["shared-cache"],
        },
      },
      automatic: {
        reason: "Promoted",
        shortWindowRequests: 12,
        longWindowColdActivations: 10,
      },
    });
  });
});
