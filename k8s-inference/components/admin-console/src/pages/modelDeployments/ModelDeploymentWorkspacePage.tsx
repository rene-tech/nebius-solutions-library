import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { adminApi, AdminApiError } from "../../api/client";
import type {
  ModelDeploymentActionCapability,
  ModelDeploymentFastStartLevel,
  ModelExpressMechanismStatus,
  ModelDeploymentFastStartStatistics,
  ModelDeploymentMutationResult,
  ModelDeploymentRenderPreview,
  ModelDeploymentRevision,
  ModelDeploymentSpec,
  ModelDeploymentStatusView,
  ModelDeploymentValidationDecision,
} from "../../api/modelDeploymentTypes";
import { useSession } from "../../auth/SessionContext";
import { DataBoundary } from "../../components/DataBoundary";
import { rolePermits } from "../../lib/access";
import { formatTimestamp } from "../../lib/format";
import {
  createEmptyModelDeploymentSpec,
  draftFromConfigurationOption,
  effectiveFastStartLevel,
  fastStartLevelLabel,
  fastStartPolicySummary,
  fastStartSeconds,
  fastStartTarget,
  localModelDeploymentProblem,
  modelDeploymentPhaseLabels,
  normalizedFastStartStatus,
  observedValue,
} from "../../lib/modelDeployment";
import { sharedContextParams } from "../../lib/search";
import { ModelDeploymentForm } from "./ModelDeploymentForm";

function DecisionPanel({ decision, heading }: { decision: ModelDeploymentValidationDecision; heading: string }) {
  const dispositionClass = decision.disposition === "accepted"
    ? "capability-chip--healthy"
    : decision.disposition === "infrastructure-required"
      ? "capability-chip--degraded"
      : "capability-chip--unhealthy";
  return (
    <section className="panel section-stack" aria-labelledby={`${heading.replaceAll(" ", "-")}-title`}>
      <div className="section-heading">
        <div><span className="eyebrow">Server-authoritative policy</span><h2 id={`${heading.replaceAll(" ", "-")}-title`}>{heading}</h2></div>
        <span className={`capability-chip ${dispositionClass}`}>{decision.disposition.replace("-", " ")}</span>
      </div>
      <dl className="definition-grid">
        <div><dt>Proposed spec</dt><dd><code>{decision.specDigest}</code></dd></div>
        <div><dt>Admitted pool</dt><dd>{decision.admittedPoolRef ?? "Not admitted"}</dd></div>
      </dl>
      {decision.issues.length ? (
        <ul className="configuration-issues">
          {decision.issues.map((issue) => <li className={`configuration-issue configuration-issue--${issue.severity}`} key={`${issue.code}/${issue.path}`}><code>{issue.code}</code><span>{issue.path} · {issue.owner}</span><span>{issue.message}</span></li>)}
        </ul>
      ) : <p className="supporting-copy">No validation issues were published.</p>}
      {decision.terraformInputs.length ? <div className="inline-notice inline-notice--warning" role="status"><strong>Terraform infrastructure required.</strong> {decision.terraformInputs.join(", ")}</div> : null}
    </section>
  );
}

function RenderPanel({ preview }: { preview: ModelDeploymentRenderPreview }) {
  return (
    <section className="panel section-stack">
      <div className="section-heading"><div><span className="eyebrow">Deterministic preview</span><h2>Render plan</h2></div><span className="quiet-chip">Expires {formatTimestamp(preview.expires_at)}</span></div>
      <dl className="definition-grid">
        <div><dt>Preview ID</dt><dd><code>{preview.preview_id}</code></dd></div>
        <div><dt>Proposed ETag</dt><dd><code>{preview.proposed_etag}</code></dd></div>
        <div><dt>Renderer</dt><dd>{preview.render?.renderer ?? "Not rendered"}</dd></div>
        <div><dt>Resources</dt><dd>{preview.render ? preview.render.resources.length : "Unavailable"}</dd></div>
        <div><dt>Planned endpoint</dt><dd>{preview.render ? `${preview.render.endpoint.namespace}/${preview.render.endpoint.serviceName}:${preview.render.endpoint.servicePort}` : "Unavailable"}</dd></div>
      </dl>
      {preview.render ? (
        <div className="table-frame">
          <table className="resource-table resource-table--render-preview">
            <caption className="sr-only">Resources in the deterministic render preview</caption>
            <thead><tr><th scope="col">Resource</th><th scope="col">Namespace</th><th scope="col">Digest</th><th scope="col">Field ownership</th></tr></thead>
            <tbody>{preview.render.resources.map((resource) => <tr key={`${resource.apiVersion}/${resource.kind}/${resource.namespace}/${resource.name}`}><th scope="row">{resource.kind} <code>{resource.name}</code><span className="secondary-line">{resource.apiVersion}</span></th><td>{resource.namespace}</td><td><code>{resource.digest}</code></td><td>{resource.fieldManager}<span className="secondary-line">force conflicts: {resource.forceConflicts ? "yes" : "no"}</span></td></tr>)}</tbody>
          </table>
        </div>
      ) : <div className="state-panel state-panel--empty"><strong>No runtime objects rendered</strong><span>An infrastructure-required preview cannot render until its Terraform-owned dependencies exist.</span></div>}
      <p className="supporting-copy">Mutation availability is evaluated independently from the live capability contract and your operator role.</p>
    </section>
  );
}

function MutationResultPanel({
  action,
  result,
}: {
  action: "apply" | "drain" | "rollback" | "reconcile";
  result: ModelDeploymentMutationResult;
}) {
  const applied = result.projection === "applied";
  return (
    <section className="panel section-stack" aria-labelledby="mutation-result-title">
      <div className="section-heading">
        <div><span className="eyebrow">Durable mutation result</span><h2 id="mutation-result-title">Revision r{result.revision.revision} {applied ? "projected" : "pending projection"}</h2></div>
        <span className={`capability-chip ${applied ? "capability-chip--healthy" : "capability-chip--degraded"}`}>{applied ? "Applied" : "Durable · pending"}</span>
      </div>
      <dl className="definition-grid">
        <div><dt>Action</dt><dd>{action}</dd></div>
        <div><dt>Desired ETag</dt><dd><code>{result.revision.etag}</code></dd></div>
        <div><dt>Durability</dt><dd>Revision history committed</dd></div>
        <div><dt>Idempotent replay</dt><dd>{result.idempotent_replay ? "Yes" : "No"}</dd></div>
        <div><dt>Kubernetes projection</dt><dd>{applied ? "Applied and verified" : "Pending retry"}</dd></div>
        <div><dt>Resource</dt><dd>{result.receipt ? `${result.receipt.namespace}/${result.receipt.name}` : "Unavailable until projection succeeds"}</dd></div>
        <div><dt>Generation</dt><dd>{result.receipt?.generation ?? "Unavailable"}</dd></div>
        <div><dt>Resource version</dt><dd>{result.receipt?.resource_version ?? "Unavailable"}</dd></div>
        <div><dt>Resource UID</dt><dd>{result.receipt?.uid ?? "Unavailable"}</dd></div>
        <div><dt>Verified spec digest</dt><dd>{result.receipt ? <code>{result.receipt.spec_digest}</code> : "Unavailable"}</dd></div>
      </dl>
      {!applied ? <div className="inline-notice inline-notice--warning" role="status"><strong>The desired revision is durable.</strong> {result.reason ?? "Kubernetes projection is pending retry."}</div> : null}
    </section>
  );
}

