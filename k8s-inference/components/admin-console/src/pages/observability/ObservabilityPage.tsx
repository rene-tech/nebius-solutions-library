import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import type { AdminObservabilityComponent } from "../../api/types";
import { adminApi } from "../../api/client";
import { DataBoundary } from "../../components/DataBoundary";
import { MetricCard } from "../../components/Measurement";
import { formatTimestamp } from "../../lib/format";
import { isUuid, verifiedObservabilityLaunch } from "../../lib/observability";
import { sharedContextParams } from "../../lib/search";

function booleanLabel(value: boolean | null, yes: string, no: string): string {
  return value === null ? "Unknown" : value ? yes : no;
}

function ComponentCard({ component }: { component: AdminObservabilityComponent }) {
  const launch = verifiedObservabilityLaunch(component);
  const suppressed = component.launch.enabled && launch === null;
  return (
    <article className="observability-card">
      <header>
        <div><span className="eyebrow">{component.id}</span><h3>{component.display_name}</h3></div>
        <span className={`capability-chip capability-chip--${component.health}`}>{component.health}</span>
      </header>
      <dl>
        <div><dt>Installed</dt><dd>{booleanLabel(component.installed, "Yes", "No")}</dd></div>
        <div><dt>Data</dt><dd>{booleanLabel(component.data_present, "Present", "Absent")}</dd></div>
        <div><dt>Version</dt><dd>{component.version ?? "Not reported"}</dd></div>
        <div><dt>Observed</dt><dd>{formatTimestamp(component.observed_at)}</dd></div>
      </dl>
      {component.reason ? <p className="component-reason">{component.reason}</p> : null}
      {launch ? (
        <a aria-label={`Open ${component.display_name}${component.id === "grafana" ? "" : " in Grafana"}`} className="button observability-launch" href={launch} rel="noopener noreferrer" target="_blank">{component.id === "grafana" ? "Open Grafana" : "Open in Grafana"}<span className="sr-only"> in a new tab</span></a>
      ) : (
        <div className="launch-disabled" role="status">
          {suppressed ? "Launch suppressed: the response failed browser safety checks." : component.launch.reason ?? "No verified launch is available."}
        </div>
      )}
    </article>
  );
}

export function ObservabilityPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const context = sharedContextParams(searchParams);
  const contextSearch = context.toString();
  const requestedModel = searchParams.get("model_id");
  const requestedOperation = searchParams.get("operation_id");
  const modelsQuery = useQuery({
    queryKey: ["admin-observability-model-options", contextSearch],
    queryFn: ({ signal }) => adminApi.models(context, { limit: 256 }, signal),
  });
  const operationsQuery = useQuery({
    queryKey: ["admin-observability-operation-options", contextSearch],
    queryFn: ({ signal }) => adminApi.operations(context, { limit: 200 }, signal),
  });
  const modelIds = new Set(modelsQuery.data?.data.items.map((item) => item.identity.id) ?? []);
  const operationIds = new Set(operationsQuery.data?.data.items.map((item) => item.id) ?? []);
  const modelId = requestedModel && modelIds.has(requestedModel) ? requestedModel : undefined;
  const operationId = isUuid(requestedOperation) && operationIds.has(requestedOperation) ? requestedOperation : undefined;
  const selectorsReady = (!requestedModel || modelsQuery.isFetched) && (!requestedOperation || operationsQuery.isFetched);
  const query = useQuery({
    queryKey: ["admin-observability", contextSearch, modelId ?? "all", operationId ?? "all"],
    queryFn: ({ signal }) => adminApi.observability(context, { modelId, operationId }, signal),
    enabled: selectorsReady,
  });
  const selectorValidationUnavailable = (requestedModel !== null && modelsQuery.isError)
    || (requestedOperation !== null && operationsQuery.isError);
  const ignoredFilter = (requestedModel !== null && modelId === undefined && modelsQuery.isFetched && !modelsQuery.isError)
    || (requestedOperation !== null && operationId === undefined && operationsQuery.isFetched && !operationsQuery.isError);
  const optionsUnavailable = modelsQuery.isError || operationsQuery.isError;

  function updateFilter(key: "model_id" | "operation_id", value: string) {
    const next = new URLSearchParams(searchParams);
    value ? next.set(key, value) : next.delete(key);
    setSearchParams(next, { replace: true });
  }

  return (
    <div className="page-stack">
      <section className="panel observability-intro">
        <div><span className="eyebrow">Telemetry launchpad</span><h2>Health, signals and verified tools</h2></div>
        <p>Component discovery, target health, accepted data and external launch are independent states. Raw Prometheus and Loki endpoints stay private.</p>
      </section>

      <section className="toolbar toolbar--wrap observability-filters" aria-label="Observability context">
        <label>Model<select aria-label="Model context" disabled={modelsQuery.isPending || modelsQuery.isError} onChange={(event) => updateFilter("model_id", event.target.value)} value={modelId ?? ""}><option value="">All models</option>{modelsQuery.data?.data.items.map((item) => <option key={item.identity.id} value={item.identity.id}>{item.identity.display_name}</option>)}</select></label>
        <label>Operation<select aria-label="Operation context" disabled={operationsQuery.isPending || operationsQuery.isError} onChange={(event) => updateFilter("operation_id", event.target.value)} value={operationId ?? ""}><option value="">All recent operations</option>{operationsQuery.data?.data.items.map((item) => <option key={item.id} value={item.id}>{item.id.slice(0, 8)}… · {item.model_id}</option>)}</select></label>
        <span className="toolbar__summary">Only selected model and operation identifiers are sent to server-owned dashboards.</span>
      </section>
      {ignoredFilter ? <div className="freshness-notice" role="status"><strong>Unsafe or unknown filter ignored</strong><span>Select a model or recent operation from the server-published options.</span></div> : null}
      {selectorValidationUnavailable ? <div className="freshness-notice" role="status"><strong>Requested filter was not applied</strong><span>The server-published selector list is unavailable, so the console safely requested all-scope telemetry instead.</span></div> : optionsUnavailable ? <div className="freshness-notice" role="status"><strong>Some filter options are unavailable</strong><span>All-scope observability remains available; retry this page before selecting a missing model or operation.</span></div> : null}

      <DataBoundary data={query.data} error={query.error} pending={query.isPending || !selectorsReady}>
        {({ data }) => (
          <>
            <section className="metric-grid" aria-label="Observability signals">
              <MetricCard label="GPU utilization" value={data.signals.gpu_utilization_ratio} />
              <MetricCard label="GPU memory utilization" value={data.signals.gpu_memory_utilization_ratio} />
              <MetricCard label="OTel refused items" value={data.signals.otel_refused_items_per_second} />
              <MetricCard label="OTel export failures" value={data.signals.otel_export_failures_per_second} />
            </section>
            <section className="observability-grid" aria-label="Observability components">
              {data.components.length ? data.components.map((component) => <ComponentCard component={component} key={component.id} />) : <div className="state-panel state-panel--empty">No observability components were published for this cluster.</div>}
            </section>
          </>
        )}
      </DataBoundary>
    </div>
  );
}
