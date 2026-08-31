import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import { DataBoundary } from "../components/DataBoundary";
import { Measurement } from "../components/Measurement";
import { formatTimestamp } from "../lib/format";

export function OperationsPage() {
  const [searchParams] = useSearchParams();
  const query = useQuery({ queryKey: ["admin-operations", searchParams.toString()], queryFn: ({ signal }) => adminApi.operations(searchParams, signal) });
  return (
    <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && query.data?.data.items.length === 0}>
      {({ data }) => <div className="table-frame"><table className="resource-table"><caption className="sr-only">Recent inference operations</caption><thead><tr><th scope="col">Operation</th><th scope="col">Model</th><th scope="col">Principal</th><th scope="col">Status</th><th scope="col">Accepted</th><th scope="col">Total</th><th scope="col">GPU</th><th scope="col">Error</th></tr></thead><tbody>{data.items.map((item) => <tr key={item.id}><th scope="row"><Link className="resource-link" to={{ pathname: `/admin/operations/${item.id}`, search: searchParams.toString() }}>{item.operation}</Link><span className="secondary-line">{item.id.slice(0, 8)}…</span></th><td>{item.model_id}</td><td>{item.principal_id}<span className="secondary-line">key {item.api_key_prefix}</span></td><td><span className={`operation-state operation-state--${item.status}`}>{item.status}</span></td><td>{formatTimestamp(item.accepted_at)}</td><td><Measurement compact value={item.timings.total_seconds} /></td><td>{item.gpu_count}<span className="secondary-line">{item.preemptible === null ? "unknown capacity" : item.preemptible ? "preemptible" : "regular"}</span></td><td>{item.error_class ?? "—"}</td></tr>)}</tbody></table></div>}
    </DataBoundary>
  );
}