type BusyAction = "validate" | "plan" | "apply" | "drain" | "rollback" | "reconcile";
type MutationAction = Exclude<BusyAction, "validate" | "plan">;

function newIdempotencyKey(action: Exclude<MutationAction, "reconcile">): string {
  const random = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `model-deployment-${action}-${random}`;
}

function capabilityReason(
  capability: ModelDeploymentActionCapability | undefined,
  capabilitiesPending: boolean,
  capabilitiesFailed: boolean,
  roleAllowed: boolean,
): string | null {
  if (!roleAllowed) return "Operator role required.";
  if (capabilitiesPending) return "Loading the server-authoritative mutation capabilities.";
  if (capabilitiesFailed || !capability) return "Mutation capability data is unavailable; controls fail closed.";
  if (!capability.enabled) return capability.reason ?? "The control plane disabled this mutation.";
  return null;
}

function mechanismValue(value: unknown): string {
  if (value === null || value === undefined) return "Unavailable";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(mechanismValue).join(", ");
  try { return JSON.stringify(value); } catch { return "Unavailable"; }
}

function modelExpressStatus(value: unknown): ModelExpressMechanismStatus | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as ModelExpressMechanismStatus
    : null;
}

function modelExpressTransportSummary(status: ModelExpressMechanismStatus): string {
  const transports = status.poolTransports ?? status.pool_transports ?? {};
  const entries = Object.entries(transports).sort(([left], [right]) => left.localeCompare(right));
  if (!entries.length) return "Unavailable";
  return entries.map(([pool, transport]) => {
    const mode = transport.mode ?? "Unavailable";
    const backend = transport.nixlBackend ?? transport.nixl_backend ?? "backend unavailable";
    const resource = transport.rdmaResourceName ?? transport.rdma_resource_name;
    const quantity = transport.rdmaResourceQuantity ?? transport.rdma_resource_quantity ?? "unknown";
    const nic = transport.rdmaNicPin ?? transport.rdma_nic_pin ?? "not pinned";
    return `${pool}: ${mode} · ${backend} · ${resource ? `${resource} × ${quantity}` : "no RDMA resource"} · NIC ${nic}`;
  }).join(" | ");
}

function modelExpressCoordinatorSummary(status: ModelExpressMechanismStatus): string {
  const routeType = status.coordinatorNetworkType ?? status.coordinator_network_type ?? "Unavailable";
  const namespace = status.coordinatorNamespace ?? status.coordinator_namespace;
  const labels = status.coordinatorPodLabels ?? status.coordinator_pod_labels ?? {};
  const cidrs = status.coordinatorCidrs ?? status.coordinator_cidrs ?? [];
  if (routeType === "pod-selector") {
    const selector = Object.entries(labels).sort(([left], [right]) => left.localeCompare(right))
      .map(([key, value]) => `${key}=${value}`).join(", ");
    return `${namespace ?? "namespace unavailable"} · ${selector || "selector unavailable"}`;
  }
  if (routeType === "ip-blocks") return cidrs.join(", ") || "CIDR unavailable";
  return "Unavailable";
}

function statisticValue(
  statistics: ModelDeploymentFastStartStatistics | null | undefined,
  field: "sample" | "failed" | "p50" | "p95",
): string {
  if (!statistics) return "Unavailable";
  if (field === "sample") return String(statistics.sampleCount ?? statistics.sample_count ?? "Unavailable");
  if (field === "failed") return String(statistics.failedCount ?? statistics.failed_count ?? "Unavailable");
  if (field === "p50") return fastStartSeconds(statistics.p50Seconds ?? statistics.p50_seconds);
  return fastStartSeconds(statistics.p95Seconds ?? statistics.p95_seconds);
}

function evidenceClass(
  reason: string | null | undefined,
  level: ModelDeploymentFastStartLevel,
  statistics: ModelDeploymentFastStartStatistics | null | undefined,
): string {
  if (!statistics) return "Unavailable";
  if (reason === "InsufficientBenchmarkSamples") return "Exploratory";
  if (reason === "IncompleteCompatibilityTuple") return "Unusable · incomplete tuple";
  if (["BenchmarkFailuresPresent", "BenchmarkFailuresExceedPercentile"].includes(reason ?? "")) {
    return "Measured · failures present";
  }
  if (reason === "BenchmarkP95WithinTarget" && level !== "Off") return "Qualified";
  return "Measured · no level qualified";
}

