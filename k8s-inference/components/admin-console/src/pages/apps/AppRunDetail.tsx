import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { appsApi } from "../../api/appsClient";
import { adminApi } from "../../api/client";
import type { AppRun } from "../../api/appsTypes";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { useSession } from "../../auth/SessionContext";
import { DataBoundary } from "../../components/DataBoundary";
import { Measurement, MetricCard } from "../../components/Measurement";
import {
  ScientificMeasurement,
  ScientificMetricCard,
  FastStartTier,
} from "../scientific/ScientificPresentation";
import { scientificRunNeedsRefresh } from "../scientific/ScientificRunDetailPage";
import { formatTimestamp } from "../../lib/format";
import { AppRunTransport } from "./AppTransport";
import { RequestDebugLog } from "./RequestDebugLog";

export function appRunNeedsRefresh(run?: AppRun) {
  if (!run) return true;
  if (run.scientific) return scientificRunNeedsRefresh(run.scientific);
  return !["succeeded", "failed", "cancelled", "expired"].includes(
    run.operation.status,
  );
}

export function AppRunDetail({
  appId,
  runId,
}: {
  appId: string;
  runId: string;
}) {
  const { params, navigation } = useAdminTimeWindow();
  const { session } = useSession();
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  // Detail identity does not include the moving metrics window: terminal published
  // results stop polling, while active/result-publication transitions poll at 5s.
  const query = useQuery({
    queryKey: ["app-run", appId, runId],
    queryFn: ({ signal }) => appsApi.run(appId, runId, params, signal),
    refetchInterval: (current) =>
      appRunNeedsRefresh(current.state.data?.data) ? 5000 : false,
  });
  async function cancel() {
    setCancelBusy(true);
    setCancelError(null);
    try {
      await adminApi.cancelScientificRun(runId, params);
      setCancelConfirm(false);
      await query.refetch();
    } catch (error) {
      setCancelError(
        error instanceof Error ? error.message : "Cancellation failed.",
      );
    } finally {
      setCancelBusy(false);
    }
  }
  return (
    <DataBoundary
      data={query.data}
      error={query.error}
      pending={query.isPending}
    >
      {({ data, meta }) => {
        const { operation, scientific } = data;
        return (
          <div className="page-stack">
            <div className="configuration-actions">
              <Link
                className="back-link"
                to={{
                  pathname: `/admin/apps/${encodeURIComponent(appId)}/runs`,
                  search: navigation.toString(),
                }}
              >
                ← App runs
              </Link>
              <span>
                Run observed {formatTimestamp(meta.generated_at)} ·{" "}
                {appRunNeedsRefresh(data)
                  ? "Automatic updates every 5s"
                  : operation.status === "succeeded"
                    ? "Publication complete · polling stopped"
                    : "Run terminal · polling stopped"}
              </span>
            </div>
            <section className="panel">
              <div className="section-heading">
                <div>
                  <h3>{operation.operation}</h3>
                  <code>{operation.id}</code>
                </div>
                <span
                  className={`operation-state operation-state--${operation.status}`}
                >
                  {operation.status}
                </span>
              </div>
              <dl className="definition-grid">
                <div>
                  <dt>Protocol</dt>
                  <dd>{operation.protocol}</dd>
                </div>
                <div>
                  <dt>HTTP status</dt>
                  <dd>{operation.http_status ?? "Not reported"}</dd>
                </div>
                <div>
                  <dt>User / principal</dt>
                  <dd>{operation.principal_id}</dd>
                </div>
                <div>
                  <dt>Accepted</dt>
                  <dd>{formatTimestamp(operation.accepted_at)}</dd>
                </div>
                <div>
                  <dt>Completed</dt>
                  <dd>{formatTimestamp(operation.completed_at)}</dd>
                </div>
                <div>
                  <dt>Model revision</dt>
                  <dd className="app-id">{operation.model_revision}</dd>
                </div>
              </dl>
            </section>
            <div className="metric-grid">
              <MetricCard
                label="Queue"
                value={operation.timings.queue_seconds}
              />
              <MetricCard
                label="Startup"
                value={operation.timings.cold_start_seconds}
              />
              <MetricCard
                label="Execution"
                value={operation.timings.inference_seconds}
              />
              <MetricCard
                label="Operation duration"
                value={operation.timings.total_seconds}
              />
            </div>
            <AppRunTransport observations={data.observed_transport} />
            <RequestDebugLog appId={appId} operationId={runId} />
            {operation.error_class ? (
              <p className="inline-notice inline-notice--error">
                {operation.error_class} · {operation.outcome}
              </p>
            ) : null}
            {scientific ? (
              <>
                {scientific.run.status === "succeeded" &&
                scientific.semantic_validation.status === "not-run" ? (
                  <section
                    className="inline-notice"
                    role="status"
                    aria-label="Result publication"
                  >
                    <strong>Finalizing results</strong>
                    <p>
                      Computation finished. Validated artifacts are still being
                      published; this page continues updating automatically.
                    </p>
                  </section>
                ) : null}
                <div className="metric-grid">
                  <ScientificMetricCard
                    label="GPU occupied"
                    value={scientific.run.gpu_accounting.allocated}
                  />
                  <ScientificMetricCard
                    label="GPU active"
                    value={scientific.run.gpu_accounting.active}
                  />
                  <ScientificMetricCard
                    label="GPU allocated idle"
                    value={scientific.run.gpu_accounting.idle_total}
                  />
                  <ScientificMetricCard
                    label="GPU grace / drain"
                    value={scientific.run.gpu_accounting.grace_drain}
                  />
                </div>
                <section className="panel">
                  <div className="section-heading">
                    <h3>Admission and startup</h3>
                    <FastStartTier observation={scientific.run.fast_start} />
                  </div>
                  <p>
                    {scientific.run.queue.admission_state} ·{" "}
                    {scientific.run.queue.admission_reason}
                  </p>
                  <div
                    className="chip-list"
                    aria-label="Shard admission counts"
                  >
                    {Object.entries(
                      scientific.run.queue.shard_counts ?? {},
                    ).map(([state, count]) => (
                      <span className="mini-chip" key={state}>
                        {count} {state}
                      </span>
                    ))}
                  </div>
                </section>
                <section className="panel">
                  <h3>Observed phase durations</h3>
                  <p className="supporting-copy">
                    Wall-time union of actual lifecycle boundaries, including
                    parallel attempts. These intervals are not additive
                    end-to-end latency or GPU-seconds.
                  </p>
                  <div className="table-frame">
                    <table className="resource-table">
                      <thead>
                        <tr>
                          <th>Phase</th>
                          <th>Duration</th>
                          <th>Evidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {scientific.lifecycle_phases.map((phase) => (
                          <tr key={phase.phase}>
                            <th scope="row">{phase.phase}</th>
                            <td>
                              <ScientificMeasurement value={phase.duration} />
                            </td>
                            <td>
                              {phase.duration.source}
                              <span className="secondary-line">
                                {phase.duration.reason}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
                <section className="panel">
                  <h3>Stages and attempts</h3>
                  {scientific.stages.map((stage) => (
                    <div key={stage.id}>
                      <h4>
                        {stage.display_name} · {stage.status}{" "}
                        <span className="mini-chip">
                          {stage.resource_class}
                        </span>
                      </h4>
                      <div className="table-frame">
                        <table className="resource-table">
                          <thead>
                            <tr>
                              <th>Attempt</th>
                              <th>Status / phase</th>
                              <th>Placement</th>
                              <th>Started / completed</th>
                              <th>Failure</th>
                            </tr>
                          </thead>
                          <tbody>
                            {stage.attempts.map((attempt) => (
                              <tr key={attempt.id}>
                                <th>
                                  {attempt.number}
                                  <span className="secondary-line app-id">
                                    {attempt.id}
                                  </span>
                                </th>
                                <td>
                                  {attempt.status}
                                  <span className="secondary-line">
                                    {attempt.phase ?? "Phase not observed"}
                                  </span>
                                  <span className="secondary-line">
                                    {attempt.phase_reason}
                                  </span>
                                </td>
                                <td>
                                  {attempt.resolved_pool_id ?? "Not admitted"}
                                  <span className="secondary-line">
                                    {attempt.pod_count ?? "Unknown"} pods ·{" "}
                                    {attempt.gpu_count ?? "Unknown"} GPUs
                                  </span>
                                </td>
                                <td>
                                  {formatTimestamp(attempt.started_at)}
                                  <span className="secondary-line">
                                    {formatTimestamp(attempt.completed_at)}
                                  </span>
                                </td>
                                <td>
                                  {attempt.error
                                    ? `${attempt.error.code}: ${attempt.error.message}`
                                    : "—"}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  ))}
                </section>
                <section className="panel">
                  <h3>Results and artifacts</h3>
                  <p>
                    Validation: {scientific.semantic_validation.status} ·{" "}
                    {scientific.semantic_validation.validator_id}
                  </p>
                  {scientific.artifacts.length ? (
                    <div className="table-frame">
                      <table className="resource-table">
                        <thead>
                          <tr>
                            <th>Artifact</th>
                            <th>State / role</th>
                            <th>Size</th>
                            <th>SHA-256</th>
                            <th>Access</th>
                          </tr>
                        </thead>
                        <tbody>
                          {scientific.artifacts.map((artifact) => (
                            <tr key={artifact.artifact_id}>
                              <th>{artifact.name}</th>
                              <td>
                                {artifact.state} · {artifact.role}
                              </td>
                              <td>
                                <ScientificMeasurement
                                  value={artifact.size_bytes}
                                />
                              </td>
                              <td>
                                <code className="app-id">
                                  {artifact.sha256 ?? "Not published"}
                                </code>
                              </td>
                              <td>
                                {artifact.download.available &&
                                artifact.download.href?.startsWith(
                                  "/admin/api/v1/",
                                ) ? (
                                  <a
                                    className="resource-link"
                                    href={artifact.download.href}
                                    download
                                  >
                                    Download {artifact.name}
                                  </a>
                                ) : (
                                  (artifact.download.reason ?? "Not available")
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p>
                      {scientific.run.status === "succeeded" &&
                      scientific.semantic_validation.status === "not-run"
                        ? "Artifacts are being published."
                        : "No artifacts were published for this run."}
                    </p>
                  )}
                </section>
                {scientific.run.cancellation.can_cancel &&
                session.principal.role !== "viewer" ? (
                  <section className="panel">
                    {cancelError ? <p role="alert">{cancelError}</p> : null}
                    {cancelConfirm ? (
                      <>
                        <p>
                          Cancel this run and terminate its current attempt?
                          Completed artifacts remain available.
                        </p>
                        <button
                          className="button button--danger"
                          disabled={cancelBusy}
                          onClick={() => void cancel()}
                        >
                          {cancelBusy ? "Requesting…" : "Confirm cancellation"}
                        </button>{" "}
                        <button
                          className="button"
                          disabled={cancelBusy}
                          onClick={() => setCancelConfirm(false)}
                        >
                          Keep running
                        </button>
                      </>
                    ) : (
                      <button
                        className="button button--danger"
                        onClick={() => setCancelConfirm(true)}
                      >
                        Cancel run
                      </button>
                    )}
                  </section>
                ) : null}
              </>
            ) : (
              <section className="panel">
                <h3>Usage and outcome</h3>
                <dl className="definition-grid">
                  <div>
                    <dt>Semantic outcome</dt>
                    <dd>{operation.semantic_outcome ?? "Not reported"}</dd>
                  </div>
                  <div>
                    <dt>Outcome</dt>
                    <dd>{operation.outcome ?? "Not reported"}</dd>
                  </div>
                  <div>
                    <dt>Attempts</dt>
                    <dd>
                      {operation.attempt} / {operation.max_attempts}
                    </dd>
                  </div>
                  <div>
                    <dt>Input tokens</dt>
                    <dd>
                      <Measurement value={operation.input_tokens} />
                    </dd>
                  </div>
                  <div>
                    <dt>Output tokens</dt>
                    <dd>
                      <Measurement value={operation.output_tokens} />
                    </dd>
                  </div>
                  <div>
                    <dt>Estimated GPU allocation</dt>
                    <dd>
                      <Measurement value={operation.estimated_gpu_seconds} />
                    </dd>
                  </div>
                </dl>
                <p className="supporting-copy">
                  Request and response payloads are not exposed by the admin
                  projection.
                </p>
              </section>
            )}
          </div>
        );
      }}
    </DataBoundary>
  );
}
