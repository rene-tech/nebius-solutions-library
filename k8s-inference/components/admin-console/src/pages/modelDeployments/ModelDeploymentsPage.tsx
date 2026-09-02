import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi, AdminApiError } from "../../api/client";
import type {
  ModelDeploymentRevision,
  ModelDeploymentRuntimePhase,
  ModelDeploymentStatusView,
} from "../../api/modelDeploymentTypes";
import { DataBoundary } from "../../components/DataBoundary";
import {
  effectiveFastStartLevel,
  fastStartLevelLabel,
  fastStartPolicySummary,
  fastStartTarget,
  modelDeploymentPhaseLabels,
  normalizedFastStartStatus,
  statusPhase,
} from "../../lib/modelDeployment";
import { formatTimestamp } from "../../lib/format";
import { sharedContextParams } from "../../lib/search";

function phaseClass(phase: ModelDeploymentRuntimePhase | null, availability: ModelDeploymentStatusView["state"]) {
  if (availability === "unavailable") return "capability-chip--unavailable";
  if (availability === "stale") return "capability-chip--stale";
  if (phase === "Ready") return "capability-chip--healthy";
  if (phase === "Failed") return "capability-chip--unhealthy";
  if (phase === "InfrastructureRequired") return "capability-chip--degraded";
  return "capability-chip--unknown";
}

function ObservedState({ deployment }: { deployment: ModelDeploymentRevision }) {
  const query = useQuery({
    queryKey: ["admin-model-deployment-status", deployment.namespace, deployment.name, deployment.tenant_id],
    queryFn: ({ signal }) => adminApi.modelDeploymentStatus(deployment.name, {
      namespace: deployment.namespace,
      tenantId: deployment.tenant_id,
    }, signal),
  });
  if (query.isPending) return <span className="capability-chip capability-chip--unknown">Checking</span>;
  if (query.isError || !query.data) {
    return <span className="dense-stack"><span className="capability-chip capability-chip--unavailable">Unavailable</span><span>Status API did not publish an observation.</span></span>;
  }
  const view = query.data.data;
  const phase = statusPhase(view);
  const label = phase ? modelDeploymentPhaseLabels[phase] : "Unavailable";
  const reason = view.reason ?? (view.state === "stale" ? "Observation does not match this desired revision." : null);
  return (
    <span className="dense-stack">
      <span className={`capability-chip ${phaseClass(phase, view.state)}`}>{label}</span>
      <span>{view.state === "stale" ? `Stale · ${reason ?? "reason unavailable"}` : reason ?? `Revision ${view.revision} observed`}</span>
    </span>
  );
}

function ObservedFastStart({ deployment }: { deployment: ModelDeploymentRevision }) {
  const query = useQuery({
    queryKey: ["admin-model-deployment-status", deployment.namespace, deployment.name, deployment.tenant_id],
    queryFn: ({ signal }) => adminApi.modelDeploymentStatus(deployment.name, {
      namespace: deployment.namespace,
      tenantId: deployment.tenant_id,
    }, signal),
  });
  const requested = fastStartPolicySummary(deployment.spec.fastStart);
  if (query.isPending) return <span className="dense-stack"><strong>{requested}</strong><span>Checking assignment</span></span>;
  if (query.isError || !query.data) return <span className="dense-stack"><strong>{requested}</strong><span>Observed qualification unavailable</span></span>;
  const view = query.data.data;
  const observed = normalizedFastStartStatus(view);
  const effective = effectiveFastStartLevel(view);
  return (
    <span className="dense-stack">
      <strong>{effective ? fastStartLevelLabel(effective) : "Effective unavailable"}</strong>
      <span>Requested {requested}</span>
      <span>{observed?.assignedLevel ? `Assigned ${observed.assignedLevel} · ${fastStartTarget(observed.assignedLevel)}` : "Assignment unavailable"}</span>
      <span>{observed?.qualifiedLevel ? `Qualified ${observed.qualifiedLevel}` : "Qualification unavailable"}</span>
    </span>
  );
}

function rowLink(deployment: ModelDeploymentRevision, searchParams: URLSearchParams) {
  const query = sharedContextParams(searchParams);
  query.set("namespace", deployment.namespace);
  query.set("tenant_id", deployment.tenant_id);
  return { pathname: `/admin/model-deployments/${encodeURIComponent(deployment.name)}`, search: query.toString() };
}

