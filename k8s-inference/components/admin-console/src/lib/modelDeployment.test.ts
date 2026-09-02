import { describe, expect, it } from "vitest";
import {
  modelDeploymentMutationCapabilitiesFixture,
  modelDeploymentSpecFixture,
} from "../test/modelDeploymentFixtures";
import {
  createEmptyModelDeploymentSpec,
  draftFromConfigurationOption,
  localModelDeploymentProblem,
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
});
