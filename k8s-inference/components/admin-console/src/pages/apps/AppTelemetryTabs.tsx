import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { appsApi } from "../../api/appsClient";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { TimeSeriesChart } from "../../components/TimeSeriesChart";
import { formatTimestamp } from "../../lib/format";

export function AppMetricsTab({ appId }: { appId: string }) {
  const { params } = useAdminTimeWindow();
  const query = useQuery({
    queryKey: ["app-metrics", appId, params.toString()],
    queryFn: ({ signal }) => appsApi.metrics(appId, params, signal),
  });
  return (
    <DataBoundary
      data={query.data}
      error={query.error}
      pending={query.isPending}
    >
      {({ data }) => (
        <>
          <p className="window-caption">
            {formatTimestamp(data.from_at)} – {formatTimestamp(data.to_at)} ·{" "}
            {data.step_seconds}s sampling. Unknown intervals are gaps, not zero.
          </p>
          <div className="chart-grid-layout">
            {data.charts.map((chart) => (
              <TimeSeriesChart key={chart.id} {...chart} />
            ))}
          </div>
          {!data.charts.length ? (
            <p className="state-panel">
              No metric series are available for this app.
            </p>
          ) : null}
        </>
      )}
    </DataBoundary>
  );
}

export function AppLogsTab({ appId }: { appId: string }) {
  const { params, navigation } = useAdminTimeWindow();
  const [search, setSearch] = useSearchParams();
  const text = (search.get("log_search") ?? "").slice(0, 256);
  const pod = (search.get("pod") ?? "").slice(0, 253);
  const container = (search.get("container") ?? "").slice(0, 253);
  const cursor = search.get("log_cursor") ?? undefined;
  const filters = {
    search: text || undefined,
    pod: pod || undefined,
    container: container || undefined,
    cursor,
  };
  const query = useQuery({
    queryKey: [
      "app-logs",
      appId,
      params.toString(),
      text,
      pod,
      container,
      cursor,
    ],
    queryFn: ({ signal }) => appsApi.logs(appId, params, filters, signal),
  });
  function set(key: string, value: string) {
    const next = new URLSearchParams(search);
    next.delete("log_cursor");
    value ? next.set(key, value) : next.delete(key);
    setSearch(next, { replace: true });
  }
  return (
    <div className="page-stack">
      <div className="toolbar toolbar--wrap">
        <label>
          Search logs
          <input
            aria-label="Search logs"
            value={text}
            maxLength={256}
            onChange={(event) => set("log_search", event.target.value)}
          />
        </label>
        <label>
          Pod
          <input
            aria-label="Log pod"
            value={pod}
            maxLength={253}
            onChange={(event) => set("pod", event.target.value)}
          />
        </label>
        <label>
          Container
          <input
            aria-label="Log container"
            value={container}
            maxLength={253}
            onChange={(event) => set("container", event.target.value)}
          />
        </label>
      </div>
      <DataBoundary
        data={query.data}
        error={query.error}
        pending={query.isPending}
      >
        {({ data }) => (
          <>
            <p className="window-caption">
              {data.source} · {data.state}
              {data.reason ? ` · ${data.reason}` : ""}
            </p>
            {data.items.length ? (
              <div className="table-frame">
                <table className="resource-table app-log-table">
                  <caption className="sr-only">App logs</caption>
                  <thead>
                    <tr>
                      <th>Time / level</th>
                      <th>Instance</th>
                      <th>Message</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((line, index) => (
                      <tr key={`${line.at}-${index}`}>
                        <td>
                          {formatTimestamp(line.at)}
                          <span className="secondary-line">
                            {line.level ?? "Level not reported"}
                          </span>
                          {line.run_id ? (
                            <Link
                              to={{
                                pathname: `/admin/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(line.run_id)}`,
                                search: navigation.toString(),
                              }}
                            >
                              Run
                            </Link>
                          ) : null}
                        </td>
                        <td>
                          <code>{line.pod}</code>
                          <span className="secondary-line">
                            {line.namespace} / {line.container}
                          </span>
                        </td>
                        <td className="app-log-message">{line.message}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="state-panel">
                {data.reason ??
                  "No log lines match this app, time window and search."}
              </p>
            )}
            {data.truncated ? (
              <p className="inline-notice">
                Results are bounded. Narrow the time range or instance filter to
                inspect more precisely.
              </p>
            ) : null}
            {data.next_cursor ? (
              <button
                className="button"
                onClick={() => {
                  const next = new URLSearchParams(search);
                  next.set("log_cursor", data.next_cursor!);
                  setSearch(next);
                }}
              >
                Older log lines
              </button>
            ) : null}
          </>
        )}
      </DataBoundary>
    </div>
  );
}

export function AppContainersTab({ appId }: { appId: string }) {
  const { params, navigation } = useAdminTimeWindow();
  const query = useQuery({
    queryKey: ["app-containers", appId, params.toString()],
    queryFn: ({ signal }) => appsApi.containers(appId, params, signal),
  });
  return (
    <DataBoundary
      data={query.data}
      error={query.error}
      pending={query.isPending}
    >
      {({ data }) => (
        <>
          <p className="window-caption">
            {data.total} container records · {data.source} · {data.state}
            {data.reason ? ` · ${data.reason}` : ""}. Instances are not customer
            runs.
          </p>
          {data.items.length ? (
            <div className="table-frame">
              <table className="resource-table">
                <caption className="sr-only">App containers</caption>
                <thead>
                  <tr>
                    <th>Container / instance</th>
                    <th>State</th>
                    <th>Node / GPU</th>
                    <th>Lifecycle</th>
                    <th>Image / restarts</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((item) => (
                    <tr key={item.id}>
                      <th scope="row">
                        {item.container_name}
                        <span className="secondary-line">{item.pod_name}</span>
                        <span className="secondary-line app-id">
                          {item.pod_uid}
                        </span>
                        <Link
                          to={{
                            pathname: `/admin/apps/${encodeURIComponent(appId)}/logs`,
                            search: `${navigation.toString()}&pod=${encodeURIComponent(item.pod_name)}&container=${encodeURIComponent(item.container_name)}`,
                          }}
                        >
                          Logs
                        </Link>
                        {item.run_id ? (
                          <>
                            {" "}
                            ·{" "}
                            <Link
                              to={{
                                pathname: `/admin/apps/${encodeURIComponent(appId)}/runs/${encodeURIComponent(item.run_id)}`,
                                search: navigation.toString(),
                              }}
                            >
                              Run
                            </Link>
                          </>
                        ) : null}
                      </th>
                      <td>
                        {item.state}
                        <span className="secondary-line">
                          Ready:{" "}
                          {item.ready === null
                            ? "Unknown"
                            : item.ready
                              ? "Yes"
                              : "No"}
                        </span>
                        <span className="secondary-line">{item.reason}</span>
                      </td>
                      <td>
                        {item.node_name ?? "Not assigned"}
                        {Object.entries(item.gpu_resources).map(
                          ([name, count]) => (
                            <span key={name} className="secondary-line">
                              {count} × {name}
                            </span>
                          ),
                        )}
                      </td>
                      <td>
                        Start {formatTimestamp(item.started_at)}
                        <span className="secondary-line">
                          End {formatTimestamp(item.finished_at)}
                        </span>
                      </td>
                      <td>
                        <code className="app-id">{item.image}</code>
                        <span className="secondary-line">
                          Restarts: {item.restarts ?? "Unknown"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="state-panel">
              {data.reason ??
                "No container instances are observed for this app. Cold and job-only apps may have none."}
            </p>
          )}
          {data.truncated ? (
            <p className="inline-notice">
              Container results are truncated; this is not a complete history.
            </p>
          ) : null}
        </>
      )}
    </DataBoundary>
  );
}
