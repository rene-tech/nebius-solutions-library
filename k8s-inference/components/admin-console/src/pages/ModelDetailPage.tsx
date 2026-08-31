import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import { DataBoundary } from "../components/DataBoundary";
import { MetricCard, Measurement } from "../components/Measurement";
import { StatusChip } from "../components/StatusChip";
import { formatTimestamp } from "../lib/format";

export function ModelDetailPage() {
  const { modelId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const query = useQuery({
    queryKey: ["admin-model", modelId, searchParams.toString()],
    queryFn: ({ signal }) => adminApi.model(modelId, searchParams, signal),
    enabled: Boolean(modelId),
  });

  return (
    <DataBoundary data={query.data} error={query.error} pending={query.isPending}>
      {({ data }) => {
        const { identity, runtime, metrics } = data.model;
        const qualification = identity.qualification?.states ?? null;
        return (
          <div className="page-stack">
            <Link className="back-link" to={{ pathname: "/admin/models", search: searchParams.toString() }}>← All models</Link>
            <section className="identity-panel">
              <div><span className="eyebrow">{identity.family}</span><h2>{identity.display_name}</h2><code>{identity.id}</code></div>
              <StatusChip state={runtime.state} reason={runtime.reason} />
            </section>
            <div className="metric-grid">
              <MetricCard label="Requests" value={metrics.requests_per_second} />
              <MetricCard label="Tokens" value={metrics.tokens_per_second} />
              <MetricCard label="Error rate" value={metrics.error_rate} />
              <MetricCard label="Cold start" value={metrics.cold_start_seconds} />
            </div>
            <div className="split-grid">
              <section className="panel"><div className="section-heading"><div><span className="eyebrow">Runtime</span><h2>Serving state</h2></div></div><dl className="definition-grid"><div><dt>Ready replicas</dt><dd>{runtime.ready_replicas ?? "—"}</dd></div><div><dt>Desired replicas</dt><dd>{runtime.desired_replicas ?? "—"}</dd></div><div><dt>Queued operations</dt><dd>{runtime.queued_operations ?? "—"}</dd></div><div><dt>Last observed</dt><dd>{formatTimestamp(runtime.observed_at)}</dd></div><div><dt>Semantic health</dt><dd>{runtime.semantic_healthy === null ? "Unknown" : runtime.semantic_healthy ? "Passing" : "Failing"}</dd></div><div><dt>Activation</dt><dd>{runtime.activation_phase ?? "Unknown"}</dd></div></dl></section>
              <section className="panel"><div className="section-heading"><div><span className="eyebrow">Placement</span><h2>Accelerator profile</h2></div></div><dl className="definition-grid"><div><dt>GPU class</dt><dd>{identity.gpu_class}</dd></div><div><dt>GPU count</dt><dd>{identity.gpu_count}</dd></div><div><dt>Execution</dt><dd>{identity.execution_mode}</dd></div><div><dt>Runtime</dt><dd>{identity.runtime_kind}</dd></div><div><dt>MCP</dt><dd>{identity.mcp_exposed ? identity.mcp_tool_name ?? "Exposed" : "Not exposed"}</dd></div><div><dt>Protocols</dt><dd>{identity.protocols.join(", ")}</dd></div></dl></section>
            </div>
            <div className="split-grid">
              <section className="panel"><div className="section-heading"><div><span className="eyebrow">Catalog</span><h2>Active runtime origin</h2></div></div><dl className="definition-grid"><div><dt>Origin</dt><dd>{identity.active_runtime?.kind ?? "Not projected"}</dd></div><div><dt>Variant</dt><dd><code>{identity.active_runtime?.variant_id ?? "canonical"}</code></dd></div><div><dt>Source</dt><dd>{identity.active_runtime?.source_kind ?? "—"}</dd></div><div><dt>Repository</dt><dd>{identity.active_runtime?.repository ?? "—"}</dd></div><div><dt>Relationship</dt><dd>{identity.active_runtime?.relationship ?? "—"}</dd></div><div><dt>NIM artifact parity</dt><dd>{identity.active_runtime?.nim_artifact_parity ?? "—"}</dd></div></dl></section>
              <section className="panel"><div className="section-heading"><div><span className="eyebrow">Evidence snapshot</span><h2>Qualification</h2></div></div><dl className="definition-grid"><div><dt>Observed</dt><dd>{formatTimestamp(identity.qualification?.observed_at ?? null)}</dd></div><div><dt>Registered</dt><dd>{qualification ? qualification.registered ? "Qualified" : "Not qualified" : "Not projected"}</dd></div><div><dt>Route active</dt><dd>{qualification ? qualification.route_active ? "Qualified" : "Not qualified" : "Not projected"}</dd></div><div><dt>Runtime Ready</dt><dd>{qualification ? qualification.runtime_ready ? "Observed Ready" : "Not observed Ready" : "Not projected"}</dd></div><div><dt>Semantic</dt><dd>{qualification ? qualification.semantic_qualified ? "Qualified" : "Not qualified" : "Not projected"}</dd></div><div><dt>HTTP + MCP</dt><dd>{qualification ? qualification.http_mcp_qualified ? "Qualified" : "Not qualified" : "Not projected"}</dd></div><div><dt>Cold start</dt><dd>{qualification ? qualification.cold_start_qualified ? "Qualified" : "Not qualified" : "Not projected"}</dd></div><div><dt>Elasticity</dt><dd>{qualification ? qualification.elasticity_qualified ? "Qualified" : "Not qualified" : "Not projected"}</dd></div></dl><p className="supporting-copy">This is retained evidence at the observed timestamp, not current readiness. Current replicas and serving state above come from live cluster adapters. Policy: {identity.policy.non_clinical ? "non-clinical only" : "standard use"}; commercial use {identity.policy.commercial_use}; license {identity.policy.license_id}.</p></section>
            </div>
            <section className="panel"><div className="section-heading"><div><span className="eyebrow">Fast start</span><h2>Snapshot and cache</h2></div></div><div className="metric-grid metric-grid--small"><MetricCard label="Snapshot restore" value={data.snapshot_restore_seconds} /><MetricCard label="Cache residency" value={data.cache_residency_bytes} /><MetricCard label="Phase evidence" value={data.cold_start_phase_breakdown} /><MetricCard label="TTFT p95" value={metrics.latency.ttft_p95_seconds} /></div><p className="supporting-copy">Unavailable values remain unknown with their source reason; readiness alone never marks a model hot.</p></section>
          </div>
        );
      }}
    </DataBoundary>
  );
}
