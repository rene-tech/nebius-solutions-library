import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { adminApi, AdminApiError } from "../../api/client";
import type { ScientificCapabilities, ScientificObservabilityLink, ScientificRunDetail, ScientificRunSummary } from "../../api/scientificTypes";
import type { AdminEnvelope } from "../../api/types";
import { useSession } from "../../auth/SessionContext";
import { DataBoundary } from "../../components/DataBoundary";
import { rolePermits } from "../../lib/access";
import { formatTimestamp } from "../../lib/format";
import { sharedContextParams } from "../../lib/search";
import {
  AccessGate,
  FastStartTier,
  ScientificMeasurement,
  ScientificMetricCard,
  ScientificStatusChip,
  shortDigest,
} from "./ScientificPresentation";
import { useScientificCapabilities } from "./useScientificCapabilities";

function safeHref(link: ScientificObservabilityLink): string | null {
  if (!link.available || !link.href) return null;
  if (link.href.startsWith("/admin/")) return link.href;
  try {
    const parsed = new URL(link.href);
    return parsed.protocol === "https:" ? link.href : null;
  } catch {
    return null;
  }
}

/** Why the cancel command is unavailable, or null when the operator may request it. */
export function cancellationBlocker(
  run: ScientificRunSummary,
  capabilities: ScientificCapabilities | undefined,
  canOperate: boolean,
): string | null {
  if (!capabilities?.run_control.available) {
    return capabilities?.run_control.reason ?? "This build does not publish a scientific cancellation command.";
  }
  if (!canOperate) return "Operator role required to request cancellation.";
  if (run.cancellation.state !== "not-requested") return "Cancellation has already been requested for this run.";
  if (!run.cancellation.can_cancel) return "This run is terminal and can no longer be cancelled.";
  return null;
}

interface CancellationControlProps {
  run: ScientificRunSummary;
  blocker: string | null;
  onCancel: () => Promise<void>;
}

function CancellationControl({ run, blocker, onCancel }: CancellationControlProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [requested, setRequested] = useState(false);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await onCancel();
      setRequested(true);
      setConfirming(false);
    } catch (caught) {
      const detail = caught instanceof AdminApiError
        ? `${caught.message}${caught.requestId ? ` · request ${caught.requestId}` : ""}`
        : "The cancellation request failed.";
      setError(detail);
    } finally {
      setBusy(false);
    }
  }

  // A request this operator just made outranks the "already requested" blocker
  // the refreshed projection now reports, so the confirmation stays visible.
  if (requested) {
    return <div className="inline-notice" role="status"><strong>Cancellation requested.</strong> The controller terminates the active attempt on its next reconciliation; this page reflects the durable request.</div>;
  }
  if (blocker) return <p className="supporting-copy">{blocker}</p>;
  return (
    <div className="section-stack">
      {error ? <div className="inline-notice inline-notice--error" role="alert"><strong>Cancellation was not recorded.</strong> {error}</div> : null}
      {confirming ? (
        <div className="inline-notice inline-notice--warning mutation-confirmation" aria-label="Confirm cancellation">
          <div><strong>Confirm cancellation</strong><span>This records a {run.cancellation.mode} request for {run.display_name}; the running attempt stops and the run ends as cancelled. It cannot be undone.</span></div>
          <div className="configuration-actions">
            <button className="button button--danger" disabled={busy} onClick={() => void confirm()} type="button">{busy ? "Requesting…" : "Confirm cancellation"}</button>
            <button className="button" disabled={busy} onClick={() => setConfirming(false)} type="button">Keep running</button>
          </div>
        </div>
      ) : (
        <div className="configuration-actions">
          <button className="button button--danger" onClick={() => setConfirming(true)} type="button">Request cancellation</button>
          <span>Available to operators while the run is not yet terminal.</span>
        </div>
      )}
    </div>
  );
}