export function ModelDeploymentsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedNamespace = searchParams.get("namespace") ?? "fs2-models";
  const namespace = requestedNamespace.length <= 63 ? requestedNamespace : "fs2-models";
  const requestedTenant = searchParams.get("tenant_id") ?? "";
  const tenantId = requestedTenant.length <= 120 ? requestedTenant : "";
  const after = searchParams.get("after") ?? undefined;
  const query = useQuery({
    queryKey: ["admin-model-deployments", namespace, tenantId, after],
    queryFn: ({ signal }) => adminApi.modelDeployments({
      namespace,
      tenantId: tenantId || undefined,
      after,
      limit: 100,
    }, signal),
  });
  const items = query.data?.data.items ?? [];

  function updateFilter(key: "namespace" | "tenant_id", value: string) {
    const next = new URLSearchParams(searchParams);
    value ? next.set(key, value) : next.delete(key);
    next.delete("after");
    setSearchParams(next, { replace: true });
  }

  const createParams = sharedContextParams(searchParams);
  createParams.set("namespace", namespace);
  if (tenantId) createParams.set("tenant_id", tenantId);

  return (
    <div className="page-stack model-deployment-page">
      <section className="panel model-deployment-intro">
        <div>
          <span className="eyebrow">Runtime-neutral desired state</span>
          <h2>Live model deployments</h2>
          <p>Validate placement, fast-start policy, exposure and tenant controls without rebuilding cluster infrastructure. Terraform still owns pools, queues and platform capabilities.</p>
        </div>
        <Link className="button button--primary" to={{ pathname: "/admin/model-deployments/new", search: createParams.toString() }}>Draft model deployment</Link>
      </section>

      <div className="inline-notice inline-notice--warning" role="status">
        <strong>Capability-gated control plane.</strong> Desired revisions, live status, validation and render planning are available when their backend capabilities are enabled. Apply, drain, rollback and reconciliation follow the server-advertised writer capabilities; hard deletion remains unavailable in v1.
      </div>

      <div className="toolbar toolbar--wrap">
        <label>Namespace<input aria-label="Model deployment namespace" maxLength={63} onChange={(event) => updateFilter("namespace", event.target.value)} value={namespace} /></label>
        <label>Tenant filter<input aria-label="Model deployment tenant filter" maxLength={120} onChange={(event) => updateFilter("tenant_id", event.target.value)} placeholder="All authorized tenants" value={tenantId} /></label>
        <span className="toolbar__summary">{query.data ? `${items.length} desired deployments in this page` : "Deployment count unavailable"}</span>
      </div>

      {query.error instanceof AdminApiError && query.error.status === 404 ? (
        <div className="state-panel state-panel--error" role="status">
          <strong>Dynamic model configuration is unavailable</strong>
          <span>The control plane has not enabled its durable ModelDeployment read capability. No empty or zero state is inferred.</span>
          {query.error.requestId ? <code>Request {query.error.requestId}</code> : null}
        </div>
      ) : (
        <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && items.length === 0}>
          {() => (
            <div className="table-frame">
              <table className="resource-table resource-table--model-deployments">
                <caption className="sr-only">Dynamic model desired and observed state</caption>
                <thead><tr><th scope="col">Deployment</th><th scope="col">Desired</th><th scope="col">Observed</th><th scope="col">Hot floor / ceiling</th><th scope="col">Placement</th><th scope="col">Fast start</th><th scope="col">Publication</th><th scope="col">Revision</th></tr></thead>
                <tbody>{items.map((deployment) => (
                  <tr key={`${deployment.namespace}/${deployment.name}`}>
                    <th scope="row"><Link className="resource-link" to={rowLink(deployment, searchParams)}>{deployment.name}</Link><span className="secondary-line">{deployment.spec.modelRef} · {deployment.tenant_id}</span></th>
                    <td><span className="mini-chip">{deployment.spec.lifecycle.desiredState}</span></td>
                    <td><ObservedState deployment={deployment} /></td>
                    <td>{deployment.spec.availability.minReplicas} / {deployment.spec.availability.maxReplicas}<span className="secondary-line">idle after {deployment.spec.availability.idleSeconds}s</span></td>
                    <td>{deployment.spec.placement.acceleratorsPerReplica} accelerator{deployment.spec.placement.acceleratorsPerReplica === 1 ? "" : "s"}<span className="secondary-line">{deployment.spec.placement.poolRefs.join(", ")}</span></td>
                    <td><ObservedFastStart deployment={deployment} /></td>
                    <td>{deployment.spec.exposure.openAI ? "OpenAI" : "Private runtime"}<span className="secondary-line">MCP {deployment.spec.exposure.mcp ? "enabled" : "disabled"}</span></td>
                    <td>r{deployment.revision}<span className="secondary-line">{deployment.action} · {formatTimestamp(deployment.created_at)}</span></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </DataBoundary>
      )}

      {query.data?.data.next_after ? (
        <div className="pagination-actions">
          <span>More deployments are available.</span>
          <button className="button" onClick={() => {
            const next = new URLSearchParams(searchParams);
            next.set("after", query.data.data.next_after as string);
            setSearchParams(next);
          }} type="button">Next page</button>
        </div>
      ) : null}
    </div>
  );
}
