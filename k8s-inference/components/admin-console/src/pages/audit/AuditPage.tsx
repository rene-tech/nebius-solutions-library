import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { AuditEvent } from "../../api/accessTypes";
import { adminApi } from "../../api/client";
import { useSession } from "../../auth/SessionContext";
import { DataBoundary } from "../../components/DataBoundary";
import { formatTimestamp } from "../../lib/format";

function boundedTenant(value: string | null): string | undefined {
  if (!value || value.length > 120 || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(value)) return undefined;
  return value;
}

function detailText(detail: Record<string, unknown>): string {
  const entries = Object.entries(detail);
  if (entries.length === 0) return "No additional detail";
  return entries
    .slice(0, 12)
    .map(([key, value]) => {
      if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
        return `${key}: ${String(value)}`;
      }
      return `${key}: ${JSON.stringify(value)}`;
    })
    .join(" · ")
    .slice(0, 1200);
}

function matches(event: AuditEvent, search: string, outcome: string): boolean {
  if (outcome !== "all" && event.outcome !== outcome) return false;
  if (!search) return true;
  return [event.actor, event.action, event.target_type, event.target_id, event.tenant_id ?? "global", event.outcome, detailText(event.detail)]
    .some((value) => value.toLocaleLowerCase().includes(search));
}

export function AuditPage() {
  const { session } = useSession();
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const fixedTenant = session.principal.tenant_id;
  const selectedTenant = fixedTenant ?? boundedTenant(searchParams.get("tenant"));
  const requestedOutcome = searchParams.get("outcome");
  const outcome = requestedOutcome === "succeeded" || requestedOutcome === "failed"
    ? requestedOutcome
    : "all";
  const rawLimit = Number(searchParams.get("limit") ?? "200");
  const limit = [50, 100, 200, 500, 1000].includes(rawLimit) ? rawLimit : 200;
  const query = useQuery({
    queryKey: ["admin-audit", selectedTenant ?? "all", limit],
    queryFn: ({ signal }) => adminApi.audit(selectedTenant, limit, signal),
  });
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const items = query.data?.data.items.filter((event) => matches(event, normalizedSearch, outcome)) ?? [];

  function update(key: string, value: string, defaultValue?: string) {
    const next = new URLSearchParams(searchParams);
    value === defaultValue || value === "" ? next.delete(key) : next.set(key, value);
    setSearchParams(next);
  }

  return (
    <div className="page-stack">
      <section className="panel audit-intro">
        <div><span className="eyebrow">Durable control-plane ledger</span><h2>Append-only administrative events</h2></div>
        <p>Key, principal and session lifecycle actions are recorded here. Inference requests remain in Operations so administrative changes are not mixed with runtime traffic.</p>
      </section>
      <div className="toolbar toolbar--wrap">
        <label>Search<input autoComplete="off" onChange={(event) => setSearch(event.target.value)} placeholder="Actor, action or target" type="search" value={search} /></label>
        <label>Outcome<select onChange={(event) => update("outcome", event.target.value, "all")} value={outcome}><option value="all">All outcomes</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option></select></label>
        {fixedTenant === null ? <label>Tenant<input maxLength={120} onChange={(event) => update("tenant", event.target.value)} placeholder="All tenants" value={selectedTenant ?? ""} /></label> : <span className="quiet-chip">Tenant {fixedTenant}</span>}
        <label>Rows<select onChange={(event) => update("limit", event.target.value, "200")} value={limit}>{[50, 100, 200, 500, 1000].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
        <span className="toolbar__summary">{items.length} of {query.data?.data.items.length ?? "—"} events</span>
      </div>
      <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && items.length === 0}>
        {() => (
          <div className="table-frame">
            <table className="resource-table resource-table--audit">
              <caption className="sr-only">Administrative audit events</caption>
              <thead><tr><th scope="col">Time</th><th scope="col">Actor</th><th scope="col">Action</th><th scope="col">Target</th><th scope="col">Tenant</th><th scope="col">Outcome</th><th scope="col">Detail</th></tr></thead>
              <tbody>{items.map((event) => <tr key={event.id}><td>{formatTimestamp(event.occurred_at)}<span className="secondary-line">event {event.id}</span></td><th scope="row">{event.actor}</th><td><code>{event.action}</code></td><td>{event.target_type}<span className="secondary-line" title={event.target_id}>{event.target_id}</span>{event.token_id ? <span className="secondary-line">key {event.token_id.slice(0, 8)}…</span> : null}</td><td>{event.tenant_id ?? "Global"}</td><td><span className={`operation-state ${event.outcome === "succeeded" ? "operation-state--succeeded" : "operation-state--failed"}`}>{event.outcome}</span></td><td><span className="audit-detail" title={detailText(event.detail)}>{detailText(event.detail)}</span></td></tr>)}</tbody>
            </table>
          </div>
        )}
      </DataBoundary>
    </div>
  );
}