function FastStartRuntime({ view, spec }: { view: ModelDeploymentStatusView; spec: ModelDeploymentSpec }) {
  const observed = normalizedFastStartStatus(view);
  const effective = effectiveFastStartLevel(view);
  const requested = spec.fastStart
    ? fastStartPolicySummary(spec.fastStart)
    : observed?.requestedLevel
      ? fastStartLevelLabel(observed.requestedLevel)
      : "Not configured";
  const assigned = observed?.assignedLevel ? fastStartLevelLabel(observed.assignedLevel) : "Unavailable";
  const qualified = observed?.qualifiedLevel ? fastStartLevelLabel(observed.qualifiedLevel) : "Unavailable";
  const target = observed?.targetSeconds === null || observed?.targetSeconds === undefined
    ? fastStartTarget(observed?.assignedLevel)
    : `≤${observed.targetSeconds} seconds`;
  const modelExpress = modelExpressStatus(observed?.mechanisms?.modelexpress);
  const mechanisms = observed?.mechanisms
    ? Object.entries(observed.mechanisms).filter(([name]) => name !== "modelexpress")
    : [];
  return (
    <section className="subpanel section-stack" aria-labelledby="fast-start-runtime-title">
      <div className="section-heading">
        <div><span className="eyebrow">Customer start-time class</span><h3 id="fast-start-runtime-title">Fast start</h3></div>
        <span className={`capability-chip ${effective === "Hot" ? "capability-chip--healthy" : observed ? "capability-chip--unknown" : "capability-chip--unavailable"}`}>{effective ? fastStartLevelLabel(effective) : "Unavailable"}</span>
      </div>
      <div className="fast-start-status">
        <div><span>Requested</span><strong>{requested}</strong></div>
        <div><span>Assigned</span><strong>{assigned}</strong></div>
        <div><span>Effective</span><strong>{effective ? fastStartLevelLabel(effective) : "Unavailable"}</strong></div>
        <div><span>Qualified</span><strong>{qualified}</strong></div>
      </div>
      <div className="fast-start-observation">
        <span>Assigned target <strong>{target}</strong></span>
        <span>Model ready <strong>{fastStartSeconds(observed?.lastObservedSeconds)}</strong></span>
        <span>Observed p50 <strong>{fastStartSeconds(observed?.observedP50Seconds)}</strong></span>
        <span>Observed p95 <strong>{fastStartSeconds(observed?.observedP95Seconds)}</strong></span>
        <span>Evidence attempts <strong>{observed?.sampleCount ?? "Unavailable"}</strong></span>
        <span>Failed attempts <strong>{observed?.failedCount ?? "Unavailable"}</strong></span>
        <span>Capacity wait <strong>{fastStartSeconds(observed?.capacityWaitSeconds)}</strong></span>
        <span>Request to ready <strong>{fastStartSeconds(observed?.endToEndSeconds)}</strong></span>
      </div>
      {!observed ? <div className="inline-notice inline-notice--warning" role="status"><strong>Qualification unavailable.</strong> The controller has not published fast-start evidence for this revision; cache settings are not treated as proof.</div> : observed.reason ? <div className="inline-notice" role="status"><strong>{observed.state ?? "Fast-start status"}.</strong> {observed.reason}</div> : null}
      <details className="fast-start-status-details">
        <summary>Operator mechanism details</summary>
        <dl className="definition-grid">
          <div><dt>Desired cache tier</dt><dd>{spec.cache.tier}</dd></div>
          <div><dt>Snapshot preference</dt><dd>{spec.cache.snapshotPreference}</dd></div>
          <div><dt>Snapshot identity</dt><dd>{spec.cache.snapshotRef ? `${spec.cache.snapshotRef.name} · ${spec.cache.snapshotRef.strategy}` : "Not configured"}</dd></div>
          <div><dt>Evidence observed</dt><dd>{observed?.observedAt ? formatTimestamp(observed.observedAt) : "Unavailable"}</dd></div>
          <div><dt>Qualification reason</dt><dd>{observed?.qualificationReason ? <code>{observed.qualificationReason}</code> : "Unavailable"}</dd></div>
          {observed?.automatic ? <>
            <div><dt>Automatic decision</dt><dd>{observed.automatic.reason ?? "Unavailable"}</dd></div>
            <div><dt>Automatic evaluated</dt><dd>{(observed.automatic.evaluatedAt ?? observed.automatic.evaluated_at) ? formatTimestamp(observed.automatic.evaluatedAt ?? observed.automatic.evaluated_at ?? "") : "Unavailable"}</dd></div>
            <div><dt>Demand history</dt><dd>{(observed.automatic.historyComplete ?? observed.automatic.history_complete) ? "Complete" : "Unavailable · minimum/fallback used"}</dd></div>
            <div><dt>Selected mechanism</dt><dd>{observed.automatic.mechanismId ?? observed.automatic.mechanism_id ?? "Unavailable"}</dd></div>
            <div><dt>Pending level</dt><dd>{observed.automatic.pendingLevel ?? observed.automatic.pending_level ?? "None"}</dd></div>
            <div><dt>Promotion wins</dt><dd>{observed.automatic.consecutiveWins ?? observed.automatic.consecutive_wins ?? 0}</dd></div>
            <div><dt>Requests · 1h / 7d</dt><dd>{observed.automatic.shortWindowRequests ?? observed.automatic.short_window_requests ?? "Unavailable"} / {observed.automatic.longWindowRequests ?? observed.automatic.long_window_requests ?? "Unavailable"}</dd></div>
            <div><dt>Exact cold activations · 1h / 7d</dt><dd>{observed.automatic.shortWindowColdActivations ?? observed.automatic.short_window_cold_activations ?? "Unavailable"} / {observed.automatic.longWindowColdActivations ?? observed.automatic.long_window_cold_activations ?? "Unavailable"}</dd></div>
            <div><dt>Idle-gap episodes · 1h / 7d</dt><dd>{observed.automatic.shortWindowIdleGapEpisodes ?? observed.automatic.short_window_idle_gap_episodes ?? "Unavailable"} / {observed.automatic.longWindowIdleGapEpisodes ?? observed.automatic.long_window_idle_gap_episodes ?? "Unavailable"}</dd></div>
          </> : null}
          {modelExpress ? <>
            <div><dt>ModelExpress</dt><dd>{modelExpress.state ?? "Unavailable"} · configuration {modelExpress.configurationObserved ?? modelExpress.configuration_observed ? "observed" : "pending"}</dd></div>
            <div><dt>ModelExpress service</dt><dd>{modelExpress.deploymentMode ?? modelExpress.deployment_mode ?? "Unavailable"} · {modelExpress.metadataBackend ?? modelExpress.metadata_backend ?? "Unavailable"} · <code>{modelExpress.endpoint ?? "Unavailable"}</code></dd></div>
            <div><dt>ModelExpress client</dt><dd>{modelExpress.runtimeAdapter ?? modelExpress.runtime_adapter ?? "Unavailable"} · {modelExpress.clientPackageVersion ?? modelExpress.client_package_version ?? "version unavailable"}</dd></div>
            <div><dt>ModelExpress pool transports</dt><dd>{modelExpressTransportSummary(modelExpress)}</dd></div>
            <div><dt>Coordinator network scope</dt><dd>{modelExpressCoordinatorSummary(modelExpress)}</dd></div>
            <div><dt>ModelExpress pools</dt><dd>{(modelExpress.poolRefs ?? modelExpress.pool_refs ?? []).join(", ") || "Unavailable"}</dd></div>
            <div><dt>ModelExpress config</dt><dd><code>{modelExpress.configDigest ?? modelExpress.config_digest ?? "Unavailable"}</code></dd></div>
            <div><dt>Observed transfer path</dt><dd>{modelExpress.selectedPath ?? modelExpress.selected_path ?? "Unavailable"}</dd></div>
            <div><dt>Observed transfer</dt><dd>{(modelExpress.transferredBytes ?? modelExpress.transferred_bytes) === null || (modelExpress.transferredBytes ?? modelExpress.transferred_bytes) === undefined ? "Unavailable" : `${modelExpress.transferredBytes ?? modelExpress.transferred_bytes} bytes`} · {(modelExpress.transferSeconds ?? modelExpress.transfer_seconds) === null || (modelExpress.transferSeconds ?? modelExpress.transfer_seconds) === undefined ? "duration unavailable" : `${modelExpress.transferSeconds ?? modelExpress.transfer_seconds}s`}</dd></div>
            <div><dt>Transfer telemetry</dt><dd>{modelExpress.telemetryState ?? modelExpress.telemetry_state ?? "Unavailable"}{(modelExpress.fallbackReason ?? modelExpress.fallback_reason) ? ` · ${modelExpress.fallbackReason ?? modelExpress.fallback_reason}` : " · no per-deployment upstream path record"}</dd></div>
          </> : null}
          {mechanisms.map(([name, value]) => <div key={name}><dt>{name.replaceAll("_", " ")}</dt><dd>{mechanismValue(value)}</dd></div>)}
          {!mechanisms.length && !observed?.pools.length ? <div><dt>Controller mechanisms</dt><dd>Unavailable</dd></div> : null}
        </dl>
        {observed?.pools.length ? <div className="condition-list" aria-label="Per-pool fast-start evidence">
          {observed.pools.map((pool, poolIndex) => {
            const poolRef = pool.poolRef ?? pool.pool_ref ?? `pool-${poolIndex + 1}`;
            const poolLevel = pool.qualifiedLevel ?? pool.qualified_level ?? "Off";
            const poolModelStart = pool.modelStart ?? pool.model_start;
            const selectedMechanism = pool.selectedMechanism ?? pool.selected_mechanism;
            const selectedTuple = pool.selectedCompatibilityTupleDigest ?? pool.selected_compatibility_tuple_digest;
            return <div key={poolRef}>
              <span className="mini-chip">{poolRef} · {pool.acceleratorClass ?? pool.accelerator_class ?? "accelerator unavailable"}</span>
              <strong>Qualified {poolLevel}</strong>
              <span>Evidence {evidenceClass(pool.reason, poolLevel, poolModelStart)}</span>
              <span>Reason <code>{pool.reason ?? "Unavailable"}</code></span>
              <span>Selected mechanism {selectedMechanism ?? "Unavailable"}</span>
              <span>Observed attempts {statisticValue(poolModelStart, "sample")} · failures {statisticValue(poolModelStart, "failed")} · p50 {statisticValue(poolModelStart, "p50")} · p95 {statisticValue(poolModelStart, "p95")}</span>
              {selectedTuple ? <span>Compatibility tuple <code>{selectedTuple}</code></span> : null}
              {(pool.paths ?? []).map((path, pathIndex) => {
                const pathModelStart = path.modelStart ?? path.model_start;
                const pathLevel = path.qualifiedLevel ?? path.qualified_level ?? "Off";
                return <span key={`${path.mechanism ?? "path"}-${path.compatibilityTupleDigest ?? path.compatibility_tuple_digest ?? pathIndex}`}>
                  Path {path.mechanism ?? "Unavailable"} · evidence {evidenceClass(path.reason, pathLevel, pathModelStart)} · qualified {pathLevel} · reason <code>{path.reason ?? "Unavailable"}</code> · attempts {statisticValue(pathModelStart, "sample")} · failures {statisticValue(pathModelStart, "failed")} · p95 {statisticValue(pathModelStart, "p95")}
                </span>;
              })}
            </div>;
          })}
        </div> : null}
      </details>
      <p className="empty-copy">Model-ready time starts when compatible accelerator capacity is available. Capacity wait and request-to-ready time remain separate.</p>
    </section>
  );
}

