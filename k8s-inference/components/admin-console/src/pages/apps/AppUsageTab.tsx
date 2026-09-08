import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { appsApi } from "../../api/appsClient";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { chartValue, TimeSeriesChart } from "../../components/TimeSeriesChart";
import { formatTimestamp } from "../../lib/format";
import { AppTransportUsage } from "./AppTransport";

export function UsageCard({
  label,
  value,
  unit = "",
}: {
  label: string;
  value: number | null;
  unit?: string;
}) {
  return (
    <article className="metric-card">
      <span className="metric-card__label">{label}</span>
      <strong className="metric-card__value">{chartValue(value, unit)}</strong>
      {value === null ? (
        <span className="metric-card__detail">Not observed</span>
      ) : null}
    </article>
  );
}
export function AppUsageTab({ appId }: { appId: string }) {
  const { params, navigation } = useAdminTimeWindow();
  const query = useQuery({
    queryKey: ["app-usage", appId, params.toString()],
    queryFn: ({ signal }) => appsApi.usage(appId, params, signal),
  });
  return (
    <DataBoundary
      data={query.data}
      error={query.error}
      pending={query.isPending}
    >
      {({ data }) => (
        <div className="page-stack">
          <p className="window-caption">
            Logical inference usage in {formatTimestamp(params.get("from"))} –{" "}
            {formatTimestamp(params.get("to"))}. Polls and idempotent replays do
            not count as new runs.
          </p>
          <div className="metric-grid">
            <UsageCard label="Logical runs" value={data.logical_runs} />
            <UsageCard label="Succeeded" value={data.succeeded_runs} />
            <UsageCard label="Failed" value={data.failed_runs} />
            <UsageCard label="Active runs" value={data.active_runs} />
          </div>
          <TimeSeriesChart
            title="Logical runs over time"
            unit="runs"
            state={data.requests_over_time.length ? "available" : "unavailable"}
            reason="No accepted runs in the selected window."
            source="durable operations"
            aggregation={`Count of accepted logical runs per ${data.time_bucket_seconds}s bucket; not request rate`}
            series={[
              {
                id: "logical-runs",
                label: "Accepted runs",
                points: data.requests_over_time.map((bucket) => ({
                  at: bucket.timestamp,
                  value: bucket.logical_runs,
                })),
              },
            ]}
            summary={{
              average: data.requests_over_time.length
                ? data.requests_over_time.reduce(
                    (sum, bucket) => sum + bucket.logical_runs,
                    0,
                  ) / data.requests_over_time.length
                : null,
              maximum: data.requests_over_time.length
                ? Math.max(
                    ...data.requests_over_time.map(
                      (bucket) => bucket.logical_runs,
                    ),
                  )
                : null,
            }}
          />
          <div className="metric-grid">
            <UsageCard label="Distinct users" value={data.unique_users} />
          </div>
          <AppTransportUsage usage={data.observed_transport} />
          <div className="split-grid">
            <section className="panel">
              <h3>Logical run response classes</h3>
              <dl className="definition-grid">
                {Object.entries(data.status_classes).map(([status, count]) => (
                  <div key={status}>
                    <dt>{status}</dt>
                    <dd>{count}</dd>
                  </div>
                ))}
              </dl>
              {!Object.keys(data.status_classes).length ? (
                <p>No operation response statuses recorded in this window.</p>
              ) : null}
              <p className="supporting-copy">
                Recorded operation responses, not a count of all HTTP exchanges
                or semantic successes. Unrecorded statuses remain unknown.
              </p>
            </section>
            <section className="panel">
              <h3>Usage by owner</h3>
              <div className="table-frame">
                <table className="resource-table">
                  <thead>
                    <tr>
                      <th>User</th>
                      <th>Logical runs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.users.map((user) => (
                      <tr key={`${user.tenant_id}-${user.principal_id}`}>
                        <th>
                          {user.user_id ? (
                            <Link
                              to={{
                                pathname: `/admin/users/${encodeURIComponent(user.user_id)}`,
                                search: navigation.toString(),
                              }}
                            >
                              {user.principal_id}
                            </Link>
                          ) : (
                            user.principal_id
                          )}
                          <span className="secondary-line">
                            {user.tenant_id}
                          </span>
                        </th>
                        <td>{user.logical_runs}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </div>
          <div className="metric-grid">
            <UsageCard
              label="Estimated GPU allocation"
              value={data.estimated_gpu_seconds}
              unit="GPU-s"
            />
            <UsageCard label="Input tokens" value={data.input_tokens} />
            <UsageCard label="Output tokens" value={data.output_tokens} />
          </div>
          {data.scientific_gpu ? (
            <section>
              <h3>Measured scientific occupancy</h3>
              <div className="metric-grid">
                <UsageCard
                  label="GPU occupied"
                  value={data.scientific_gpu.occupied_seconds}
                  unit="GPU-s"
                />
                <UsageCard
                  label="GPU active compute"
                  value={data.scientific_gpu.active_compute_seconds}
                  unit="GPU-s"
                />
                <UsageCard
                  label="GPU allocated idle"
                  value={data.scientific_gpu.occupied_idle_seconds}
                  unit="GPU-s"
                />
              </div>
            </section>
          ) : (
            <p className="inline-notice">
              Measured per-run GPU occupancy is not available for this app.
              Shared worker idle time is not silently allocated to each user.
            </p>
          )}
          <section className="panel">
            <dl className="definition-grid">
              <div>
                <dt>First used in window</dt>
                <dd>{formatTimestamp(data.first_used_at)}</dd>
              </div>
              <div>
                <dt>Last used in window</dt>
                <dd>{formatTimestamp(data.last_used_at)}</dd>
              </div>
            </dl>
            {data.notes.map((note, index) => (
              <p className="supporting-copy" key={index}>
                {note}
              </p>
            ))}
          </section>
        </div>
      )}
    </DataBoundary>
  );
}
