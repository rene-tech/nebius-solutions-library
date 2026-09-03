import type {
  ModelDeploymentCacheMechanismStatus,
  ModelDeploymentConfigurationOption,
  ModelDeploymentEffectiveFastStartLevel,
  ModelDeploymentFastStartFallbackPolicy,
  ModelDeploymentFastStartLevel,
  ModelDeploymentFastStartMode,
  ModelDeploymentFastStartPoolStatus,
  ModelDeploymentFastStartPolicy,
  ModelDeploymentFastStartStatus,
  ModelDeploymentRuntimePhase,
  ModelDeploymentSpec,
  ModelDeploymentStatusView,
} from "../api/modelDeploymentTypes";

const dnsLabel = /^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$/;
const dnsSubdomain = /^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*$/;
const sha256 = /^sha256:[a-f0-9]{64}$/;
const imageDigest = /^[^\s@]+@sha256:[a-f0-9]{64}$/;
const modelReference = /^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$/;
const tenant = /^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$/;
const revision = /^[A-Za-z0-9][A-Za-z0-9._:/+\-]*$/;
const openAIAlias = /^[A-Za-z0-9](?:[A-Za-z0-9._:/-]*[A-Za-z0-9])?$/;
const principalId = /^[A-Za-z0-9](?:[A-Za-z0-9_.:@/-]*[A-Za-z0-9])?$/;

export const modelDeploymentFastStartLevels = ["Off", "L1", "L2", "L3", "L4"] as const;

export const modelDeploymentFastStartLevelDetails: Record<ModelDeploymentFastStartLevel, {
  customerLabel: string;
  targetSeconds: number | null;
}> = {
  Off: { customerLabel: "Standard loading", targetSeconds: null },
  L1: { customerLabel: "Ready within 5 minutes", targetSeconds: 300 },
  L2: { customerLabel: "Ready within 2 minutes", targetSeconds: 120 },
  L3: { customerLabel: "Ready within 60 seconds", targetSeconds: 60 },
  L4: { customerLabel: "Ready within 30 seconds", targetSeconds: 30 },
};

export interface NormalizedFastStartPolicy {
  configured: boolean;
  mode: ModelDeploymentFastStartMode;
  level: ModelDeploymentFastStartLevel;
  minimumLevel: ModelDeploymentFastStartLevel;
  maximumLevel: ModelDeploymentFastStartLevel;
  fallbackPolicy: ModelDeploymentFastStartFallbackPolicy;
}

export interface NormalizedFastStartStatus {
  requestedLevel: ModelDeploymentFastStartLevel | null;
  assignedLevel: ModelDeploymentFastStartLevel | null;
  effectiveLevel: ModelDeploymentEffectiveFastStartLevel | null;
  qualifiedLevel: ModelDeploymentFastStartLevel | null;
  selectedIdentityDigest: string | null;
  effectiveIdentityDigest: string | null;
  state: string | null;
  reason: string | null;
  qualificationReason: string | null;
  targetSeconds: number | null;
  lastObservedSeconds: number | null;
  observedP50Seconds: number | null;
  observedP95Seconds: number | null;
  sampleCount: number | null;
  failedCount: number | null;
  capacityWaitSeconds: number | null;
  endToEndSeconds: number | null;
  observedAt: string | null;
  mechanisms: Record<string, unknown> | null;
  cacheMechanisms: Record<string, ModelDeploymentCacheMechanismStatus>;
  pools: ModelDeploymentFastStartPoolStatus[];
  automatic: ModelDeploymentFastStartStatus["automatic"];
}

const fastStartLevelOrder = new Map<ModelDeploymentFastStartLevel, number>(
  modelDeploymentFastStartLevels.map((level, index) => [level, index]),
);

export function normalizeFastStartPolicy(policy: ModelDeploymentFastStartPolicy | null | undefined): NormalizedFastStartPolicy {
  const mode = policy?.mode === "Automatic" ? "Automatic" : "Fixed";
  const level = policy?.level ?? "Off";
  const minimumLevel = policy?.minimumLevel ?? "Off";
  const maximumLevel = policy?.maximumLevel ?? (policy?.level ?? "L1");
  return {
    configured: Boolean(policy),
    mode,
    level,
    minimumLevel,
    maximumLevel,
    fallbackPolicy: policy?.fallbackPolicy ?? "AllowLowerLevel",
  };
}

