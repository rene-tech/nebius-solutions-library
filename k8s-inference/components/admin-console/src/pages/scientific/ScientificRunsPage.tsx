import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../../api/client";
import type {
  ScientificAccessState,
  ScientificRunState,
  ScientificServiceClass,
} from "../../api/scientificTypes";
import { useSession } from "../../auth/SessionContext";
import { DataBoundary } from "../../components/DataBoundary";
import { formatTimestamp } from "../../lib/format";
import { sharedContextParams } from "../../lib/search";
import {
  AccessGate,
  FastStartTier,
  ScientificMeasurement,
  ScientificStatusChip,
  shortDigest,
} from "./ScientificPresentation";
import { useScientificCapabilities } from "./useScientificCapabilities";

const runStates = ["waiting-for-access", "queued", "admitted", "running", "succeeded", "failed", "cancelling", "cancelled"] as const satisfies readonly ScientificRunState[];
const serviceClasses = ["presentation", "interactive", "customer-batch", "bulk-backfill"] as const satisfies readonly ScientificServiceClass[];
const accessStates = ["not-required", "unverified", "verified", "blocked"] as const satisfies readonly ScientificAccessState[];
const runStateSet = new Set<string>(runStates);
const serviceClassSet = new Set<string>(serviceClasses);
const accessStateSet = new Set<string>(accessStates);

function bounded(value: string | null, maximum: number, pattern?: RegExp): string | undefined {
  if (!value || value.length > maximum || (pattern && !pattern.test(value))) return undefined;
  return value;
}

