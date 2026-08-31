import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import type {
  AdminCapacityType,
  AdminKueueWorkloadCounts,
  AdminQuantity,
  SourceState,
} from "../../api/types";
import { adminApi } from "../../api/client";
import { DataBoundary } from "../../components/DataBoundary";
import { Measurement } from "../../components/Measurement";
import { formatTimestamp } from "../../lib/format";
import { sharedContextParams } from "../../lib/search";

function stateLabel(value: boolean | null, yes: string, no: string): string {
  return value === null ? "Unknown" : value ? yes : no;
}

function StateChip({ state, label }: { state: SourceState; label?: string }) {
  return <span className={`capability-chip capability-chip--${state}`}>{label ?? state}</span>;
}

function CapacityTypeChip({ value }: { value: AdminCapacityType }) {
  return <span className={`capacity-type capacity-type--${value}`}>{value}</span>;
}

function Quantity({ value }: { value: AdminQuantity }) {
  if (value.value === null) {
    return (
      <span className="measurement measurement--unavailable" title={value.reason ?? "Unavailable"}>
        —<span className="sr-only">. {value.reason ?? "Unavailable"}</span>
      </span>
    );
  }
  return (
    <span className={`measurement measurement--${value.state}`} title={value.reason ?? value.source}>
      <code>{value.value}</code>
      {value.state === "estimated" ? <span className="measurement__qualifier"> estimated</span> : null}
    </span>
  );
}

function CollectionState({
  state,
  reason,
  empty,
  emptyText,
  children,
}: {
  state: SourceState;
  reason: string | null;
  empty: boolean;
  emptyText: string;
  children: React.ReactNode;
}) {
  if (state !== "available") {
    return (
      <div className={`state-panel ${state === "unavailable" ? "state-panel--error" : ""}`} role="status">
        <strong>{state === "stale" ? "Stale projection" : "Projection unavailable"}</strong>
        <span>{reason ?? "The server did not publish this projection."}</span>
      </div>
    );
  }
  if (empty) return <div className="state-panel state-panel--empty">{emptyText}</div>;
  return children;
}

function WorkloadCounts({ value }: { value: AdminKueueWorkloadCounts }) {
  return (
    <div className="measurement-stack">
      <span>Pending <Measurement compact value={value.pending} /></span>
      <span>Reserving <Measurement compact value={value.reserving} /></span>
      <span>Admitted <Measurement compact value={value.admitted} /></span>
    </div>
  );
}

