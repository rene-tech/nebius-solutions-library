import { useQuery } from "@tanstack/react-query";
import { Link, NavLink, useParams } from "react-router-dom";
import { appsApi } from "../../api/appsClient";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import {
  AppMetricsTab,
  AppLogsTab,
  AppContainersTab,
} from "./AppTelemetryTabs";
import { AppRunsTab } from "./AppRunsTab";
import { AppUsageTab } from "./AppUsageTab";
import { AppSettingsTab } from "./AppSettingsTab";
import { AppRunDetail } from "./AppRunDetail";

const tabs = [
  ["runs", "Runs"],
  ["metrics", "Metrics"],
  ["logs", "App Logs"],
  ["containers", "Containers"],
  ["usage", "Usage"],
  ["settings", "Settings"],
] as const;
export function AppDetailPage() {
  const { appId = "", tab = "runs", runId } = useParams();
  const { params, navigation } = useAdminTimeWindow();
  const query = useQuery({
    queryKey: ["app-detail", appId],
    queryFn: ({ signal }) => appsApi.detail(appId, params, signal),
    enabled: Boolean(appId),
    refetchInterval: 15000,
  });
  return (
    <DataBoundary
      data={query.data}
      error={query.error}
      pending={query.isPending}
    >
      {({ data }) => (
        <div className="page-stack">
          <Link
            className="back-link"
            to={{ pathname: "/admin/apps", search: navigation.toString() }}
          >
            ← All apps
          </Link>
          <div className="app-heading">
            <div>
              <h2>{data.display_name}</h2>
              <p>
                {data.model_ref} ·{" "}
                {data.execution_mode === "scientific"
                  ? "Batch workflow"
                  : "Serving"}
              </p>
              <code className="app-id">App {data.app_id}</code>
              <p>
                Public route: <code>{data.public_model_id}</code>
              </p>
            </div>
            <div>
              <span className="mini-chip">{data.status}</span>
              <p>{data.status_reason}</p>
            </div>
          </div>
          <nav className="app-tabs" aria-label="App sections">
            {tabs.map(([key, label]) => (
              <NavLink
                key={key}
                to={{
                  pathname: `/admin/apps/${encodeURIComponent(appId)}/${key}`,
                  search: navigation.toString(),
                }}
              >
                {label}
              </NavLink>
            ))}
          </nav>
          {runId ? (
            <AppRunDetail appId={appId} runId={runId} />
          ) : tab === "runs" ? (
            <AppRunsTab appId={appId} />
          ) : tab === "metrics" ? (
            <AppMetricsTab appId={appId} />
          ) : tab === "logs" ? (
            <AppLogsTab appId={appId} />
          ) : tab === "containers" ? (
            <AppContainersTab appId={appId} />
          ) : tab === "usage" ? (
            <AppUsageTab appId={appId} />
          ) : tab === "settings" ? (
            <AppSettingsTab app={data} />
          ) : (
            <p className="state-panel">
              This app section does not exist. Choose a tab above.
            </p>
          )}
        </div>
      )}
    </DataBoundary>
  );
}