export function ScientificRunDetailPage() {
  const { runId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { session } = useSession();
  const queryClient = useQueryClient();
  const context = sharedContextParams(searchParams);
  const capabilitiesQuery = useScientificCapabilities(context);
  const capabilities = capabilitiesQuery.data?.data;
  const runsAvailable = capabilities?.run_history.available === true;
  const canOperate = rolePermits(session.principal.role, "operator");
  const backParams = new URLSearchParams(context);
  for (const key of ["tenant", "model", "run_status", "service_class", "access_state", "cursor"]) {
    const value = searchParams.get(key);
    if (value) backParams.set(key, value);
  }
  const queryKey = ["admin-scientific-run", runId, context.toString()];
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => adminApi.scientificRun(runId, context, signal),
    enabled: Boolean(runId) && runsAvailable,
    refetchInterval: (current) => {
      const status = current.state.data?.data.run.status;
      return status && ["succeeded", "failed", "cancelled"].includes(status) ? false : 5000;
    },
  });

  async function requestCancellation() {
    const refreshed = await adminApi.cancelScientificRun(runId, context);
    queryClient.setQueryData<AdminEnvelope<ScientificRunDetail>>(queryKey, refreshed);
    await queryClient.invalidateQueries({ queryKey: ["admin-scientific-runs"] });
  }

  if (capabilitiesQuery.isPending) {
    return <div className="state-panel state-panel--loading" role="status">Checking scientific run capability…</div>;
  }
  if (!runsAvailable) {
    return (
      <div className="state-panel" role="status">
        <strong>Scientific run history is not enabled</strong>
        <span>{capabilitiesQuery.data?.data.run_history.reason ?? capabilitiesQuery.error?.message ?? "No durable scientific run reader is configured."}</span>
      </div>
    );
  }

  return (
    <DataBoundary data={query.data} error={query.error} pending={query.isPending} loadingLabel="Loading scientific run detail…">
      {({ data }) => {
        const { run } = data;
        const measuredIdleCauses = run.gpu_accounting.idle_by_cause.filter((entry) => entry.duration.evidence === "measured");
        const waitingReasons = [...new Set(data.stages.flatMap((stage) => stage.attempts)
          .filter((attempt) => attempt.status === "queued" && attempt.phase_reason)
          .map((attempt) => attempt.phase_reason))];
        return (
          <div className="page-stack scientific-page">
            <div className="configuration-actions">
              <Link className="back-link" to={{ pathname: "/admin/scientific-runs", search: backParams.toString() }}>← All scientific runs</Link>
              <button className="button" disabled={query.isFetching} onClick={() => void query.refetch()} type="button">Refresh run</button>
              <span className="supporting-copy">Active runs update every 5 seconds.</span>
            </div>
            <section className="identity-panel scientific-run-identity">
              <div><span className="eyebrow">{run.model.display_name} · {run.operation}</span><h2>{run.display_name}</h2><code>{run.id}</code></div>
              <ScientificStatusChip state={run.status} reason={run.error?.message ?? `Run is ${run.status}.`} />
            </section>

            {waitingReasons.length ? <section className="inline-notice inline-notice--warning" aria-label="Placement progress"><strong>Waiting for placement</strong>{waitingReasons.map((reason) => <p key={reason}>{reason}</p>)}<span>See each attempt's selected pool below. Admission does not mean GPU computation has started.</span></section> : null}

            <section className={`inline-notice ${run.access.state === "blocked" ? "inline-notice--error" : run.access.state === "unverified" ? "inline-notice--warning" : ""}`} aria-labelledby="scientific-access-title">
              <strong id="scientific-access-title">Access admission</strong>
              <AccessGate access={run.access} />
              <span className="secondary-line scientific-secondary">Credentials are never exposed in this projection. Receipt digests are non-secret admission evidence.</span>
            </section>

            <div className="metric-grid">
              <ScientificMetricCard label="GPU occupied" value={run.gpu_accounting.allocated} detail={`Scheduler occupancy · ${run.gpu_accounting.gpu_count === null ? "GPU count unavailable" : `${run.gpu_accounting.gpu_count} GPU per attempt`} · ${run.gpu_accounting.capacity_type}`} />
              <ScientificMetricCard label="GPU active" value={run.gpu_accounting.active} detail="Active compute from the lifecycle ledger" />
              <ScientificMetricCard label="GPU idle" value={run.gpu_accounting.idle_total} detail={`${measuredIdleCauses.length} measured idle causes`} />
              <ScientificMetricCard label="GPU grace / drain" value={run.gpu_accounting.grace_drain} detail="Measured separately from active compute" />
            </div>

            <div className="split-grid">
              <section className="panel" aria-labelledby="scientific-request-identity-title">
                <div className="section-heading"><div><span className="eyebrow">Request</span><h2 id="scientific-request-identity-title">Attribution and service class</h2></div></div>
                <dl className="definition-grid">
                  <div><dt>Tenant</dt><dd>{run.attribution.tenant_id}</dd></div>
                  <div><dt>User</dt><dd>{run.attribution.user_id}</dd></div>
                  <div><dt>Principal</dt><dd>{run.attribution.principal_id}</dd></div>
                  <div><dt>API key prefix</dt><dd>{run.attribution.api_key_prefix}</dd></div>
                  <div><dt>Batch</dt><dd>{run.batch_id}</dd></div>
                  <div><dt>Submitted</dt><dd>{formatTimestamp(run.submitted_at)}</dd></div>
                  <div><dt>Completed</dt><dd>{formatTimestamp(run.completed_at)}</dd></div>
                  <div><dt>Requested class</dt><dd>{run.service_class.requested}</dd></div>
                  <div><dt>Effective class</dt><dd>{run.service_class.effective}</dd></div>
                  <div><dt>Class decision</dt><dd>{run.service_class.reason}</dd></div>
                </dl>
              </section>
              <section className="panel" aria-labelledby="scientific-queue-title">
                <div className="section-heading"><div><span className="eyebrow">Admission</span><h2 id="scientific-queue-title">Queue and priority</h2></div><ScientificStatusChip state={run.queue.admission_state === "finished" ? "succeeded" : run.queue.admission_state === "inadmissible" ? "blocked" : run.queue.admission_state} label={run.queue.admission_state} reason={run.queue.admission_reason} /></div>
                <dl className="definition-grid">
                  <div><dt>Tenant queue</dt><dd>{run.queue.tenant_queue}</dd></div>
                  <div><dt>Model lane</dt><dd>{run.queue.model_lane}</dd></div>
                  <div><dt>Local queue</dt><dd>{run.queue.local_queue}</dd></div>
                  <div><dt>Cluster queue</dt><dd>{run.queue.cluster_queue}</dd></div>
                  <div><dt>Priority class</dt><dd>{run.queue.workload_priority_class}</dd></div>
                  <div><dt>Priority value</dt><dd>{run.queue.priority_value}</dd></div>
                  <div><dt>Admitted</dt><dd>{formatTimestamp(run.queue.admitted_at)}</dd></div>
                  <div><dt>Position at observation</dt><dd><ScientificMeasurement compact value={run.queue.queue_position} /></dd></div>
                </dl>
                <p className="supporting-copy">{run.queue.admission_reason}</p>
                {Object.keys(run.queue.shard_counts ?? {}).length ? <div className="chip-list" aria-label="Current shard admission">{Object.entries(run.queue.shard_counts ?? {}).map(([state, count]) => <span className="mini-chip" key={state}>{count} {state}</span>)}</div> : null}
              </section>
            </div>

            <section className="panel" aria-labelledby="scientific-backend-title">
              <div className="section-heading"><div><span className="eyebrow">Immutable execution</span><h2 id="scientific-backend-title">Model and backend identity</h2></div><FastStartTier observation={run.fast_start} /></div>
              <dl className="definition-grid scientific-definition-grid--wide">
                <div><dt>Model</dt><dd>{run.model.model_id}</dd></div>
                <div><dt>Execution mode</dt><dd>{run.model.execution_mode}</dd></div>
                <div><dt>Backend</dt><dd>{run.model.backend.backend_id}</dd></div>
                <div><dt>Backend kind</dt><dd>{run.model.backend.kind}</dd></div>
                <div><dt>Source</dt><dd>{run.model.backend.source_repository}</dd></div>
                <div><dt>Source revision</dt><dd><code title={run.model.backend.source_revision ?? undefined}>{shortDigest(run.model.backend.source_revision)}</code></dd></div>
                <div><dt>Model revision</dt><dd><code title={run.model.backend.model_revision ?? undefined}>{shortDigest(run.model.backend.model_revision)}</code></dd></div>
                <div><dt>Runtime image</dt><dd><code title={run.model.backend.runtime_image_digest ?? undefined}>{shortDigest(run.model.backend.runtime_image_digest)}</code></dd></div>
                <div><dt>Execution identity</dt><dd><code title={run.model.backend.execution_identity_digest ?? undefined}>{shortDigest(run.model.backend.execution_identity_digest)}</code></dd></div>
                <div><dt>Fast-start observed</dt><dd>{formatTimestamp(run.fast_start.observed_at)}</dd></div>
              </dl>
              <p className="supporting-copy">{run.fast_start.reason}</p>
            </section>

            <section className="panel" aria-labelledby="scientific-lifecycle-title">
              <div className="section-heading"><div><span className="eyebrow">Lifecycle</span><h2 id="scientific-lifecycle-title">Phase durations</h2></div></div>
              <div className="scientific-phase-grid">
                {data.lifecycle_phases.map((phase) => <article key={phase.phase}><span>{phase.phase}</span><strong><ScientificMeasurement value={phase.duration} /></strong><small>{phase.duration.source}</small></article>)}
              </div>
            </section>

            <section className="panel" aria-labelledby="scientific-gpu-accounting-title">
              <div className="section-heading"><div><span className="eyebrow">No double counting</span><h2 id="scientific-gpu-accounting-title">GPU idle by cause</h2></div><span className="section-heading__meta">Reconciliation <ScientificMeasurement compact value={run.gpu_accounting.reconciliation_delta} /></span></div>
              <div className="scientific-accounting-grid">
                {run.gpu_accounting.idle_by_cause.map((entry) => <div key={entry.cause}><span>{entry.cause}</span><strong><ScientificMeasurement value={entry.duration} /></strong><small>{entry.duration.reason ?? entry.duration.source}</small></div>)}
              </div>
              <p className="supporting-copy">Scheduler-occupied GPU time is partitioned into active, idle-by-cause, and grace/drain across every attempt, including retries. It is not GPU-memory allocation or DCGM utilization. Estimated or incomplete boundaries remain labelled.</p>
            </section>

            <section className="section-stack" aria-labelledby="scientific-dag-title">
              <div className="section-heading"><div><span className="eyebrow">Workload DAG</span><h2 id="scientific-dag-title">Stages and attempts</h2></div><span className="section-heading__meta">Maximum {data.retry.max_attempts_per_stage} attempts per stage</span></div>
              <ol className="scientific-stage-list">
                {[...data.stages].sort((left, right) => left.ordinal - right.ordinal).map((stage) => (
                  <li key={stage.id} className="panel scientific-stage">
                    <header>
                      <div><span className="scientific-stage__ordinal" aria-hidden="true">{stage.ordinal}</span><span className="eyebrow">Needs {stage.needs.length ? stage.needs.join(", ") : "nothing"}</span><h3>{stage.display_name}</h3><code>{stage.id}</code></div>
                      <ScientificStatusChip state={stage.status} reason={`Stage ${stage.display_name} is ${stage.status}.`} />
                    </header>
                    <div className="chip-list"><span className="mini-chip">{stage.resource_class}</span><span className="mini-chip">{stage.admission_mode}</span><span className="mini-chip">checkpoint {stage.checkpoint_mode}</span></div>
                    <div className="table-frame scientific-attempts">
                      <table className="resource-table">
                        <caption className="sr-only">Attempts for {stage.display_name}</caption>
                        <thead><tr><th scope="col">Attempt</th><th scope="col">Status</th><th scope="col">Started</th><th scope="col">Completed</th><th scope="col">Workload / job</th><th scope="col">Admission / placement</th><th scope="col">Checkpoint</th><th scope="col">Error</th></tr></thead>
                        <tbody>{stage.attempts.map((attempt) => <tr key={attempt.id}>
                          <th scope="row">#{attempt.number}<span className="secondary-line">{attempt.id}</span></th>
                          <td><ScientificStatusChip state={attempt.status} reason={attempt.error?.message ?? attempt.phase_reason ?? `Attempt is ${attempt.status}.`} />{attempt.phase ? <span className="secondary-line">{attempt.phase.replaceAll("_", " ")}</span> : null}{attempt.phase_reason ? <span className="secondary-line scientific-secondary">{attempt.phase_reason}</span> : null}</td>
                          <td>{formatTimestamp(attempt.started_at)}</td>
                          <td>{formatTimestamp(attempt.completed_at)}</td>
                          <td><code>{attempt.workload_uid ?? "Not created"}</code><span className="secondary-line">{attempt.job_uid ?? "No job"}</span></td>
                          <td>{attempt.gpu_count === null ? "GPU count unavailable" : attempt.gpu_count ? `${attempt.gpu_count} GPU` : "CPU"}<span className="secondary-line">Admitted {formatTimestamp(attempt.admitted_at)}</span><span className="secondary-line">Pool {attempt.resolved_pool_id ?? "unavailable"} · flavor {attempt.admitted_resource_flavor ?? "unavailable"}</span><span className="secondary-line">Resource {attempt.accelerator_resource_name ?? "unavailable"}</span><span className="secondary-line">{attempt.pod_count === null ? "pod count unavailable" : `${attempt.pod_count} pod`} · {attempt.node_count === null ? "node count unavailable" : `${attempt.node_count} node`}</span></td>
                          <td>{attempt.checkpoint_output_artifact_id ?? attempt.checkpoint_input_artifact_id ?? "None"}</td>
                          <td>{attempt.error ? <><code>{attempt.error.code}</code><span className="secondary-line scientific-secondary">{attempt.error.message} · {attempt.error.retryable ? "retryable" : "terminal"}</span></> : "No error"}</td>
                        </tr>)}</tbody>
                      </table>
                    </div>
                  </li>
                ))}
              </ol>
            </section>

            <section className="panel" aria-labelledby="scientific-artifacts-title">
              <div className="section-heading"><div><span className="eyebrow">Immutable results</span><h2 id="scientific-artifacts-title">Artifacts</h2></div><span className="section-heading__meta">Semantic validation {data.semantic_validation.status}</span></div>
              <div className="table-frame">
                <table className="resource-table resource-table--scientific-artifacts">
                  <caption className="sr-only">Scientific input, output, checkpoint, and validation artifacts</caption>
                  <thead><tr><th scope="col">Artifact</th><th scope="col">Role</th><th scope="col">Semantic type</th><th scope="col">Integrity</th><th scope="col">Size</th><th scope="col">Created</th><th scope="col">Access</th></tr></thead>
                  <tbody>{data.artifacts.map((artifact) => <tr key={artifact.artifact_id}>
                    <th scope="row">{artifact.name}<span className="secondary-line">{artifact.artifact_id} · {artifact.media_type}</span></th>
                    <td><ScientificStatusChip state={artifact.state === "available" ? "succeeded" : artifact.state === "pending" ? "pending" : "failed"} label={`${artifact.role} · ${artifact.state}`} /></td>
                    <td>{artifact.semantic_type}</td>
                    <td>{artifact.sha256 ? <code title={artifact.sha256}>{shortDigest(artifact.sha256)}</code> : "Digest pending"}</td>
                    <td><ScientificMeasurement compact value={artifact.size_bytes} /></td>
                    <td>{formatTimestamp(artifact.created_at)}</td>
                    <td>{artifact.download.available && artifact.download.href?.startsWith("/admin/") ? <a className="text-link" href={artifact.download.href} download={artifact.name}>Download artifact</a> : <span title={artifact.download.reason ?? undefined}>Not available</span>}</td>
                  </tr>)}</tbody>
                </table>
              </div>
            </section>

            <div className="split-grid">
              <section className="panel" aria-labelledby="scientific-retry-title">
                <div className="section-heading"><div><span className="eyebrow">Failure policy</span><h2 id="scientific-retry-title">Errors and retries</h2></div></div>
                <dl className="definition-grid">
                  <div><dt>Run error</dt><dd>{run.error ? run.error.code : "No terminal error"}</dd></div>
                  <div><dt>Retryable</dt><dd>{run.error ? run.error.retryable ? "Yes" : "No" : "Not applicable"}</dd></div>
                  <div><dt>Attempts per stage</dt><dd>{data.retry.max_attempts_per_stage}</dd></div>
                  <div><dt>Retryable exits</dt><dd>{data.retry.retryable_exit_codes.length ? data.retry.retryable_exit_codes.join(", ") : "None"}</dd></div>
                  <div><dt>Semantic validator</dt><dd>{data.semantic_validation.validator_id}</dd></div>
                  <div><dt>Validation receipt</dt><dd>{data.semantic_validation.receipt_digest ? <code title={data.semantic_validation.receipt_digest}>{shortDigest(data.semantic_validation.receipt_digest)}</code> : "Not emitted"}</dd></div>
                </dl>
              </section>
              <section className="panel" aria-labelledby="scientific-cancellation-title">
                <div className="section-heading"><div><span className="eyebrow">Control state</span><h2 id="scientific-cancellation-title">Cancellation</h2></div><ScientificStatusChip state={run.cancellation.state === "acknowledged" ? "cancelled" : run.cancellation.state === "requested" ? "cancelling" : "candidate"} label={run.cancellation.state} reason={run.cancellation.reason ?? "No cancellation reason was recorded."} /></div>
                <dl className="definition-grid">
                  <div><dt>Can cancel now</dt><dd>{run.cancellation.can_cancel ? "Yes" : "No"}</dd></div>
                  <div><dt>Mode</dt><dd>{run.cancellation.mode}</dd></div>
                  <div><dt>Grace</dt><dd>{run.cancellation.grace_seconds === null ? "Unavailable" : `${run.cancellation.grace_seconds}s`}</dd></div>
                  <div><dt>Requested</dt><dd>{formatTimestamp(run.cancellation.requested_at)}</dd></div>
                  <div><dt>Requested by</dt><dd>{run.cancellation.requested_by ?? "No request"}</dd></div>
                  <div><dt>Reason</dt><dd>{run.cancellation.reason ?? "No request"}</dd></div>
                </dl>
                <CancellationControl blocker={cancellationBlocker(run, capabilities, canOperate)} onCancel={requestCancellation} run={run} />
              </section>
            </div>

            <section className="panel" aria-labelledby="scientific-observability-title">
              <div className="section-heading"><div><span className="eyebrow">Correlation</span><h2 id="scientific-observability-title">Traces, logs, and metrics</h2></div><span className="section-heading__meta">Payloads {data.payloads_exposed ? "exposed" : "never exposed"}</span></div>
              <div className="scientific-observability-links">
                {data.observability.map((link) => {
                  const href = safeHref(link);
                  return href
                    ? <a className="observability-card" href={href} key={link.kind}><span className="eyebrow">{link.kind}</span><strong>{link.label}</strong><small>Open correlated signal</small></a>
                    : <div className="observability-card observability-card--disabled" key={link.kind}><span className="eyebrow">{link.kind}</span><strong>{link.label}</strong><small>{link.reason ?? "This signal is unavailable."}</small></div>;
                })}
              </div>
            </section>
          </div>
        );
      }}
    </DataBoundary>
  );
}