export function CapacityPage() {
  const [searchParams] = useSearchParams();
  const context = sharedContextParams(searchParams);
  const contextSearch = context.toString();
  const query = useQuery({
    queryKey: ["admin-capacity", contextSearch],
    queryFn: ({ signal }) => adminApi.capacity(context, signal),
  });

  return (
    <DataBoundary data={query.data} error={query.error} pending={query.isPending}>
      {({ data }) => {
        const { node_pools: pools, kueue, autoscaling, node_scaler: scaler } = data;
        return (
          <div className="page-stack">
            <section className="panel capacity-intro">
              <div>
                <span className="eyebrow">Heterogeneous accelerator fleet</span>
                <h2>Capacity, queues and elastic supply</h2>
              </div>
              <p>Pool and GPU classes come from the cluster at runtime. Allocated GPU values are Pod-request estimates; healthy GPU values require explicit device telemetry.</p>
            </section>

            <section className="panel section-stack" aria-labelledby="node-pools-title">
              <header className="section-heading">
                <div><span className="eyebrow">Kubernetes inventory</span><h2 id="node-pools-title">Node pools</h2></div>
                <div className="section-heading__meta"><StateChip state={pools.state} /><span>{pools.state === "available" ? `${pools.items.length} observed` : "No current count"}</span></div>
              </header>
              <CollectionState state={pools.state} reason={pools.reason} empty={pools.items.length === 0} emptyText="0 node pools observed in this context.">
                <div aria-label="Provider-neutral GPU node pools" className="table-frame" role="region" tabIndex={0}>
                  <table className="resource-table resource-table--capacity">
                    <caption className="sr-only">Provider-neutral GPU node pools</caption>
                    <thead><tr><th scope="col">Pool</th><th scope="col">GPU class</th><th scope="col">Capacity type</th><th scope="col">Nodes</th><th scope="col">GPU resources</th></tr></thead>
                    <tbody>{pools.items.map((pool) => (
                      <tr key={pool.id}>
                        <th scope="row">{pool.pool_label ?? "Unlabeled pool"}<span className="secondary-line">{pool.instance_type ?? "Instance type unknown"} · {pool.id}</span></th>
                        <td><code>{pool.gpu_class ?? "unknown"}</code></td>
                        <td><CapacityTypeChip value={pool.capacity_type} /></td>
                        <td><strong><Measurement compact value={pool.nodes.ready} /> / <Measurement compact value={pool.nodes.total} /></strong><span className="secondary-line">not ready <Measurement compact value={pool.nodes.not_ready} /> · cordoned <Measurement compact value={pool.nodes.unschedulable} /></span></td>
                        <td>{pool.gpu_resources.length === 0 ? <span className="secondary-line">No extended GPU resources</span> : <div className="gpu-resource-list">{pool.gpu_resources.map((resource) => (
                          <div className="gpu-resource" key={resource.resource_name}>
                            <code>{resource.resource_name}</code>
                            <dl><div><dt>Capacity</dt><dd><Measurement compact value={resource.capacity} /></dd></div><div><dt>Allocatable</dt><dd><Measurement compact value={resource.allocatable} /></dd></div><div><dt>Allocated</dt><dd><Measurement compact value={resource.allocated} /></dd></div><div><dt>Healthy</dt><dd><Measurement compact value={resource.healthy} /></dd></div></dl>
                          </div>
                        ))}</div>}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </CollectionState>
            </section>

            <section className="panel section-stack" aria-labelledby="kueue-title">
              <header className="section-heading">
                <div><span className="eyebrow">Admission control</span><h2 id="kueue-title">Kueue</h2></div>
                <StateChip state={kueue.state} />
              </header>
              <CollectionState state={kueue.state} reason={kueue.reason} empty={false} emptyText="">
                <div className="count-strip" aria-label="Kueue object counts" role="group">
                  <div><strong>{kueue.resource_flavors.length}</strong><span>Resource flavors</span></div>
                  <div><strong>{kueue.cluster_queues.length}</strong><span>Cluster queues</span></div>
                  <div><strong>{kueue.local_queues.length}</strong><span>Local queues</span></div>
                  <div><strong>{kueue.workloads.length}</strong><span>Bounded workloads{kueue.workloads_truncated ? "+" : ""}</span></div>
                </div>

                <div className="split-grid split-grid--capacity">
                  <article className="subpanel">
                    <h3>Resource flavors</h3>
                    {kueue.resource_flavors.length === 0 ? <p className="empty-copy">0 resource flavors observed.</p> : <div className="flavor-list">{kueue.resource_flavors.map((flavor) => <div key={flavor.name}><code>{flavor.name}</code><span>{flavor.gpu_class ?? "GPU class unknown"}</span><CapacityTypeChip value={flavor.capacity_type} /></div>)}</div>}
                  </article>
                  <article className="subpanel">
                    <h3>Cohorts</h3>
                    {kueue.cohorts_state !== "available" ? <p className="empty-copy">{kueue.cohorts_reason ?? "Cohort projection unavailable."}</p> : kueue.cohorts.length === 0 ? <p className="empty-copy">0 cohorts observed.</p> : <div className="flavor-list">{kueue.cohorts.map((cohort) => <div key={cohort.name}><code>{cohort.name}</code><span>Parent {cohort.parent ?? "none"}</span></div>)}</div>}
                  </article>
                </div>

                <div aria-label="Kueue cluster queues" className="table-frame" role="region" tabIndex={0}>
                  <table className="resource-table resource-table--queues">
                    <caption className="sr-only">Kueue cluster queues</caption>
                    <thead><tr><th scope="col">Cluster queue</th><th scope="col">Policy</th><th scope="col">State</th><th scope="col">Workloads</th><th scope="col">Resource quota</th></tr></thead>
                    <tbody>{kueue.cluster_queues.length === 0 ? <tr><td colSpan={5}>0 cluster queues observed.</td></tr> : kueue.cluster_queues.map((queue) => (
                      <tr key={queue.name}>
                        <th scope="row"><code>{queue.name}</code><span className="secondary-line">cohort {queue.cohort ?? "none"}</span></th>
                        <td>{queue.queueing_strategy ?? "Unknown"}<span className="secondary-line">stop: {queue.stop_policy ?? "none"}</span></td>
                        <td>{stateLabel(queue.active, "Active", "Inactive")}</td>
                        <td><WorkloadCounts value={queue.workloads} /></td>
                        <td>{queue.resources.length === 0 ? <span className="secondary-line">No quota status</span> : <div className="quota-list">{queue.resources.map((resource) => <div key={`${resource.flavor}/${resource.resource_name}`}><code>{resource.flavor} · {resource.resource_name}</code><span>Nominal <Quantity value={resource.nominal_quota} /> · reserved <Quantity value={resource.reservation} /> · used <Quantity value={resource.usage} /> · borrowed <Quantity value={resource.borrowed} /></span></div>)}</div>}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>

                <div className="split-grid split-grid--capacity">
                  <article className="subpanel section-stack">
                    <h3>Local queues</h3>
                    {kueue.local_queues.length === 0 ? <p className="empty-copy">0 local queues observed.</p> : <div aria-label="Kueue local queues" className="table-frame" role="region" tabIndex={0}><table className="resource-table"><thead><tr><th scope="col">Queue</th><th scope="col">Cluster queue</th><th scope="col">State</th><th scope="col">Workloads</th></tr></thead><tbody>{kueue.local_queues.map((queue) => <tr key={`${queue.namespace}/${queue.name}`}><th scope="row"><code>{queue.name}</code><span className="secondary-line">{queue.namespace}</span></th><td><code>{queue.cluster_queue}</code></td><td>{stateLabel(queue.active, "Active", "Inactive")}<span className="secondary-line">stop: {queue.stop_policy ?? "none"}</span></td><td><WorkloadCounts value={queue.workloads} /></td></tr>)}</tbody></table></div>}
                  </article>
                  <article className="subpanel section-stack">
                    <h3>Pending and recent workloads</h3>
                    {kueue.workloads.length === 0 ? <p className="empty-copy">0 pending or recent workloads observed.</p> : <div aria-label="Pending and recent Kueue workloads" className="table-frame" role="region" tabIndex={0}><table className="resource-table"><thead><tr><th scope="col">Workload</th><th scope="col">Queue</th><th scope="col">State</th><th scope="col">Created</th></tr></thead><tbody>{kueue.workloads.map((workload) => <tr key={`${workload.namespace}/${workload.name}`}><th scope="row"><code>{workload.name}</code><span className="secondary-line">{workload.namespace}</span></th><td>{workload.local_queue ?? "Unknown"}<span className="secondary-line">cluster {workload.cluster_queue ?? "unknown"}</span></td><td><span className={`operation-state operation-state--${workload.state}`} title={workload.reason ?? undefined}>{workload.state}</span></td><td>{formatTimestamp(workload.created_at)}</td></tr>)}</tbody></table></div>}
                  </article>
                </div>
              </CollectionState>
            </section>

            <div className="split-grid split-grid--capacity">
              <section className="panel section-stack" aria-labelledby="hpa-title">
                <header className="section-heading"><div><span className="eyebrow">Pod elasticity</span><h2 id="hpa-title">Horizontal Pod Autoscalers</h2></div><StateChip state={autoscaling.hpa.state} /></header>
                <CollectionState state={autoscaling.hpa.state} reason={autoscaling.hpa.reason} empty={autoscaling.hpa.horizontal_pod_autoscalers.length === 0} emptyText="0 HPAs observed.">
                  <div aria-label="Horizontal Pod Autoscalers" className="table-frame" role="region" tabIndex={0}><table className="resource-table"><thead><tr><th scope="col">Autoscaler</th><th scope="col">Target</th><th scope="col">Replicas</th><th scope="col">Conditions</th></tr></thead><tbody>{autoscaling.hpa.horizontal_pod_autoscalers.map((item) => <tr key={`${item.namespace}/${item.name}`}><th scope="row"><code>{item.name}</code><span className="secondary-line">{item.namespace}</span></th><td>{item.target_kind} / <code>{item.target_name}</code></td><td><Measurement compact value={item.current_replicas} /> current · <Measurement compact value={item.desired_replicas} /> desired<span className="secondary-line"><Measurement compact value={item.min_replicas} /> min · <Measurement compact value={item.max_replicas} /> max</span></td><td>Able: {stateLabel(item.able_to_scale, "yes", "no")}<span className="secondary-line">Active: {stateLabel(item.scaling_active, "yes", "no")} · limited: {stateLabel(item.scaling_limited, "yes", "no")}</span></td></tr>)}</tbody></table></div>
                </CollectionState>
              </section>

              <section className="panel section-stack" aria-labelledby="keda-title">
                <header className="section-heading"><div><span className="eyebrow">Event-driven elasticity</span><h2 id="keda-title">KEDA scaled objects</h2></div><StateChip state={autoscaling.keda.state} /></header>
                <CollectionState state={autoscaling.keda.state} reason={autoscaling.keda.reason} empty={autoscaling.keda.keda_scaled_objects.length === 0} emptyText="0 KEDA ScaledObjects observed.">
                  <div aria-label="KEDA ScaledObjects" className="table-frame" role="region" tabIndex={0}><table className="resource-table"><thead><tr><th scope="col">Scaled object</th><th scope="col">Target</th><th scope="col">Bounds</th><th scope="col">State</th></tr></thead><tbody>{autoscaling.keda.keda_scaled_objects.map((item) => <tr key={`${item.namespace}/${item.name}`}><th scope="row"><code>{item.name}</code><span className="secondary-line">{item.namespace}</span></th><td>{item.target_kind ?? "Unknown kind"} / <code>{item.target_name}</code></td><td><Measurement compact value={item.min_replicas} /> min · <Measurement compact value={item.max_replicas} /> max</td><td>Ready: {stateLabel(item.ready, "yes", "no")}<span className="secondary-line">Active: {stateLabel(item.active, "yes", "no")} · fallback: {stateLabel(item.fallback, "yes", "no")} · paused: {stateLabel(item.paused, "yes", "no")}</span></td></tr>)}</tbody></table></div>
                </CollectionState>
              </section>
            </div>

            <section className="panel node-scaler" aria-labelledby="node-scaler-title">
              <div><span className="eyebrow">Provider integration</span><h2 id="node-scaler-title">Node scaler capability</h2><p>{scaler.reason ?? "A provider-neutral node scaler has been configured and probed."}</p></div>
              <dl><div><dt>Status</dt><dd><StateChip state={scaler.state} /></dd></div><div><dt>Provider</dt><dd>{scaler.provider ?? "Not reported"}</dd></div><div><dt>Configured</dt><dd>{stateLabel(scaler.configured, "Yes", "No")}</dd></div><div><dt>Healthy</dt><dd>{stateLabel(scaler.healthy, "Yes", "No")}</dd></div><div><dt>Observed</dt><dd>{formatTimestamp(scaler.observed_at)}</dd></div></dl>
            </section>
          </div>
        );
      }}
    </DataBoundary>
  );
}
