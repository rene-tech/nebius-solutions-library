import type {
  ObservedTransport,
  ObservedTransportUsage,
} from "../../api/appsTypes";
import { chartValue, TimeSeriesChart } from "../../components/TimeSeriesChart";
import { formatTimestamp } from "../../lib/format";

export function AppRunTransport({
  observations,
}: {
  observations?: ObservedTransport[];
}) {
  return (
    <section className="panel">
      <h3>Observed requests</h3>
      <p className="supporting-copy">
        Actual HTTP exchanges associated with this run, including MCP, polls and
        idempotent replays. These are not additional logical runs. Duration ends
        at the last response body chunk; bytes count ASGI payloads, not
        compressed wire traffic.
      </p>
      {!observations?.length ? (
        <p>
          No transport observations are available for this run. Historical
          endpoint, method, tool, duration and byte sizes are not inferred from
          the operation.
        </p>
      ) : (
        <div className="table-frame">
          <table className="resource-table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Method / endpoint</th>
                <th>Transport / tool</th>
                <th>HTTP / tool outcome</th>
                <th>Response duration</th>
                <th>Request / response bytes</th>
              </tr>
            </thead>
            <tbody>
              {observations.map((row) => (
                <tr key={row.request_id}>
                  <th scope="row">{formatTimestamp(row.started_at)}</th>
                  <td>
                    {row.method}
                    <span className="secondary-line app-id">
                      {row.endpoint}
                    </span>
                  </td>
                  <td>
                    {row.transport.toUpperCase()}
                    <span className="secondary-line">
                      {row.mcp_tool ?? "—"}
                    </span>
                  </td>
                  <td>
                    {row.http_status ?? "Not observed"}
                    <span className="secondary-line">
                      {row.mcp_is_error === true
                        ? "MCP tool error"
                        : row.mcp_is_error === false
                          ? "MCP tool completed"
                          : ""}
                    </span>
                    {row.error_type || row.disconnected ? (
                      <span className="secondary-line">
                        {row.error_type ?? "Client disconnected"}
                      </span>
                    ) : null}
                  </td>
                  <td>
                    {chartValue(row.response_duration_seconds, "s")}
                    <span className="secondary-line">
                      {row.response_complete
                        ? "Complete response"
                        : "Incomplete response; elapsed until interruption"}
                    </span>
                  </td>
                  <td>
                    {chartValue(row.request_bytes, "B")} /{" "}
                    {chartValue(row.response_bytes, "B")}
                    {!row.request_complete || !row.response_complete ? (
                      <span className="secondary-line">
                        Observed partial payload: {row.request_bytes_observed} /{" "}
                        {row.response_bytes_observed} B. Incomplete totals
                        remain unknown.
                      </span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {observations && observations.length >= 500 ? (
        <p className="supporting-copy">
          Showing the first 500 observed exchanges for this run. Usage
          aggregates include the entire selected window.
        </p>
      ) : null}
    </section>
  );
}

export function AppTransportUsage({
  usage,
}: {
  usage?: ObservedTransportUsage | null;
}) {
  return (
    <section className="panel">
      <h3>Observed transport usage</h3>
      <p className="supporting-copy">
        Separate from logical runs: every observed HTTP exchange, including
        polls and replays, counts once. HTTP success does not imply MCP tool
        success or valid model output. Unrecorded historical traffic is not
        reconstructed.
      </p>
      {!usage || usage.first_observed_at === null ? (
        <p>
          No transport observations in this window. Historical request counts,
          byte sizes and response latency are unavailable.
        </p>
      ) : (
        <>
          <TimeSeriesChart
            title="Actual HTTP responses over time"
            unit="requests"
            state="available"
            reason={null}
            source="observed public HTTP exchanges"
            aggregation={`Observed requests per ${usage.time_bucket_seconds}s bucket, grouped by actual HTTP status. Unobserved buckets remain gaps; MCP tool errors are counted separately.`}
            series={["2xx", "3xx", "4xx", "5xx", "unknown"].map((status) => ({
              id: status,
              label: status,
              points: usage.requests_over_time.map((bucket) => ({
                at: bucket.timestamp,
                value:
                  bucket.status_classes === null
                    ? null
                    : (bucket.status_classes[status] ?? 0),
              })),
            }))}
            summary={{
              average: usage.requests_over_time.some(
                (bucket) => bucket.request_count !== null,
              )
                ? usage.request_count /
                  usage.requests_over_time.filter(
                    (bucket) => bucket.request_count !== null,
                  ).length
                : null,
              maximum: usage.requests_over_time.some(
                (bucket) => bucket.request_count !== null,
              )
                ? Math.max(
                    ...usage.requests_over_time.map(
                      (bucket) => bucket.request_count ?? 0,
                    ),
                  )
                : null,
            }}
          />
          <dl className="definition-grid">
            <div>
              <dt>Observed requests</dt>
              <dd>{usage.request_count}</dd>
            </div>
            <div>
              <dt>HTTP 2xx / 3xx</dt>
              <dd>{usage.successful_http_count}</dd>
            </div>
            <div>
              <dt>HTTP 4xx / 5xx</dt>
              <dd>{usage.failed_http_count}</dd>
            </div>
            <div>
              <dt>MCP tool errors</dt>
              <dd>{usage.mcp_tool_error_count}</dd>
            </div>
            <div>
              <dt>Incomplete responses</dt>
              <dd>{usage.incomplete_response_count}</dd>
            </div>
            <div>
              <dt>Average complete response</dt>
              <dd>
                {chartValue(usage.average_response_duration_seconds, "s")}
              </dd>
            </div>
            <div>
              <dt>Maximum complete response</dt>
              <dd>
                {chartValue(usage.maximum_response_duration_seconds, "s")}
              </dd>
            </div>
            <div>
              <dt>Complete request bytes</dt>
              <dd>
                {chartValue(usage.request_bytes, "B")}
                <span className="secondary-line">
                  {usage.request_bytes_known_count} / {usage.request_count}{" "}
                  bodies fully observed
                </span>
              </dd>
            </div>
            <div>
              <dt>Complete response bytes</dt>
              <dd>
                {chartValue(usage.response_bytes, "B")}
                <span className="secondary-line">
                  {usage.response_bytes_known_count} / {usage.request_count}{" "}
                  bodies fully observed
                </span>
              </dd>
            </div>
          </dl>
          <div className="chip-list" aria-label="Actual HTTP response classes">
            {Object.entries(usage.status_classes).map(([status, count]) => (
              <span className="mini-chip" key={status}>
                {status}: {count}
              </span>
            ))}
          </div>
          <p className="supporting-copy">
            Observation coverage: {formatTimestamp(usage.first_observed_at)} –{" "}
            {formatTimestamp(usage.last_observed_at)}. Unknown/incomplete
            payload totals remain unavailable, not zero.
          </p>
        </>
      )}
    </section>
  );
}