export function ScientificRunsPage() {
  const { session } = useSession();
  const [searchParams, setSearchParams] = useSearchParams();
  const context = sharedContextParams(searchParams);
  const capabilitiesQuery = useScientificCapabilities(context);
  const capabilities = capabilitiesQuery.data?.data;
  const runsAvailable = capabilities?.run_history.available === true;
  const modelsAvailable = capabilities?.model_readiness.available === true;
  const fixedTenant = session.principal.tenant_id ?? undefined;
  const rawStatus = searchParams.get("run_status");
  const rawServiceClass = searchParams.get("service_class");
  const rawAccessState = searchParams.get("access_state");
  const status = rawStatus && runStateSet.has(rawStatus) ? rawStatus as ScientificRunState : undefined;
  const serviceClass = rawServiceClass && serviceClassSet.has(rawServiceClass) ? rawServiceClass as ScientificServiceClass : undefined;
  const accessState = rawAccessState && accessStateSet.has(rawAccessState) ? rawAccessState as ScientificAccessState : undefined;
  const cursor = bounded(searchParams.get("cursor"), 512);
  const tenantId = fixedTenant ?? bounded(searchParams.get("tenant"), 120, /^[A-Za-z0-9][A-Za-z0-9_.-]*$/);
  const modelId = bounded(searchParams.get("model"), 128, /^[A-Za-z0-9][A-Za-z0-9_.-]*$/);
  const invalidFilter = (rawStatus !== null && status === undefined)
    || (rawServiceClass !== null && serviceClass === undefined)
    || (rawAccessState !== null && accessState === undefined)
    || (searchParams.has("cursor") && cursor === undefined)
    || (!fixedTenant && searchParams.has("tenant") && tenantId === undefined)
    || (searchParams.has("model") && modelId === undefined);

  const navigationParams = new URLSearchParams(context);
  if (!fixedTenant && tenantId) navigationParams.set("tenant", tenantId);
  if (modelId) navigationParams.set("model", modelId);
  if (status) navigationParams.set("run_status", status);
  if (serviceClass) navigationParams.set("service_class", serviceClass);
  if (accessState) navigationParams.set("access_state", accessState);
  if (cursor) navigationParams.set("cursor", cursor);

  const runsQuery = useQuery({
    queryKey: ["admin-scientific-runs", context.toString(), cursor ?? "first", tenantId ?? "all-tenants", modelId ?? "all-models", status ?? "all-states", serviceClass ?? "all-classes", accessState ?? "all-access"],
    queryFn: ({ signal }) => adminApi.scientificRuns(context, {
      cursor,
      tenantId,
      modelId,
      status,
      serviceClass,
      accessState,
      limit: 100,
    }, signal),
    enabled: runsAvailable,
  });
  const modelsQuery = useQuery({
    queryKey: ["admin-scientific-models", context.toString()],
    queryFn: ({ signal }) => adminApi.scientificModels(context, signal),
    enabled: modelsAvailable,
  });

  function update(key: string, value: string) {
    const next = new URLSearchParams(navigationParams);
    value ? next.set(key, value) : next.delete(key);
    next.delete("cursor");
    setSearchParams(next, { replace: true });
  }

  function nextPage(nextCursor: string) {
    const next = new URLSearchParams(navigationParams);
    next.set("cursor", nextCursor);
    setSearchParams(next);
  }

  const runs = runsQuery.data?.data.items ?? [];
  const models = modelsQuery.data?.data.items ?? [];
  return (
    <div className="page-stack scientific-page">
      <section className="panel scientific-intro" aria-labelledby="scientific-runs-intro-title">
        <div>
          <span className="eyebrow">Scientific operations</span>
          <h2 id="scientific-runs-intro-title">Batch execution and exact GPU evidence</h2>
          <p>Run identity, access admission, DAG progress, artifacts, and lifecycle accounting remain separate facts. Estimated and unavailable values are always labelled.</p>
        </div>
        <span className="quiet-chip">Read-only</span>
      </section>

      <section className="section-stack" aria-labelledby="scientific-run-list-title">
        <div className="section-heading">
          <div><span className="eyebrow">Runs</span><h2 id="scientific-run-list-title">Scientific run ledger</h2></div>
          <span className="section-heading__meta">{runs.length} runs on this page</span>
        </div>
        {capabilitiesQuery.isPending ? (
          <div className="state-panel state-panel--loading" role="status">Checking scientific run capability…</div>
        ) : runsAvailable ? <>
        <div className="toolbar toolbar--wrap" aria-label="Scientific run filters">
          <label>Model<input maxLength={128} onChange={(event) => update("model", event.target.value)} placeholder="All models" value={modelId ?? ""} /></label>
          <label>Run status<select onChange={(event) => update("run_status", event.target.value)} value={status ?? ""}><option value="">All states</option>{runStates.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label>Service class<select onChange={(event) => update("service_class", event.target.value)} value={serviceClass ?? ""}><option value="">All classes</option>{serviceClasses.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label>Access state<select onChange={(event) => update("access_state", event.target.value)} value={accessState ?? ""}><option value="">All access states</option>{accessStates.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          {fixedTenant ? <span className="quiet-chip">Tenant {fixedTenant}</span> : <label>Tenant<input maxLength={120} onChange={(event) => update("tenant", event.target.value)} placeholder="All tenants" value={tenantId ?? ""} /></label>}
        </div>
        {invalidFilter ? <div className="freshness-notice" role="status"><strong>Invalid filter ignored</strong><span>Use a published run, service-class, or access state and bounded model, tenant, or cursor value.</span></div> : null}
        <DataBoundary data={runsQuery.data} error={runsQuery.error} pending={runsQuery.isPending} empty={!runsQuery.isPending && runs.length === 0} loadingLabel="Loading scientific runs…" emptyLabel="No scientific runs match the selected context.">
          {({ data }) => (
            <div className="page-stack">
              <div className="table-frame">
                <table className="resource-table resource-table--scientific-runs">
                  <caption className="sr-only">Scientific runs with attribution, admission, access, and GPU accounting</caption>
                  <thead><tr><th scope="col">Run</th><th scope="col">Model and backend</th><th scope="col">Attribution</th><th scope="col">Service class</th><th scope="col">Queue and admission</th><th scope="col">Status and stages</th><th scope="col">GPU accounting</th><th scope="col">Access</th></tr></thead>
                  <tbody>{data.items.map((run) => {
                    const completedStages = run.stage_counts.succeeded + run.stage_counts.skipped + run.stage_counts.cancelled + run.stage_counts.failed;
                    const allStages = Object.values(run.stage_counts).reduce((total, count) => total + count, 0);
                    return (
                      <tr key={run.id}>
                        <th scope="row"><Link className="resource-link" to={{ pathname: `/admin/scientific-runs/${encodeURIComponent(run.id)}`, search: navigationParams.toString() }}>{run.display_name}</Link><span className="secondary-line">{run.id} · {run.operation}</span><span className="secondary-line">Submitted {formatTimestamp(run.submitted_at)}</span></th>
                        <td><strong>{run.model.display_name}</strong><span className="secondary-line">{run.model.execution_mode} · {run.model.backend.backend_id}</span><code className="scientific-digest" title={run.model.backend.execution_identity_digest ?? undefined}>{shortDigest(run.model.backend.execution_identity_digest)}</code></td>
                        <td>{run.attribution.user_id}<span className="secondary-line">{run.attribution.principal_id} · key {run.attribution.api_key_prefix}</span><span className="secondary-line">{run.attribution.tenant_id}</span></td>
                        <td>{run.service_class.effective}<span className="secondary-line">requested {run.service_class.requested}</span>{run.service_class.requested !== run.service_class.effective ? <span className="scientific-decision">Policy changed class</span> : null}</td>
                        <td>{run.queue.tenant_queue}<span className="secondary-line">{run.queue.local_queue} → {run.queue.cluster_queue}</span><ScientificStatusChip state={run.queue.admission_state === "finished" ? "succeeded" : run.queue.admission_state === "inadmissible" ? "blocked" : run.queue.admission_state} label={run.queue.admission_state} reason={run.queue.admission_reason} /></td>
                        <td><ScientificStatusChip state={run.status} reason={run.error?.message ?? `Run is ${run.status}.`} /><span className="secondary-line">{completedStages} / {allStages} stages terminal</span><FastStartTier observation={run.fast_start} /></td>
                        <td><span className="scientific-accounting-line">Allocated <ScientificMeasurement compact value={run.gpu_accounting.allocated} /></span><span className="scientific-accounting-line">Active <ScientificMeasurement compact value={run.gpu_accounting.active} /></span><span className="scientific-accounting-line">Idle <ScientificMeasurement compact value={run.gpu_accounting.idle_total} /></span><span className="scientific-accounting-line">Grace <ScientificMeasurement compact value={run.gpu_accounting.grace_drain} /></span></td>
                        <td><AccessGate access={run.access} compact /></td>
                      </tr>
                    );
                  })}</tbody>
                </table>
              </div>
              {data.next_cursor ? <div className="pagination-actions"><span>More scientific runs are available.</span><button className="button" onClick={() => nextPage(data.next_cursor as string)} type="button">Next page</button></div> : <p className="pagination-summary">End of the selected scientific run window.</p>}
            </div>
          )}
        </DataBoundary>
        </> : (
          <div className="state-panel" role="status">
            <strong>Scientific run history is not enabled</strong>
            <span>{capabilities?.run_history.reason ?? capabilitiesQuery.error?.message ?? "No durable scientific run reader is configured."}</span>
          </div>
        )}
      </section>

      <section className="section-stack" aria-labelledby="scientific-model-readiness-title">
        <div className="section-heading">
          <div><span className="eyebrow">Model-level contract</span><h2 id="scientific-model-readiness-title">Scientific model readiness</h2></div>
          <span className="section-heading__meta">{models.length} model backends</span>
        </div>
        {capabilitiesQuery.isPending ? (
          <div className="state-panel state-panel--loading" role="status">Checking scientific model capability…</div>
        ) : modelsAvailable ? <DataBoundary data={modelsQuery.data} error={modelsQuery.error} pending={modelsQuery.isPending} empty={!modelsQuery.isPending && models.length === 0} loadingLabel="Loading scientific model readiness…" emptyLabel="No scientific model readiness records are available.">
          {({ data }) => (
            <div className="page-stack">
              {data.projection_issues.length ? (
                <div className="freshness-notice" role="status">
                  <strong>{data.projection_issues.length} catalog projection issue{data.projection_issues.length === 1 ? "" : "s"}</strong>
                  <span>Invalid evidence was isolated to its candidate; other model rows remain available.</span>
                </div>
              ) : null}
              <div className="table-frame">
              <table className="resource-table resource-table--scientific-models">
                <caption className="sr-only">Scientific model batch, hybrid, access, backend, and caching readiness</caption>
                <thead><tr><th scope="col">Model</th><th scope="col">Readiness</th><th scope="col">Execution</th><th scope="col">Access gate</th><th scope="col">Backend identity</th><th scope="col">Exact caching state</th></tr></thead>
                <tbody>{data.items.map((model) => (
                  <tr key={model.candidate_id}>
                    <th scope="row">{model.display_name}<span className="secondary-line">{model.model_id} · candidate {model.candidate_id}</span></th>
                    <td><ScientificStatusChip state={model.readiness} reason={model.readiness_reason} /><span className="secondary-line scientific-secondary">{model.readiness_reason}</span>{model.missing_evidence.length ? <span className="secondary-line scientific-secondary">Missing {model.missing_evidence.join(", ")}</span> : null}</td>
                    <td>{model.execution_mode ?? "Not published"}<span className="secondary-line">Batch {model.batch_supported ? "supported" : "unsupported"} · Interactive {model.interactive_supported === null ? "unknown" : model.interactive_supported ? "supported" : "unsupported"}</span><span className="secondary-line scientific-secondary">{model.service_classes.join(", ") || "No service classes published"}</span></td>
                    <td><AccessGate access={model.access} compact /></td>
                    <td>
                      {model.backend.backend_id}
                      <span className="secondary-line scientific-secondary">Deployed / pinned revision</span>
                      <span className="secondary-line">{model.backend.source_repository}@{shortDigest(model.backend.source_revision)}</span>
                      <code className="scientific-digest" title={model.backend.runtime_image_digest ?? undefined}>{shortDigest(model.backend.runtime_image_digest)}</code>
                      {model.available_upgrade ? (
                        <>
                          <span className="secondary-line scientific-secondary">Available upstream revision · unqualified</span>
                          <code className="scientific-digest" title={model.available_upgrade.source_revision}>{shortDigest(model.available_upgrade.source_revision)}</code>
                        </>
                      ) : null}
                    </td>
                    <td><span className="scientific-tier scientific-tier--declared">{model.caching.exact_tier}</span><span className="secondary-line scientific-secondary">Image {model.caching.image} · artifacts {model.caching.artifacts} · references {model.caching.reference_data}</span><span className="secondary-line scientific-secondary">Checkpoint {model.caching.runtime_checkpoint} · GPU snapshot {model.caching.gpu_snapshot}</span><span className="secondary-line scientific-secondary">{model.caching.reason}</span></td>
                  </tr>
                ))}</tbody>
              </table>
              </div>
            </div>
          )}
        </DataBoundary> : (
          <div className="state-panel" role="status">
            <strong>Scientific model readiness is not enabled</strong>
            <span>{capabilities?.model_readiness.reason ?? capabilitiesQuery.error?.message ?? "No scientific catalog reader is configured."}</span>
          </div>
        )}
      </section>
    </div>
  );
}