function RuntimeStatus({ view, expectedRevision, spec }: { view: ModelDeploymentStatusView; expectedRevision: number; spec: ModelDeploymentSpec }) {
  if (view.state === "unavailable") {
    return <div className="state-panel state-panel--error" role="status"><strong>Observed runtime state is unavailable</strong><span>{view.reason ?? "The controller has not published an observation."}</span><span>No replica, cache, readiness or publication value is inferred.</span></div>;
  }
  const observation = view.observation;
  if (!observation) {
    return <div className="state-panel state-panel--error"><strong>Invalid status response</strong><span>The backend declared {view.state} without an observation.</span></div>;
  }
  const status = observation.status;
  const stale = view.state === "stale" || view.revision !== expectedRevision;
  const staleReason = view.reason ?? "The latest durable revision has not yet appeared in this status response.";
  return (
    <section className="panel section-stack">
      <div className="section-heading"><div><span className="eyebrow">Kubernetes observation</span><h2>{modelDeploymentPhaseLabels[status.phase]}</h2></div><span className={`capability-chip ${stale ? "capability-chip--stale" : status.phase === "Ready" ? "capability-chip--healthy" : status.phase === "Failed" ? "capability-chip--unhealthy" : "capability-chip--unknown"}`}>{stale ? "Stale observation" : "Observed"}</span></div>
      {stale ? <div className="inline-notice inline-notice--warning" role="status"><strong>Desired and observed revisions differ.</strong> {staleReason}</div> : null}
      <div className="runtime-phase-track" aria-label="Model deployment lifecycle phases">
        {(["Admitted", "NodePending", "Localizing", "RuntimeStarting", "Warming", "Ready", "Cached", "Cold", "Draining", "Failed", "InfrastructureRequired"] as const).map((phase) => <span aria-current={phase === status.phase ? "step" : undefined} className={phase === status.phase ? "runtime-phase runtime-phase--current" : "runtime-phase"} key={phase}>{phase === "Cached" ? "Cached" : phase in modelDeploymentPhaseLabels ? modelDeploymentPhaseLabels[phase as keyof typeof modelDeploymentPhaseLabels] : phase}</span>)}
      </div>
      <FastStartRuntime spec={spec} view={view} />
      <dl className="definition-grid">
        <div><dt>Desired replicas</dt><dd>{observedValue(status.replicas?.desired)}</dd></div>
        <div><dt>Admitted replicas</dt><dd>{observedValue(status.replicas?.admitted)}</dd></div>
        <div><dt>Node pending</dt><dd>{observedValue(status.replicas?.node_pending)}</dd></div>
        <div><dt>Localizing</dt><dd>{observedValue(status.replicas?.localizing)}</dd></div>
        <div><dt>Runtime starting</dt><dd>{observedValue(status.replicas?.runtime_starting)}</dd></div>
        <div><dt>Warming</dt><dd>{observedValue(status.replicas?.warming)}</dd></div>
        <div><dt>Ready replicas</dt><dd>{observedValue(status.replicas?.ready)}</dd></div>
        <div><dt>Available replicas</dt><dd>{observedValue(status.replicas?.available)}</dd></div>
        <div><dt>Cache state</dt><dd>{status.cache?.state ?? "Unavailable"}</dd></div>
        <div><dt>Cache tier</dt><dd>{status.cache?.tier ?? "Unavailable"}</dd></div>
        <div><dt>OpenAI published</dt><dd>{status.publication ? status.publication.open_ai ? "Yes" : "No" : "Unavailable"}</dd></div>
        <div><dt>MCP published</dt><dd>{status.publication ? status.publication.mcp ? "Yes" : "No" : "Unavailable"}</dd></div>
        <div><dt>Observed endpoint</dt><dd>{status.endpoint ? `${status.endpoint.namespace}/${status.endpoint.service_name}:${status.endpoint.service_port}` : "Unavailable"}</dd></div>
        <div><dt>Primary admitted pool</dt><dd>{status.admitted_pool_ref ?? "Unavailable"}</dd></div>
        <div><dt>Eligible pools</dt><dd>{status.eligible_pool_refs.length ? status.eligible_pool_refs.join(", ") : "Unavailable"}</dd></div>
        <div><dt>Last reconcile</dt><dd>{formatTimestamp(status.last_reconcile_time)}</dd></div>
      </dl>
      {status.placements.length ? <div className="table-frame"><table className="resource-table"><caption className="sr-only">Observed hot and burst workload placements</caption><thead><tr><th scope="col">Workload</th><th scope="col">Role</th><th scope="col">Pool</th><th scope="col">Desired</th><th scope="col">Ready</th><th scope="col">Available</th></tr></thead><tbody>{status.placements.map((placement) => <tr key={placement.deployment_name}><th scope="row"><code>{placement.deployment_name}</code></th><td>{placement.role}</td><td><code>{placement.pool_ref}</code></td><td>{observedValue(placement.desired)}</td><td>{observedValue(placement.ready)}</td><td>{observedValue(placement.available)}</td></tr>)}</tbody></table></div> : null}
      {status.infrastructure_handoff ? <div className="inline-notice inline-notice--warning"><strong>{status.infrastructure_handoff.reason}</strong> Terraform inputs: {status.infrastructure_handoff.required_inputs.join(", ")}</div> : null}
      {status.conditions.length ? <div className="condition-list">{status.conditions.map((condition) => <div key={condition.type}><span className="mini-chip">{condition.type} · {condition.status}</span><strong>{condition.reason}</strong><span>{condition.message}</span></div>)}</div> : <p className="empty-copy">No controller conditions were published.</p>}
    </section>
  );
}

