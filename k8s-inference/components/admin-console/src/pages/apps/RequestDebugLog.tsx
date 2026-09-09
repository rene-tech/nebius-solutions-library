import { Fragment, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { requestDebugApi } from "../../api/requestDebugClient";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { formatTimestamp } from "../../lib/format";
import { RequestDebugExchange } from "./RequestDebugExchange";
import "./RequestDebugLog.css";

export function RequestDebugLog({
  appId,
  operationId,
}: {
  appId?: string;
  operationId?: string;
}) {
  const { params, navigation } = useAdminTimeWindow();
  const scope = JSON.stringify([appId, operationId, params.toString()]);
  const [page, setPage] = useState<{ scope: string; cursor?: string }>({
    scope,
  });
  const cursor = page.scope === scope ? page.cursor : undefined;
  const [expanded, setExpanded] = useState<string | null>(null);
  const headingId = useId();
  const query = useQuery({
    queryKey: ["request-debug-list", scope, cursor],
    queryFn: ({ signal }) =>
      requestDebugApi.list(
        appId,
        params,
        { operation_id: operationId, cursor },
        signal,
      ),
  });
  return (
    <section className="panel request-debug-log" aria-labelledby={headingId}>
      <div className="section-heading">
        <h3 id={headingId}>
          {operationId ? "Run request / response debug" : "Request log"}
        </h3>
        <button
          type="button"
          className="button"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          Refresh request log
        </button>
      </div>
      <p className="supporting-copy">
        Actual public API exchanges and model upstream attempts, not logical run
        counts.
        {operationId
          ? " Filtered to this operation."
          : " Includes rejected requests without an operation."}
        {!appId
          ? " All Apps and requests with no App attribution; App search filters do not apply."
          : " Run-state and user filters above do not filter this request log."}{" "}
        Bodies load only when you inspect an exchange.
      </p>
      <p className="window-caption">
        Request start {formatTimestamp(params.get("from"))} –{" "}
        {formatTimestamp(params.get("to"))}.
      </p>
      <DataBoundary
        data={query.data}
        error={query.error}
        pending={query.isPending}
        loadingLabel="Loading request log…"
        empty={query.data?.data.items.length === 0}
        emptyLabel="No request/response capture recorded in this window. Historical payloads cannot be reconstructed; this does not mean there were no requests."
      >
        {({ data }) => (
          <>
            <div className="table-frame">
              <table className="resource-table request-debug-table">
                <caption className="sr-only">
                  Captured request exchanges
                </caption>
                <thead>
                  <tr>
                    <th>Started</th>
                    <th>Source / attempt</th>
                    <th>Request</th>
                    <th>Outcome</th>
                    <th>Operation</th>
                    <th>Capture</th>
                    <th>Inspect</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((item) => (
                    <Fragment key={item.id}>
                      <tr>
                        <td>{formatTimestamp(item.started_at)}</td>
                        <td>
                          {item.source === "public"
                            ? "Public API"
                            : "Model upstream"}
                          <span className="secondary-line">
                            {item.operation_attempt !== null
                              ? `Operation attempt ${item.operation_attempt}`
                              : "Operation attempt not recorded"}
                          </span>
                          {item.source === "upstream" ? (
                            <span className="secondary-line">
                              {item.upstream_attempt !== null
                                ? `Upstream attempt ${item.upstream_attempt}`
                                : "Upstream attempt not recorded"}
                            </span>
                          ) : null}
                        </td>
                        <td>
                          <code>
                            {item.method} {item.endpoint}
                          </code>
                          <span className="secondary-line">
                            {item.mcp_tool}
                          </span>
                          <span className="secondary-line app-id">
                            {item.request_id ?? "Request ID not recorded"}
                          </span>
                        </td>
                        <td>
                          {item.http_status !== null
                            ? `HTTP ${item.http_status}`
                            : "HTTP not observed"}
                          <span className="secondary-line">
                            {item.error_type}
                          </span>
                          {item.disconnected ? (
                            <span className="secondary-line">Disconnected</span>
                          ) : null}
                        </td>
                        <td>
                          {item.operation_id ? (
                            appId ? (
                              <Link
                                to={{
                                  pathname: `/admin/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(item.operation_id)}`,
                                  search: navigation.toString(),
                                }}
                              >
                                {item.operation_id}
                              </Link>
                            ) : (
                              <code>{item.operation_id}</code>
                            )
                          ) : (
                            "No operation"
                          )}
                        </td>
                        <td>
                          {item.request_observed_bytes.toLocaleString()} B
                          request /{" "}
                          {item.response_observed_bytes.toLocaleString()} B
                          response
                          <span className="secondary-line">
                            {item.request_complete && item.response_complete
                              ? "Complete"
                              : "Partial / incomplete"}
                            {item.request_redacted || item.response_redacted
                              ? " · Redacted"
                              : ""}
                          </span>
                        </td>
                        <td>
                          <button
                            type="button"
                            className="button"
                            aria-label={`${expanded === item.id ? "Collapse" : "Inspect"} exchange ${item.id}`}
                            aria-expanded={expanded === item.id}
                            onClick={() =>
                              setExpanded(expanded === item.id ? null : item.id)
                            }
                          >
                            {expanded === item.id ? "Close" : "Inspect"}
                          </button>
                        </td>
                      </tr>
                      {expanded === item.id ? (
                        <tr>
                          <td colSpan={7}>
                            <RequestDebugExchange
                              appId={appId}
                              exchangeId={item.id}
                            />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="pagination-actions">
              {cursor ? (
                <button
                  type="button"
                  className="button"
                  onClick={() => setPage({ scope })}
                >
                  Newest requests
                </button>
              ) : null}
              {data.next_cursor ? (
                <button
                  type="button"
                  className="button"
                  onClick={() => {
                    setExpanded(null);
                    setPage({ scope, cursor: data.next_cursor! });
                  }}
                >
                  Older requests
                </button>
              ) : (
                <span>End of recorded exchanges in this window</span>
              )}
            </div>
          </>
        )}
      </DataBoundary>
    </section>
  );
}

/** Global rejects may lack a model/App identity; fetch only when requested. */
export function GlobalRequestDebugLog() {
  const [open, setOpen] = useState(false);
  return (
    <section className="panel">
      <button
        type="button"
        className="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? "Hide" : "Show"} all request logs
      </button>
      <p className="supporting-copy">
        Inspect requests across Apps, including rejected requests with no App
        attribution.
      </p>
      {open ? <RequestDebugLog /> : null}
    </section>
  );
}
