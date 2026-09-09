import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { appsApi } from "../../api/appsClient";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { Measurement } from "../../components/Measurement";
import { formatTimestamp } from "../../lib/format";
import { RequestDebugLog } from "./RequestDebugLog";

export function AppRunsTab({ appId }: { appId: string }) {
  const { params, navigation } = useAdminTimeWindow();
  const [search, setSearch] = useSearchParams();
  const status = search.get("status") ?? "";
  const principal = (search.get("principal_id") ?? "").slice(0, 200);
  const cursor = search.get("cursor") ?? undefined;
  const query = useQuery({
    queryKey: ["app-runs", appId, params.toString(), status, principal, cursor],
    queryFn: ({ signal }) =>
      appsApi.runs(
        appId,
        params,
        {
          status: status || undefined,
          principal_id: principal || undefined,
          cursor,
        },
        signal,
      ),
  });
  function filter(key: string, value: string) {
    const next = new URLSearchParams(search);
    next.delete("cursor");
    value ? next.set(key, value) : next.delete(key);
    setSearch(next, { replace: true });
  }
  return (
    <div className="page-stack">
      <div className="toolbar">
        <label>
          Run state
          <select
            aria-label="Run state"
            value={status}
            onChange={(event) => filter("status", event.target.value)}
          >
            <option value="">All states</option>
            {[
              "queued",
              "activating",
              "running",
              "succeeded",
              "failed",
              "cancelled",
              "preempted",
              "expired",
            ].map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          User / principal
          <input
            aria-label="Run user"
            value={principal}
            maxLength={200}
            onChange={(event) => filter("principal_id", event.target.value)}
          />
        </label>
      </div>
      <p className="window-caption">
        Accepted {formatTimestamp(params.get("from"))} –{" "}
        {formatTimestamp(params.get("to"))}. One row per logical run, including
        internal retries. Transport status and execution state are separate.
      </p>
      <DataBoundary
        data={query.data}
        error={query.error}
        pending={query.isPending}
        empty={query.data?.data.items.length === 0}
        emptyLabel="No runs in the selected time window and filters."
      >
        {({ data }) => (
          <>
            <div className="table-frame">
              <table className="resource-table">
                <caption className="sr-only">App runs</caption>
                <thead>
                  <tr>
                    <th>Run / operation</th>
                    <th>Execution / result</th>
                    <th>Protocol / HTTP</th>
                    <th>User</th>
                    <th>Accepted</th>
                    <th>Queue</th>
                    <th>Startup</th>
                    <th>Execution</th>
                    <th>Total</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map(({ operation, scientific }) => (
                    <tr key={operation.id}>
                      <th scope="row">
                        <Link
                          className="resource-link"
                          to={{
                            pathname: `/admin/apps/${encodeURIComponent(appId)}/runs/${operation.id}`,
                            search: navigation.toString(),
                          }}
                        >
                          {operation.id.slice(0, 8)}
                        </Link>
                        <span className="secondary-line">
                          {operation.operation}
                        </span>
                      </th>
                      <td>
                        <span
                          className={`operation-state operation-state--${operation.status}`}
                        >
                          {operation.status}
                        </span>
                        <span className="secondary-line">
                          {scientific?.semantic_validation.status ===
                            "not-run" && operation.status === "succeeded"
                            ? "Finalizing results"
                            : (scientific?.semantic_validation.status ??
                              operation.semantic_outcome ??
                              "Result not reported")}
                        </span>
                        {scientific ? (
                          <span className="secondary-line">
                            {Object.entries(scientific.run.stage_counts)
                              .filter(([, count]) => count > 0)
                              .map(([state, count]) => `${count} ${state}`)
                              .join(" · ")}
                          </span>
                        ) : null}
                      </td>
                      <td>
                        {operation.protocol}
                        <span className="secondary-line">
                          HTTP {operation.http_status ?? "Not reported"}
                        </span>
                      </td>
                      <td>
                        <Link
                          to={{
                            pathname: `/admin/users/${encodeURIComponent(operation.principal_id)}`,
                            search: navigation.toString(),
                          }}
                        >
                          {operation.principal_id}
                        </Link>
                      </td>
                      <td>{formatTimestamp(operation.accepted_at)}</td>
                      <td>
                        <Measurement
                          compact
                          value={operation.timings.queue_seconds}
                        />
                      </td>
                      <td>
                        <Measurement
                          compact
                          value={operation.timings.cold_start_seconds}
                        />
                      </td>
                      <td>
                        <Measurement
                          compact
                          value={operation.timings.inference_seconds}
                        />
                      </td>
                      <td>
                        <Measurement
                          compact
                          value={operation.timings.total_seconds}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="pagination-actions">
              {cursor ? (
                <button className="button" onClick={() => filter("cursor", "")}>
                  Newest runs
                </button>
              ) : null}
              {data.next_cursor ? (
                <button
                  className="button"
                  onClick={() => filter("cursor", data.next_cursor!)}
                >
                  Older runs
                </button>
              ) : (
                <span>End of selected window</span>
              )}
            </div>
          </>
        )}
      </DataBoundary>
      <RequestDebugLog appId={appId} />
    </div>
  );
}