function History({ rows }: { rows: ModelDeploymentRevision[] }) {
  return (
    <section className="panel section-stack">
      <div className="section-heading"><div><span className="eyebrow">Append-only desired state</span><h2>Revision history</h2></div><span className="quiet-chip">{rows.length} loaded</span></div>
      {rows.length ? <div className="table-frame"><table className="resource-table resource-table--history"><caption className="sr-only">Model deployment revision history</caption><thead><tr><th scope="col">Revision</th><th scope="col">Action</th><th scope="col">Actor</th><th scope="col">Created</th><th scope="col">ETag</th></tr></thead><tbody>{rows.map((row) => <tr key={row.revision}><th scope="row">r{row.revision}</th><td>{row.action}</td><td>{row.created_by}</td><td>{formatTimestamp(row.created_at)}</td><td><code>{row.etag}</code></td></tr>)}</tbody></table></div> : <p className="empty-copy">No revision history was published.</p>}
    </section>
  );
}

export function ModelDeploymentWorkspacePage({ create = false }: { create?: boolean }) {
  const { deploymentName = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { session } = useSession();
  const namespaceFilter = searchParams.get("namespace") ?? "fs2-models";
  const tenantFilter = searchParams.get("tenant_id") ?? session.principal.tenant_id ?? "";
  const [name, setName] = useState(create ? "" : deploymentName);
  const [namespace, setNamespace] = useState(namespaceFilter);
  const [draft, setDraft] = useState<ModelDeploymentSpec | null>(create ? createEmptyModelDeploymentSpec(tenantFilter) : null);
  const [validation, setValidation] = useState<ModelDeploymentValidationDecision | null>(null);
  const [plan, setPlan] = useState<ModelDeploymentRenderPreview | null>(null);
  const [busy, setBusy] = useState<BusyAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [mutationResult, setMutationResult] = useState<ModelDeploymentMutationResult | null>(null);
  const [lastMutationAction, setLastMutationAction] = useState<MutationAction | null>(null);
  const [confirmation, setConfirmation] = useState<"drain" | "rollback" | null>(null);
  const [rollbackRevision, setRollbackRevision] = useState<number | "">("");
  const [, setPreviewFreshness] = useState(0);
  const idempotencyKeys = useRef<Partial<Record<Exclude<MutationAction, "reconcile">, string>>>({});
  const canMutate = rolePermits(session.principal.role, "operator");

  const identityQuery = useMemo(() => ({ namespace: namespaceFilter, tenantId: tenantFilter || undefined }), [namespaceFilter, tenantFilter]);
  const capabilitiesQuery = useQuery({
    queryKey: ["admin-model-deployment-mutation-capabilities"],
    queryFn: ({ signal }) => adminApi.modelDeploymentCapabilities(signal),
  });
  const currentQuery = useQuery({
    queryKey: ["admin-model-deployment", deploymentName, namespaceFilter, tenantFilter],
    queryFn: ({ signal }) => adminApi.modelDeployment(deploymentName, identityQuery, signal),
    enabled: !create && Boolean(deploymentName),
  });
  const statusQuery = useQuery({
    queryKey: ["admin-model-deployment-status", deploymentName, namespaceFilter, tenantFilter],
    queryFn: ({ signal }) => adminApi.modelDeploymentStatus(deploymentName, identityQuery, signal),
    enabled: !create && Boolean(deploymentName),
    refetchInterval: 10_000,
  });
  const historyQuery = useQuery({
    queryKey: ["admin-model-deployment-history", deploymentName, namespaceFilter, tenantFilter],
    queryFn: ({ signal }) => adminApi.modelDeploymentHistory(deploymentName, { ...identityQuery, limit: 100 }, signal),
    enabled: !create && Boolean(deploymentName),
  });
  const current = currentQuery.data?.data;
  const desiredRevision = current && (!mutationResult || current.revision >= mutationResult.revision.revision)
    ? current
    : mutationResult?.revision;
  const capabilities = capabilitiesQuery.data?.data;
  const configurationOptions = capabilitiesQuery.error ? [] : capabilities?.configuration_options ?? [];
  const selectedConfigurationOption = useMemo(
    () => configurationOptions.find((option) => option.model_ref === draft?.modelRef) ?? null,
    [configurationOptions, draft?.modelRef],
  );
  const createNeedsConfiguration = create && selectedConfigurationOption === null;
  const rollbackCandidates = useMemo(
    () => (historyQuery.data?.data.items ?? []).filter((row) => desiredRevision && row.revision < desiredRevision.revision),
    [desiredRevision, historyQuery.data],
  );

  useEffect(() => {
    if (!current) return;
    setName(current.name);
    setNamespace(current.namespace);
    setDraft(structuredClone(current.spec));
    setValidation(null);
    setPlan(null);
    setActionError(null);
    setConflict(false);
    setConfirmation(null);
    idempotencyKeys.current = {};
  }, [current?.etag]);

  useEffect(() => {
    setRollbackRevision((selected) => rollbackCandidates.some((row) => row.revision === selected)
      ? selected
      : rollbackCandidates[0]?.revision ?? "");
  }, [rollbackCandidates]);

  useEffect(() => {
    if (!plan) return;
    const remaining = Date.parse(plan.expires_at) - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0 || remaining > 2_147_483_000) return;
    const timer = window.setTimeout(() => setPreviewFreshness((value) => value + 1), remaining + 25);
    return () => window.clearTimeout(timer);
  }, [plan?.expires_at]);

  function changed(next: ModelDeploymentSpec) {
    setDraft(next);
    setValidation(null);
    setPlan(null);
    setActionError(null);
    setConfirmation(null);
    delete idempotencyKeys.current.apply;
    if (create) setConflict(false);
  }

  function changeIdentity(field: "name" | "namespace", value: string) {
    field === "name" ? setName(value) : setNamespace(value);
    setValidation(null);
    setPlan(null);
    setActionError(null);
    setConfirmation(null);
    delete idempotencyKeys.current.apply;
    if (create) setConflict(false);
  }

  function selectConfigurationOption(modelRef: string) {
    if (!draft) return;
    const option = configurationOptions.find((candidate) => candidate.model_ref === modelRef);
    if (!option) return;
    const previousSuggestion = selectedConfigurationOption?.suggested_name;
    setName((currentName) => !currentName || currentName === previousSuggestion ? option.suggested_name : currentName);
    setNamespace(option.namespace);
    changed(draftFromConfigurationOption(draft, option));
  }

  async function refreshCurrent() {
    const result = await currentQuery.refetch();
    if (!result.data) return;
    const refreshed = result.data.data;
    setName(refreshed.name);
    setNamespace(refreshed.namespace);
    setDraft(structuredClone(refreshed.spec));
    setValidation(null);
    setPlan(null);
    setMutationResult(null);
    setLastMutationAction(null);
    setActionError(null);
    setConflict(false);
  }

  function proposal() {
    if (!draft) throw new Error("Model deployment draft is unavailable.");
    const problem = localModelDeploymentProblem(name, namespace, draft);
    if (problem) throw new Error(problem);
    return { name, namespace, base_etag: desiredRevision?.etag ?? null, spec: draft };
  }

  function failed(caught: unknown, fallback = "Model deployment request failed.") {
    if (caught instanceof AdminApiError && caught.status === 409) {
      setConflict(true);
      setActionError("This desired revision changed on the server. Refresh it before retrying the request.");
      return;
    }
    setActionError(caught instanceof Error ? caught.message : fallback);
  }

  async function validateDraft() {
    setBusy("validate"); setActionError(null);
    try {
      const result = await adminApi.validateModelDeployment(proposal());
      setValidation(result.data.decision);
      setPlan(null);
    } catch (caught) { failed(caught); } finally { setBusy(null); }
  }

  async function previewPlan() {
    setBusy("plan"); setActionError(null);
    try {
      const result = await adminApi.planModelDeployment(proposal());
      setValidation(result.data.decision);
      setPlan(result.data);
    } catch (caught) { failed(caught); } finally { setBusy(null); }
  }

  function idempotencyKey(action: Exclude<MutationAction, "reconcile">): string {
    const existing = idempotencyKeys.current[action];
    if (existing) return existing;
    const created = newIdempotencyKey(action);
    idempotencyKeys.current[action] = created;
    return created;
  }

  async function refreshReadSurface() {
    if (create) return;
    await Promise.allSettled([currentQuery.refetch(), historyQuery.refetch(), statusQuery.refetch()]);
  }

  async function completedMutation(action: MutationAction, result: ModelDeploymentMutationResult) {
    setMutationResult(result);
    setLastMutationAction(action);
    setName(result.revision.name);
    setNamespace(result.revision.namespace);
    setDraft(structuredClone(result.revision.spec));
    setValidation(null);
    setConflict(false);
    setConfirmation(null);
    if (action !== "reconcile") delete idempotencyKeys.current[action];
    if (action === "apply") setPlan(null);
    await refreshReadSurface();
  }

  async function applyPlan() {
    if (!plan) return;
    setBusy("apply"); setActionError(null);
    try {
      const result = await adminApi.applyModelDeployment({
        preview_id: plan.preview_id,
        proposed_etag: plan.proposed_etag,
        proposal: proposal(),
        idempotency_key: idempotencyKey("apply"),
      });
      await completedMutation("apply", result.data);
    } catch (caught) { failed(caught, "Applying the desired revision failed."); } finally { setBusy(null); }
  }

  async function drainDeployment() {
    if (!desiredRevision || create) return;
    setBusy("drain"); setActionError(null);
    try {
      const result = await adminApi.drainModelDeployment(desiredRevision.name, {
        base_etag: desiredRevision.etag,
        idempotency_key: idempotencyKey("drain"),
      });
      await completedMutation("drain", result.data);
    } catch (caught) { failed(caught, "Draining the model deployment failed."); } finally { setBusy(null); }
  }

  async function rollbackDeployment() {
    if (!desiredRevision || create || rollbackRevision === "") return;
    setBusy("rollback"); setActionError(null);
    try {
      const result = await adminApi.rollbackModelDeployment(desiredRevision.name, {
        target_revision: rollbackRevision,
        base_etag: desiredRevision.etag,
        idempotency_key: idempotencyKey("rollback"),
      });
      await completedMutation("rollback", result.data);
    } catch (caught) { failed(caught, "Rolling back the model deployment failed."); } finally { setBusy(null); }
  }

  async function reconcileDeployment() {
    const revision = mutationResult?.projection === "pending" ? mutationResult.revision : desiredRevision;
    if (!revision) return;
    setBusy("reconcile"); setActionError(null);
    try {
      const result = await adminApi.reconcileModelDeployment(revision.name, { expected_etag: revision.etag });
      await completedMutation("reconcile", result.data);
    } catch (caught) { failed(caught, "Retrying Kubernetes projection failed."); } finally { setBusy(null); }
  }

  const returnSearch = sharedContextParams(searchParams);
  returnSearch.set("namespace", namespaceFilter);
  if (tenantFilter) returnSearch.set("tenant_id", tenantFilter);

  if (!create && (currentQuery.isPending || currentQuery.error || !currentQuery.data)) {
    return <div className="page-stack"><Link className="back-link" to={{ pathname: "/admin/model-deployments", search: returnSearch.toString() }}>← Model deployments</Link><DataBoundary data={currentQuery.data} error={currentQuery.error} pending={currentQuery.isPending}>{() => null}</DataBoundary></div>;
  }
  if (!draft) return <div className="state-panel state-panel--loading">Loading model deployment draft…</div>;

  const gate = (capability: ModelDeploymentActionCapability | undefined) => capabilityReason(
    capability,
    capabilitiesQuery.isPending,
    Boolean(capabilitiesQuery.error),
    canMutate,
  );
  const pendingProjection = mutationResult?.projection === "pending";
  const planExpiry = plan ? Date.parse(plan.expires_at) : Number.NaN;
  const planMatchesCurrentIdentity = Boolean(plan
    && plan.name === name
    && plan.namespace === namespace
    && plan.base_etag === (desiredRevision?.etag ?? null)
    && plan.proposed_etag === plan.decision.specDigest);
  const applyReason = gate(capabilities?.declarative_apply)
    ?? (pendingProjection ? "Retry the pending Kubernetes projection before applying another revision." : null)
    ?? (!plan ? "Create a current render preview before applying." : null)
    ?? (!planMatchesCurrentIdentity ? "The render preview no longer matches the current draft identity or base ETag." : null)
    ?? (plan && (plan.decision.disposition !== "accepted" || !plan.render) ? "Only an accepted, rendered plan can be applied." : null)
    ?? (plan && (!Number.isFinite(planExpiry) || planExpiry <= Date.now()) ? "This render preview expired or has an invalid expiry; create a new plan." : null)
    ?? (conflict ? "Refresh the current revision before applying." : null);
  const drainReason = gate(capabilities?.drain)
    ?? (pendingProjection ? "Retry the pending Kubernetes projection before draining." : null)
    ?? (create || !desiredRevision ? "Drain is available from an existing deployment workspace." : null)
    ?? (desiredRevision?.spec.lifecycle.desiredState === "Draining" ? "This deployment is already draining." : null)
    ?? (conflict ? "Refresh the current revision before draining." : null);
  const rollbackReason = gate(capabilities?.rollback)
    ?? (pendingProjection ? "Retry the pending Kubernetes projection before rolling back." : null)
    ?? (create || !desiredRevision ? "Rollback is available after the deployment has revision history." : null)
    ?? (rollbackRevision === "" ? "No earlier revision is loaded for rollback." : null)
    ?? (conflict ? "Refresh the current revision before rolling back." : null);
  const reconcileReason = gate(capabilities?.reconcile)
    ?? (!(mutationResult?.projection === "pending" ? mutationResult.revision : desiredRevision) ? "No durable revision is available to project." : null)
    ?? (conflict ? "Refresh the current revision before retrying projection." : null);
  const deleteReason = capabilities?.hard_delete.enabled
    ? "The server advertised hard delete, but the v1 API has no reviewed hard-delete route; this console fails closed."
    : gate(capabilities?.hard_delete) ?? "Hard deletion is unavailable in the v1 mutation API.";
  const capabilitiesSummary = !canMutate
    ? "Viewer role is read-only; an operator or administrator is required for mutations."
    : capabilitiesQuery.isPending
      ? "Loading server-authoritative mutation capabilities. All controls remain disabled."
      : capabilitiesQuery.error || !capabilities
        ? "Mutation capabilities are unavailable. All controls fail closed."
        : "Actions are enabled only when the server advertises them and their revision prerequisites are satisfied.";

  return (
    <div className="page-stack model-deployment-page">
      <Link className="back-link" to={{ pathname: "/admin/model-deployments", search: returnSearch.toString() }}>← Model deployments</Link>
      <section className="identity-panel">
        <div><span className="eyebrow">{create && !mutationResult ? "New desired state" : `Desired revision ${desiredRevision?.revision}`}</span><h2>{name || "Untitled model deployment"}</h2><code>{namespace}/{draft.modelRef || "model-reference-pending"}</code></div>
        <span className="mini-chip">{draft.lifecycle.desiredState}</span>
      </section>

      <section className="panel model-deployment-action-bar" aria-label="Model deployment actions">
        <div><strong>{create ? "Draft and preview" : "Edit and preview"}</strong><span>Validation is read-only. Planning is available to operators and uses the current ETag.</span></div>
        <div className="configuration-actions">
          <button className="button" disabled={busy !== null || conflict || createNeedsConfiguration} onClick={() => void validateDraft()} title={createNeedsConfiguration ? "Select a server-qualified model first" : undefined} type="button">{busy === "validate" ? "Validating…" : "Validate draft"}</button>
          <button className="button button--primary" disabled={!canMutate || busy !== null || conflict || createNeedsConfiguration} onClick={() => void previewPlan()} title={!canMutate ? "Operator role required" : createNeedsConfiguration ? "Select a server-qualified model first" : undefined} type="button">{busy === "plan" ? "Planning…" : "Preview render plan"}</button>
        </div>
      </section>
      {!canMutate ? <div className="inline-notice inline-notice--warning" role="status"><strong>Viewer mode.</strong> You may validate this draft; an operator or administrator is required to create a render preview or mutate desired state.</div> : null}
      {actionError ? <div className="inline-notice inline-notice--error" role="alert"><strong>Request failed.</strong> {actionError}{conflict && !create ? <button className="button" onClick={() => void refreshCurrent()} type="button">Refresh current revision</button> : null}</div> : null}

      {create ? (
        <section className="panel section-stack" aria-labelledby="qualified-model-title">
          <div className="section-heading">
            <div><span className="eyebrow">Installed configuration</span><h2 id="qualified-model-title">Qualified model</h2><p>Select an exact model, runtime, artifact and accelerator tuple published by this control plane.</p></div>
            {capabilities ? <code>{capabilities.configuration_revision}</code> : null}
          </div>
          <label className="compact-field">Qualified model
            <select
              aria-label="Qualified model"
              disabled={busy !== null || capabilitiesQuery.isPending || Boolean(capabilitiesQuery.error) || configurationOptions.length === 0}
              onChange={(event) => selectConfigurationOption(event.target.value)}
              value={selectedConfigurationOption?.model_ref ?? draft.modelRef}
            >
              <option value="">{capabilitiesQuery.isPending ? "Loading qualified models…" : capabilitiesQuery.error ? "Qualified models unavailable" : configurationOptions.length === 0 ? "No qualified models available" : "Select a qualified model"}</option>
              {draft.modelRef && !selectedConfigurationOption ? <option value={draft.modelRef}>{draft.modelRef} · no longer available</option> : null}
              {configurationOptions.map((option) => <option key={option.model_ref} value={option.model_ref}>{option.model_ref}</option>)}
            </select>
          </label>
          {capabilitiesQuery.error ? <div className="inline-notice inline-notice--error" role="status">Qualified model choices are unavailable. This draft remains disabled until the server-authoritative configuration can be read.</div> : capabilities && configurationOptions.length === 0 ? <div className="inline-notice inline-notice--warning" role="status">This cluster currently advertises no complete qualified model configurations.</div> : selectedConfigurationOption ? <p className="supporting-copy">{selectedConfigurationOption.pool_choices.length} compatible pool{selectedConfigurationOption.pool_choices.length === 1 ? "" : "s"}; scale-to-zero {selectedConfigurationOption.scale_to_zero_qualified ? "qualified" : "not qualified"}. Changing the model replaces only its qualified runtime material and preserves operator policy.</p> : <p className="supporting-copy">Choose a model before editing or validating the desired policy.</p>}
        </section>
      ) : null}

      <ModelDeploymentForm configurationOption={selectedConfigurationOption} disabled={busy !== null || createNeedsConfiguration} identityLocked={!create} name={name} namespace={namespace} onChange={changed} onNameChange={(value) => changeIdentity("name", value)} onNamespaceChange={(value) => changeIdentity("namespace", value)} spec={draft} />

      {validation ? <DecisionPanel decision={validation} heading="Validation decision" /> : null}
      {plan ? <RenderPanel preview={plan} /> : null}
      {mutationResult && lastMutationAction ? <MutationResultPanel action={lastMutationAction} result={mutationResult} /> : null}

      <section className="panel mutation-gate" aria-labelledby="mutation-title">
        <div><span className="eyebrow">Capability and role gate</span><h2 id="mutation-title">Cluster mutation controls</h2><p>{capabilitiesSummary}</p>{capabilities ? <code>{capabilities.schema_version}</code> : null}</div>
        <div className="mutation-controls">
          {!create && rollbackCandidates.length ? <label className="compact-field">Rollback target revision<select aria-label="Rollback target revision" disabled={busy !== null || Boolean(gate(capabilities?.rollback))} onChange={(event) => setRollbackRevision(Number(event.target.value))} value={rollbackRevision}>{rollbackCandidates.map((row) => <option key={row.revision} value={row.revision}>r{row.revision} · {row.action}</option>)}</select></label> : null}
          <div className="configuration-actions">
            <button className="button button--primary" disabled={busy !== null || Boolean(applyReason)} onClick={() => void applyPlan()} title={applyReason ?? undefined} type="button">{busy === "apply" ? "Applying…" : "Apply"}</button>
            <button className="button" disabled={busy !== null || Boolean(drainReason)} onClick={() => setConfirmation("drain")} title={drainReason ?? undefined} type="button">Drain</button>
            <button className="button" disabled={busy !== null || Boolean(rollbackReason)} onClick={() => setConfirmation("rollback")} title={rollbackReason ?? undefined} type="button">Rollback</button>
            <button className="button" disabled={busy !== null || Boolean(reconcileReason)} onClick={() => void reconcileDeployment()} title={reconcileReason ?? undefined} type="button">{busy === "reconcile" ? "Retrying projection…" : pendingProjection ? "Retry Kubernetes projection" : "Reconcile"}</button>
            <button className="button button--danger" disabled title={deleteReason} type="button">Delete</button>
          </div>
        </div>
      </section>
      {confirmation ? <section className="inline-notice inline-notice--warning mutation-confirmation" aria-label={`Confirm ${confirmation}`}>
        <div><strong>Confirm {confirmation}</strong><span>{confirmation === "drain" ? "This commits a Draining revision with a zero hot floor. Existing revision history is retained." : `This commits revision r${rollbackRevision} as a new desired revision. Existing history is retained.`}</span></div>
        <div className="configuration-actions">
          <button className="button button--primary" disabled={busy !== null} onClick={() => void (confirmation === "drain" ? drainDeployment() : rollbackDeployment())} type="button">Confirm {confirmation}</button>
          <button className="button" disabled={busy !== null} onClick={() => setConfirmation(null)} type="button">Cancel</button>
        </div>
      </section> : null}

      {!create ? (
        <>
          <DataBoundary data={statusQuery.data} error={statusQuery.error} pending={statusQuery.isPending}>{({ data }) => <RuntimeStatus expectedRevision={desiredRevision?.revision ?? data.revision} spec={desiredRevision?.spec ?? draft} view={data} />}</DataBoundary>
          <DataBoundary data={historyQuery.data} error={historyQuery.error} pending={historyQuery.isPending}>{({ data }) => <History rows={data.items} />}</DataBoundary>
        </>
      ) : mutationResult ? <div className="state-panel"><strong>The first durable revision was created</strong><span>Open the deployment from the list to follow controller status and revision history.</span></div> : <div className="state-panel"><strong>No observed state or history yet</strong><span>These views become available only after a durable revision exists and the controller publishes an independent observation.</span></div>}
    </div>
  );
}