export function fastStartTarget(level: ModelDeploymentEffectiveFastStartLevel | null | undefined): string {
  if (!level) return "Unavailable";
  if (level === "Hot") return "Already serving";
  const targetSeconds = modelDeploymentFastStartLevelDetails[level].targetSeconds;
  return targetSeconds === null ? "No start-time target" : `≤${targetSeconds} seconds`;
}

export function fastStartLevelLabel(level: ModelDeploymentEffectiveFastStartLevel | null | undefined): string {
  if (!level) return "Unavailable";
  if (level === "Hot") return "Hot · already serving";
  return `${level} · ${modelDeploymentFastStartLevelDetails[level].customerLabel}`;
}

export function fastStartPolicySummary(policy: ModelDeploymentFastStartPolicy | null | undefined): string {
  if (!policy) return "Not configured";
  const normalized = normalizeFastStartPolicy(policy);
  if (normalized.mode === "Automatic") return `Automatic · ${normalized.minimumLevel}–${normalized.maximumLevel}`;
  return `Fixed · ${fastStartLevelLabel(normalized.level)}`;
}

export function fastStartStatus(view: ModelDeploymentStatusView): ModelDeploymentFastStartStatus | null {
  const status = view.observation?.status;
  return status?.fastStart ?? status?.fast_start ?? null;
}

export function normalizedFastStartStatus(view: ModelDeploymentStatusView): NormalizedFastStartStatus | null {
  const status = fastStartStatus(view);
  if (!status) return null;
  const modelStart = status.modelStart ?? status.model_start ?? null;
  const capacityWait = status.capacityWait ?? status.capacity_wait ?? null;
  const endToEnd = status.endToEnd ?? status.end_to_end ?? null;
  const pools = status.pools ?? [];
  return {
    requestedLevel: status.requestedLevel ?? status.requested_level ?? null,
    assignedLevel: status.assignedLevel ?? status.assigned_level ?? null,
    effectiveLevel: status.effectiveLevel ?? status.effective_level ?? null,
    qualifiedLevel: status.qualifiedLevel ?? status.qualified_level ?? null,
    selectedIdentityDigest: status.selectedIdentityDigest ?? status.selected_identity_digest ?? null,
    effectiveIdentityDigest: status.effectiveIdentityDigest ?? status.effective_identity_digest ?? null,
    state: status.qualification?.state ?? status.state ?? null,
    reason: status.qualification?.message ?? status.qualification?.reason ?? status.reason ?? null,
    qualificationReason: status.qualification?.reason ?? null,
    targetSeconds: status.targetSeconds ?? status.target_seconds ?? null,
    lastObservedSeconds: status.lastObservedSeconds ?? status.last_observed_seconds
      ?? modelStart?.latestSeconds ?? modelStart?.latest_seconds ?? null,
    observedP50Seconds: status.qualifiedP50Seconds ?? status.qualified_p50_seconds
      ?? modelStart?.p50Seconds ?? modelStart?.p50_seconds ?? null,
    observedP95Seconds: status.qualifiedP95Seconds ?? status.qualified_p95_seconds
      ?? modelStart?.p95Seconds ?? modelStart?.p95_seconds ?? null,
    sampleCount: modelStart?.sampleCount ?? modelStart?.sample_count ?? null,
    failedCount: modelStart?.failedCount ?? modelStart?.failed_count ?? null,
    capacityWaitSeconds: status.capacityWaitSeconds ?? status.capacity_wait_seconds
      ?? capacityWait?.latestSeconds ?? capacityWait?.latest_seconds ?? null,
    endToEndSeconds: status.endToEndSeconds ?? status.end_to_end_seconds
      ?? endToEnd?.latestSeconds ?? endToEnd?.latest_seconds ?? null,
    observedAt: status.observedAt ?? status.observed_at
      ?? modelStart?.latestObservedAt ?? modelStart?.latest_observed_at ?? null,
    mechanisms: status.mechanisms ?? null,
    cacheMechanisms: status.cacheMechanisms ?? status.cache_mechanisms ?? {},
    pools,
    automatic: status.automatic ?? null,
  };
}

export function effectiveFastStartLevel(view: ModelDeploymentStatusView): ModelDeploymentEffectiveFastStartLevel | null {
  const status = view.observation?.status;
  if (
    view.state === "observed"
    && status?.replicas?.ready !== null
    && status?.replicas?.ready !== undefined
    && status.replicas.ready > 0
  ) return "Hot";
  return normalizedFastStartStatus(view)?.effectiveLevel ?? null;
}

