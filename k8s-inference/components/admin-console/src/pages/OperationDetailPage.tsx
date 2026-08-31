import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import { DataBoundary } from "../components/DataBoundary";
import { Measurement } from "../components/Measurement";
import { formatTimestamp } from "../lib/format";

export function OperationDetailPage() {
  const { operationId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const query = useQuery({ queryKey: ["admin-operation", operationId, searchParams.toString()], queryFn: ({ signal }) => adminApi.operation(operationId, searchParams, signal), enabled: Boolean(operationId) });
  return <DataBoundary data={query.data} error={query.error} pending={query.isPending}>{({ data }) => { const item = data.operation; return <div className="page-stack"><Link className="back-link" to={{ pathname: "/admin/operations", search: searchParams.toString() }}>← All operations</Link><section className="identity-panel"><div><span className="eyebrow">{item.model_id}</span><h2>{item.operation}</h2><code>{item.id}</code></div><span className={`operation-state operation-state--${item.status}`}>{item.status}</span></section><section className="panel"><div className="section-heading"><div><span className="eyebrow">Lifecycle</span><h2>Timing breakdown</h2></div></div><div className="metric-grid metric-grid--small"><Measurement value={item.timings.queue_seconds} /><Measurement value={item.timings.cold_start_seconds} /><Measurement value={item.timings.inference_seconds} /><Measurement value={item.timings.total_seconds} /></div><dl className="definition-grid"><div><dt>Accepted</dt><dd>{formatTimestamp(item.accepted_at)}</dd></div><div><dt>Completed</dt><dd>{formatTimestamp(item.completed_at)}</dd></div><div><dt>Principal</dt><dd>{item.principal_id}</dd></div><div><dt>API key</dt><dd>{item.api_key_prefix}</dd></div><div><dt>Attempt</dt><dd>{item.attempt} / {item.max_attempts}</dd></div><div><dt>Payloads</dt><dd>{data.payloads_exposed ? "Exposed" : "Never exposed"}</dd></div></dl></section></div>; }}</DataBoundary>;
}
