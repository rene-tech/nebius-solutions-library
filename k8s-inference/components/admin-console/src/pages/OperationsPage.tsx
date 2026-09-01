import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import type { OperationState } from "../api/types";
import { useSession } from "../auth/SessionContext";
import { DataBoundary } from "../components/DataBoundary";
import { Measurement } from "../components/Measurement";
import { formatTimestamp } from "../lib/format";
import { sharedContextParams } from "../lib/search";

const operationStates = [
  "queued",
  "activating",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "preempted",
  "expired",
] as const satisfies readonly OperationState[];
const operationStateSet = new Set<string>(operationStates);

function bounded(value: string | null, maximum: number, pattern?: RegExp): string | undefined {
  if (!value || value.length > maximum || (pattern && !pattern.test(value))) return undefined;
  return value;
}

export function OperationsPage() {
  const { session } = useSession();
  const [searchParams, setSearchParams] = useSearchParams();
  const context = sharedContextParams(searchParams);
  const fixedTenant = session.principal.tenant_id ?? undefined;
  const rawStatus = searchParams.get("status");
  const status = rawStatus && operationStateSet.has(rawStatus) ? rawStatus as OperationState : undefined;
  const cursor = bounded(searchParams.get("cursor"), 512);
  const tenantId = fixedTenant ?? bounded(searchParams.get("tenant"), 120, /^[A-Za-z0-9][A-Za-z0-9_.-]*$/);
  const modelId = bounded(searchParams.get("model"), 128);
  const principalId = bounded(searchParams.get("principal"), 200);
  const apiKeyPrefix = bounded(searchParams.get("key"), 64);
  const errorCode = bounded(searchParams.get("error"), 64, /^[a-z][a-z0-9_]*$/);
  const navigationParams = new URLSearchParams(context);
  if (!fixedTenant && tenantId) navigationParams.set("tenant", tenantId);
  if (modelId) navigationParams.set("model", modelId);
  if (principalId) navigationParams.set("principal", principalId);
  if (apiKeyPrefix) navigationParams.set("key", apiKeyPrefix);
  if (status) navigationParams.set("status", status);
  if (errorCode) navigationParams.set("error", errorCode);
  if (cursor) navigationParams.set("cursor", cursor);
  const invalidFilter = (rawStatus !== null && status === undefined)
    || (searchParams.has("cursor") && cursor === undefined)
    || (!fixedTenant && searchParams.has("tenant") && tenantId === undefined)
    || (searchParams.has("model") && modelId === undefined)
    || (searchParams.has("principal") && principalId === undefined)
    || (searchParams.has("key") && apiKeyPrefix === undefined)
    || (searchParams.has("error") && errorCode === undefined);
  const query = useQuery({
    queryKey: [
      "admin-operations",
      context.toString(),
      cursor ?? "first",
      tenantId ?? "all-tenants",
      modelId ?? "all-models",
      principalId ?? "all-principals",
      apiKeyPrefix ?? "all-keys",
      status ?? "all-states",
      errorCode ?? "all-errors",
    ],
    queryFn: ({ signal }) => adminApi.operations(context, {
      cursor,
      tenantId,
      modelId,
      principalId,
      apiKeyPrefix,
      status,
      errorCode,
      limit: 100,
    }, signal),
  });

  function update(key: string, value: string) {
    const next = new URLSearchParams(navigationParams);
    value ? next.set(key, value) : next.delete(key);
    next.delete("cursor");
    setSearchParams(next, { replace: true });
  }

  function nextPage(nextCursor: string) {
    const next = new URLSearchParams(navigationParams);
    next.set("cursor", nextCursor);
    setSearchParams(next);
  }

  const items = query.data?.data.items ?? [];
  return (
    <div className="page-stack">
      <div className="toolbar toolbar--wrap" aria-label="Operation filters">
        <label>Model<input maxLength={128} onChange={(event) => update("model", event.target.value)} placeholder="All models" value={modelId ?? ""} /></label>
        <label>Principal<input maxLength={200} onChange={(event) => update("principal", event.target.value)} placeholder="All principals" value={principalId ?? ""} /></label>
        <label>Status<select onChange={(event) => update("status", event.target.value)} value={status ?? ""}><option value="">All states</option>{operationStates.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <label>Error code<input maxLength={64} onChange={(event) => update("error", event.target.value)} pattern="[a-z][a-z0-9_]*" placeholder="All errors" value={errorCode ?? ""} /></label>
        {fixedTenant ? <span className="quiet-chip">Tenant {fixedTenant}</span> : <label>Tenant<input maxLength={120} onChange={(event) => update("tenant", event.target.value)} placeholder="All tenants" value={tenantId ?? ""} /></label>}
        <span className="toolbar__summary">{items.length} operations on this page</span>
      </div>
      {invalidFilter ? <div className="freshness-notice" role="status"><strong>Invalid filter ignored</strong><span>Use a published status and bounded model, principal, tenant, key or error identifiers.</span></div> : null}
      <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && items.length === 0}>
        {({ data }) => (
          <div className="page-stack">
            <div className="table-frame">
              <table className="resource-table">
                <caption className="sr-only">Recent inference operations</caption>
                <thead><tr><th scope="col">Operation</th><th scope="col">Model</th><th scope="col">Principal</th><th scope="col">Status</th><th scope="col">Accepted</th><th scope="col">Total</th><th scope="col">GPU</th><th scope="col">Outcome</th></tr></thead>
                <tbody>{data.items.map((item) => (
                  <tr key={item.id}>
                    <th scope="row"><Link className="resource-link" to={{ pathname: `/admin/operations/${item.id}`, search: navigationParams.toString() }}>{item.operation}</Link><span className="secondary-line">{item.id.slice(0, 8)}… · {item.protocol}</span></th>
                    <td><Link className="resource-link" to={{ pathname: `/admin/models/${encodeURIComponent(item.model_id)}`, search: sharedContextParams(searchParams).toString() }}>{item.model_id}</Link><span className="secondary-line">{item.model_revision}</span></td>
                    <td>{item.principal_id}<span className="secondary-line">key {item.api_key_prefix} · {item.tenant_id}</span></td>
                    <td><span className={`operation-state operation-state--${item.status}`}>{item.status}</span></td>
                    <td>{formatTimestamp(item.accepted_at)}</td>
                    <td><Measurement compact value={item.timings.total_seconds} /></td>
                    <td>{item.gpu_count}<span className="secondary-line">{item.preemptible === null ? "unknown capacity" : item.preemptible ? "preemptible" : "regular"}</span></td>
                    <td>{item.outcome ?? "Pending"}<span className="secondary-line">HTTP {item.http_status ?? "—"} · {item.error_class ?? "no error"}</span></td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            {data.next_cursor ? <div className="pagination-actions"><span>More operations are available.</span><button className="button" onClick={() => nextPage(data.next_cursor as string)} type="button">Next page</button></div> : <p className="pagination-summary">End of the selected operation window.</p>}
          </div>
        )}
      </DataBoundary>
    </div>
  );
}