export function fastStartSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unavailable";
  return `${value < 10 ? value.toFixed(2) : value.toFixed(1)} s`;
}

export function uniqueCsv(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

export function createEmptyModelDeploymentSpec(tenantId = ""): ModelDeploymentSpec {
  return {
    modelRef: "",
    tenantId,
    lifecycle: { desiredState: "Enabled" },
    artifact: { revision: "", manifestDigest: "", storageRef: null },
    runtime: { profile: "", image: "", templateRef: { name: "", digest: "" } },
    placement: { poolRefs: [], acceleratorsPerReplica: 1, topologyPolicy: "Any" },
    availability: {
      minReplicas: 0,
      maxReplicas: 1,
      idleSeconds: 300,
      targetQueueDepth: 1,
      pollingIntervalSeconds: 5,
      cooldownSeconds: 300,
      warmWindows: [],
    },
    cache: { tier: "Disabled", snapshotPreference: "Never", snapshotRef: null, mechanism: null },
    fastStart: { mode: "Fixed", level: "Off", fallbackPolicy: "AllowLowerLevel" },
    queue: { localQueue: "", priorityClass: "", maxQueueSeconds: 900 },
    rollout: { strategy: "Rolling", maxUnavailable: 0, maxSurge: 1, progressDeadlineSeconds: 1800 },
    exposure: { openAI: false, openAIAliases: [], mcp: false, mcpToolName: null },
    policy: { visibility: "Private", policyRef: "tenant-default.v1", allowedPrincipalIds: [], ratePolicyRef: null },
    adoption: { mode: "None", receiptRef: null },
  };
}

export function draftFromConfigurationOption(
  current: ModelDeploymentSpec,
  option: ModelDeploymentConfigurationOption,
): ModelDeploymentSpec {
  const next = structuredClone(option.default_spec);
  if (!current.modelRef) {
    if (option.tenant_choices.includes(current.tenantId)) next.tenantId = current.tenantId;
    return next;
  }

  next.tenantId = option.tenant_choices.includes(current.tenantId)
    ? current.tenantId
    : option.default_spec.tenantId;
  next.lifecycle = structuredClone(current.lifecycle);
  next.availability = structuredClone(current.availability);
  if (next.lifecycle.desiredState === "Enabled" && !option.scale_to_zero_qualified) {
    next.availability.minReplicas = Math.max(1, next.availability.minReplicas);
    next.availability.maxReplicas = Math.max(next.availability.minReplicas, next.availability.maxReplicas);
  }
  next.queue = {
    localQueue: option.local_queue_choices.includes(current.queue.localQueue)
      ? current.queue.localQueue
      : option.default_spec.queue.localQueue,
    priorityClass: option.priority_class_choices.includes(current.queue.priorityClass)
      ? current.queue.priorityClass
      : option.default_spec.queue.priorityClass,
    maxQueueSeconds: current.queue.maxQueueSeconds,
  };
  next.rollout = structuredClone(current.rollout);
  next.exposure = structuredClone(current.exposure);
  next.policy = structuredClone(current.policy);
  if (current.fastStart !== undefined) next.fastStart = structuredClone(current.fastStart);
  return next;
}

function required(value: string, label: string): string | null {
  return value.trim() ? null : `${label} is required.`;
}

function integerProblem(value: number, minimum: number, maximum: number, label: string): string | null {
  return Number.isInteger(value) && value >= minimum && value <= maximum
    ? null
    : `${label} must be a whole number between ${minimum} and ${maximum}.`;
}

export function localModelDeploymentProblem(
  name: string,
  namespace: string,
  spec: ModelDeploymentSpec,
): string | null {
  if (!dnsSubdomain.test(name)) return "Deployment name must be a lowercase Kubernetes DNS subdomain.";
  if (!dnsLabel.test(namespace)) return "Namespace must be a lowercase Kubernetes DNS label.";
  const requiredFields: Array<[string, string]> = [
    [spec.modelRef, "Model reference"],
    [spec.tenantId, "Tenant ID"],
    [spec.artifact.revision, "Artifact revision"],
    [spec.runtime.profile, "Runtime profile"],
    [spec.runtime.templateRef.name, "Runtime template name"],
    [spec.queue.localQueue, "Local queue"],
    [spec.queue.priorityClass, "Priority class"],
    [spec.policy.policyRef, "Tenant policy reference"],
  ];
  for (const [value, label] of requiredFields) {
    const problem = required(value, label);
    if (problem) return problem;
  }
  if (!modelReference.test(spec.modelRef)) return "Model reference contains unsupported characters.";
  if (!tenant.test(spec.tenantId)) return "Tenant ID contains unsupported characters.";
  if (!revision.test(spec.artifact.revision)) return "Artifact revision contains unsupported characters.";
  if (!sha256.test(spec.artifact.manifestDigest)) return "Artifact manifest digest must be sha256 followed by 64 lowercase hexadecimal characters.";
  if (spec.artifact.storageRef && !dnsSubdomain.test(spec.artifact.storageRef.name)) return "Artifact storage reference must be a Kubernetes DNS subdomain.";
  if (!modelReference.test(spec.runtime.profile)) return "Runtime profile contains unsupported characters.";
  if (!imageDigest.test(spec.runtime.image)) return "Runtime image must be pinned as repository@sha256:digest.";
  if (!dnsSubdomain.test(spec.runtime.templateRef.name)) return "Runtime template name must be a Kubernetes DNS subdomain.";
  if (!sha256.test(spec.runtime.templateRef.digest)) return "Runtime template digest must be a complete sha256 digest.";
  if (spec.placement.poolRefs.length === 0) return "At least one accelerator pool reference is required.";
  if (spec.placement.poolRefs.some((pool) => !modelReference.test(pool))) return "Accelerator pool references contain unsupported characters.";
  const numericFields: Array<[number, number, number, string]> = [
    [spec.placement.acceleratorsPerReplica, 1, 64, "Accelerators per replica"],
    [spec.availability.minReplicas, 0, 10000, "Hot floor"],
    [spec.availability.maxReplicas, 0, 10000, "Replica ceiling"],
    [spec.availability.idleSeconds, 0, 604800, "Idle duration"],
    [spec.availability.targetQueueDepth, 1, 100000, "Target queue depth"],
    [spec.availability.pollingIntervalSeconds, 1, 60, "Polling interval"],
    [spec.availability.cooldownSeconds, 5, 86400, "Cooldown"],
    [spec.queue.maxQueueSeconds, 1, 604800, "Maximum queue time"],
    [spec.rollout.maxUnavailable, 0, 10000, "Maximum unavailable"],
    [spec.rollout.maxSurge, 0, 10000, "Maximum surge"],
    [spec.rollout.progressDeadlineSeconds, 60, 86400, "Progress deadline"],
  ];
  for (const [value, minimum, maximum, label] of numericFields) {
    const problem = integerProblem(value, minimum, maximum, label);
    if (problem) return problem;
  }
  if (spec.availability.maxReplicas < spec.availability.minReplicas) return "Maximum replicas cannot be lower than the hot floor.";
  if (spec.lifecycle.desiredState !== "Enabled" && spec.availability.minReplicas !== 0) return "Draining and disabled models must use a zero hot floor.";
  if (spec.lifecycle.desiredState === "Enabled" && spec.availability.maxReplicas === 0) return "Enabled models must permit at least one replica.";
  if (spec.availability.warmWindows.some((window) => window.minReplicas > spec.availability.maxReplicas)) return "A warm-window floor cannot exceed the model replica ceiling.";
  if (new Set(spec.availability.warmWindows.map((window) => window.name)).size !== spec.availability.warmWindows.length) return "Warm-window names must be unique.";
  for (const window of spec.availability.warmWindows) {
    if (!dnsLabel.test(window.name) || window.schedule.length < 9 || !window.timeZone.trim()) return "Every warm window needs a valid name, schedule and time zone.";
    const durationProblem = integerProblem(window.durationSeconds, 60, 604800, "Warm-window duration");
    if (durationProblem) return durationProblem;
    const floorProblem = integerProblem(window.minReplicas, 1, 10000, "Warm-window floor");
    if (floorProblem) return floorProblem;
  }
  if (spec.cache.snapshotPreference === "Never" && spec.cache.snapshotRef !== null) return "Remove the snapshot reference when snapshot preference is Never.";
  if (spec.cache.snapshotPreference !== "Never" && spec.cache.snapshotRef === null) return "Prefer and Require snapshot policies need a qualified snapshot reference.";
  if (spec.cache.snapshotPreference === "Require" && spec.cache.tier === "Disabled") return "A required snapshot needs an enabled cache tier.";
  if (spec.cache.snapshotRef && !dnsSubdomain.test(spec.cache.snapshotRef.name)) return "Snapshot name must be a Kubernetes DNS subdomain.";
  if (spec.cache.snapshotRef && !sha256.test(spec.cache.snapshotRef.digest)) return "Snapshot digest must be a complete sha256 digest.";
  if (
    (spec.cache.mechanism === "regional-cache" || spec.cache.mechanism === "host-memory-residency")
    && spec.cache.tier !== "SharedFilesystem"
  ) return "A retained regional payload needs the SharedFilesystem cache tier.";
  if (spec.fastStart) {
    if (spec.fastStart.mode === "Fixed" && !spec.fastStart.level) return "A fixed fast-start policy needs a level.";
    if (spec.fastStart.mode === "Automatic") {
      if (!spec.fastStart.minimumLevel || !spec.fastStart.maximumLevel) return "An automatic fast-start policy needs minimum and maximum levels.";
      if ((fastStartLevelOrder.get(spec.fastStart.minimumLevel) ?? -1) > (fastStartLevelOrder.get(spec.fastStart.maximumLevel) ?? -1)) return "Automatic fast-start minimum cannot exceed its maximum.";
    }
  }
  if (!dnsSubdomain.test(spec.queue.localQueue) || !dnsSubdomain.test(spec.queue.priorityClass)) return "Queue and priority-class references must be Kubernetes DNS subdomains.";
  if (spec.rollout.strategy === "Rolling" && spec.rollout.maxUnavailable + spec.rollout.maxSurge === 0) return "A rolling rollout must permit unavailability or surge.";
  if (spec.rollout.strategy === "Recreate" && (spec.rollout.maxUnavailable !== 1 || spec.rollout.maxSurge !== 0)) return "Recreate rollout uses maximum unavailable 1 and maximum surge 0.";
  if (!spec.exposure.openAI && spec.exposure.openAIAliases.length > 0) return "OpenAI aliases cannot be published while OpenAI exposure is disabled.";
  if (spec.exposure.openAIAliases.some((alias) => !openAIAlias.test(alias))) return "An OpenAI alias contains unsupported characters.";
  if (spec.exposure.mcp !== Boolean(spec.exposure.mcpToolName)) return "MCP exposure and its tool name must be enabled together.";
  if (spec.exposure.mcpToolName && !/^[a-z][a-z0-9_]*$/.test(spec.exposure.mcpToolName)) return "MCP tool name must start with a lowercase letter and use lowercase letters, numbers or underscores.";
  if (!dnsSubdomain.test(spec.policy.policyRef) || (spec.policy.ratePolicyRef && !dnsSubdomain.test(spec.policy.ratePolicyRef))) return "Tenant and rate policy references must be Kubernetes DNS subdomains.";
  if (spec.policy.allowedPrincipalIds.some((principal) => !principalId.test(principal))) return "An allowed principal ID contains unsupported characters.";
  if (spec.adoption.mode === "Claim" && !spec.adoption.receiptRef) return "Claim adoption requires a verified receipt reference.";
  if (spec.adoption.receiptRef && (!dnsSubdomain.test(spec.adoption.receiptRef.name) || !sha256.test(spec.adoption.receiptRef.digest))) return "Adoption receipt needs a Kubernetes name and complete sha256 digest.";
  return null;
}

export const modelDeploymentPhaseLabels: Record<ModelDeploymentRuntimePhase, string> = {
  Desired: "Desired",
  Admitted: "Admitted",
  NodePending: "Node pending",
  Localizing: "Localizing",
  RuntimeStarting: "Runtime starting",
  Warming: "Warming",
  Ready: "Ready",
  Cold: "Cold",
  Draining: "Draining",
  Failed: "Failed",
  InfrastructureRequired: "Infrastructure required",
};

export function statusPhase(view: ModelDeploymentStatusView): ModelDeploymentRuntimePhase | null {
  return view.observation?.status.phase ?? null;
}

export function observedValue(value: number | null | undefined): string {
  return value === null || value === undefined ? "Unavailable" : String(value);
}
