import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import { DataBoundary } from "../components/DataBoundary";
import { MetricCard } from "../components/Measurement";
import { formatTimestamp } from "../lib/format";
import { sharedContextParams } from "../lib/search";

export function OperationDetailPage() {
  const { operationId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const context = sharedContextParams(searchParams);
  const query = useQuery({
    queryKey: ["admin-operation", operationId, context.toString()],
    queryFn: ({ signal }) => adminApi.operation(operationId, context, signal),
    enabled: Boolean(operationId),
  });

  return (
    <DataBoundary data={query.data} error={query.error} pending={query.isPending}>
      {({ data }) => {
        const item = data.operation;
        return (
          <div className="page-stack">
            <Link className="back-link" to={{ pathname: "/admin/operations", search: context.toString() }}>← All operations</Link>
            <section className="identity-panel">
              <div><span className="eyebrow">{item.model_id}</span><h2>{item.operation}</h2><code>{item.id}</code></div>
              <span className={`operation-state operation-state--${item.status}`}>{item.status}</span>
            </section>

            <section className="panel">
              <div className="section-heading"><div><span className="eyebrow">Lifecycle</span><h2>Timing breakdown</h2></div></div>
              <div className="metric-grid metric-grid--small">
                <MetricCard label="Queue" value={item.timings.queue_seconds} />
                <MetricCard label="Cold start" value={item.timings.cold_start_seconds} />
                <MetricCard label="Inference" value={item.timings.inference_seconds} />
                <MetricCard label="Total" value={item.timings.total_seconds} />
              </div>
            </section>

            <div className="split-grid">
              <section className="panel">
                <div className="section-heading"><div><span className="eyebrow">Request</span><h2>Identity and routing</h2></div></div>
                <dl className="definition-grid">
                  <div><dt>Accepted</dt><dd>{formatTimestamp(item.accepted_at)}</dd></div>
                  <div><dt>Completed</dt><dd>{formatTimestamp(item.completed_at)}</dd></div>
                  <div><dt>Tenant</dt><dd>{item.tenant_id}</dd></div>
                  <div><dt>Principal</dt><dd>{item.principal_id}</dd></div>
                  <div><dt>API key</dt><dd>{item.api_key_prefix}</dd></div>
                  <div><dt>Protocol</dt><dd>{item.protocol}</dd></div>
                  <div><dt>Model revision</dt><dd>{item.model_revision}</dd></div>
                  <div><dt>Attempt</dt><dd>{item.attempt} / {item.max_attempts}</dd></div>
                  <div><dt>Payloads</dt><dd>{data.payloads_exposed ? "Exposed" : "Never exposed"}</dd></div>
                </dl>
              </section>
              <section className="panel">
                <div className="section-heading"><div><span className="eyebrow">Outcome</span><h2>Execution result</h2></div></div>
                <dl className="definition-grid">
                  <div><dt>Outcome</dt><dd>{item.outcome ?? "Pending"}</dd></div>
                  <div><dt>Semantic outcome</dt><dd>{item.semantic_outcome ?? "Not reported"}</dd></div>
                  <div><dt>HTTP status</dt><dd>{item.http_status ?? "Not reported"}</dd></div>
                  <div><dt>Error class</dt><dd>{item.error_class ?? "No error"}</dd></div>
                  <div><dt>GPU count</dt><dd>{item.gpu_count}</dd></div>
                  <div><dt>Capacity</dt><dd>{item.preemptible === null ? "Unknown" : item.preemptible ? "Preemptible" : "Regular"}</dd></div>
                </dl>
              </section>
            </div>

            <section className="panel">
              <div className="section-heading"><div><span className="eyebrow">Accounting</span><h2>Usage evidence</h2></div></div>
              <div className="metric-grid metric-grid--small">
                <MetricCard label="Estimated GPU" value={item.estimated_gpu_seconds} />
                <MetricCard label="Input tokens" value={item.input_tokens} />
                <MetricCard label="Output tokens" value={item.output_tokens} />
                <MetricCard label="TTFT" value={item.timings.ttft_seconds} />
              </div>
            </section>
          </div>
        );
      }}
    </DataBoundary>
  );
}
