import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { envelopeRequest } from "../../api/client";
import type { Customer, CustomerList, Workbench, WorkbenchInventory } from "../../api/customerTypes";
import { useSession } from "../../auth/SessionContext";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { rolePermits } from "../../lib/access";
import { formatTimestamp } from "../../lib/format";
import { CustomerActions, WorkbenchUpgrade } from "./CustomerActions";

function bytes(value: number | null | undefined): string {
  return value == null ? "Unknown" : `${(value / 1e9).toFixed(2)} GB`;
}

export function customerWindowKey(params: URLSearchParams, live: boolean, range: string): string {
  const key = new URLSearchParams(params);
  if (live) {
    key.delete("from"); key.delete("to"); key.set("window", range);
  }
  return key.toString();
}

function WorkbenchCard({ value, releases, enabled, canManage }: { value: Workbench; releases: Record<string, string>; enabled: boolean; canManage: boolean }) {
  const observed = value.observation;
  return <article className="panel section-stack">
    <div className="section-heading"><h3>{value.name}</h3>
      {value.protected && <span className="badge">Protected — no changes</span>}</div>
    <p>{value.management === "managed" ? "Operator managed" : "Customer managed"} · {value.principal_ids.join(", ")}</p>
    <p>{value.observation_state === "available" ? observed?.state : `Status ${value.observation_state}`}
      {observed && <> · observed {formatTimestamp(observed.observed_at)}</>}</p>
    {observed?.url && <a className="button" href={observed.url} target="_blank" rel="noreferrer">Open LibreChat</a>}
    <dl className="detail-grid">
      <dt>Endpoint</dt><dd><code>{value.endpoint_id}</code></dd>
      <dt>Workspace bucket</dt><dd>{value.bucket_name ?? "Not bound"}</dd>
      <dt>CPU / memory shape</dt><dd>{observed ? `${observed.platform} / ${observed.preset}` : "Unknown"}</dd>
      <dt>Running image</dt><dd className="customer-image"><code>{observed?.image ?? "Unknown"}</code></dd>
      <dt>Desired release</dt><dd>{value.desired_release ?? "Current release retained"}</dd>
      <dt>Chat state</dt><dd>{value.state_filesystem_id ?? "Legacy local disk — migration required"}</dd>
    </dl>
    {value.protected && <p>{value.protection_reason}</p>}
    {canManage && <WorkbenchUpgrade value={value} releases={releases} enabled={enabled} />}
    {value.operations.length > 0 && <ul>{value.operations.map(operation =>
      <li key={operation.id}>{operation.kind}: {operation.state} — {formatTimestamp(operation.created_at)}
        {operation.error_code && ` (${operation.error_code})`}</li>)}</ul>}
  </article>;
}

function CustomerDetail({ customer, releases, enabled, canManage }: { customer: Customer; releases: Record<string, string>; enabled: boolean; canManage: boolean }) {
  const { navigation } = useAdminTimeWindow();
  return <>
    <section className="panel section-stack">
      <Link to={`/admin/customers?${navigation}`}>All customers</Link>
      <h2>{customer.profile.display_name}</h2>
      <p>{customer.tenant_id} · {customer.profile.purpose} · {customer.active_keys} active API keys</p>
      <div className="count-strip"><div><strong>{customer.requests}</strong><span>Accepted model requests</span></div>
        <div><strong>{customer.workbenches.length}</strong><span>Registered LibreChat instances</span></div>
        <div><strong>{customer.buckets.length}</strong><span>Workspace buckets</span></div></div>
    </section>
    <section className="panel section-stack"><h3>Models used in selected window</h3>
      <p>Actual accepted requests, not the access allowlist. Key and user grants are managed under Users.</p>
      {!customer.model_usage.length ? <p>No accepted model requests in this window.</p> :
        <div className="table-scroll"><table><thead><tr><th>Model</th><th>Requests</th><th>Succeeded</th><th>Failed</th><th>Pending / running</th><th>Last request</th></tr></thead>
          <tbody>{customer.model_usage.map(model => <tr key={model.model_id}><td>{model.model_id}</td><td>{model.requests}</td><td>{model.succeeded}</td><td>{model.failed}</td><td>{model.in_progress}</td><td>{formatTimestamp(model.last_request_at)}</td></tr>)}</tbody></table></div>}
    </section>
    <section className="panel section-stack"><h3>Users, keys and usage</h3>
      <div className="table-scroll"><table><thead><tr><th>User</th><th>Status</th><th>Active keys</th><th>Requests</th><th>GPU occupied</th><th>GPU occupied idle</th></tr></thead>
        <tbody>{customer.users.map(user => <tr key={user.id}>
          <td><Link to={`/admin/users/${user.id}?${navigation}`}>{user.display_name}</Link></td>
          <td>{user.enabled ? "Enabled" : "Disabled"}</td><td>{user.active_key_count}</td><td>{user.usage.requests}</td>
          <td>{user.usage.scheduler_occupied_gpu_seconds.value == null ? "Unknown" : `${(user.usage.scheduler_occupied_gpu_seconds.value / 3600).toFixed(3)} h (estimated)`}</td>
          <td>{user.usage.occupied_idle_gpu_seconds.value == null ? "Unknown" : `${(user.usage.occupied_idle_gpu_seconds.value / 3600).toFixed(3)} h (estimated)`}</td>
        </tr>)}</tbody></table></div>
      <p>Shared API keys identify the inference owner, not individual LibreChat logins. GPU figures retain the existing accounting coverage limitations.</p>
    </section>
    <section className="panel section-stack"><h3>Storage</h3>
      {!customer.buckets.length && <p>No platform workspace binding. Legacy buckets may appear in the cloud inventory.</p>}
      {customer.buckets.map(bucket => <article key={bucket.bucket_id}>
        <h4>{bucket.bucket_name}</h4><p>{bucket.mode === "tenant" ? "Shared tenant bucket" : `Private: ${bucket.owner_key}`} · {bucket.region}</p>
        <p>Reported current objects: {bytes(bucket.observation?.size_bytes)} · quota {bytes(bucket.quota_bytes)} · observation {bucket.observation_state}</p>
        <p><code>{bucket.bucket_id}</code></p>
      </article>)}
    </section>
    <section className="section-stack"><h3>LibreChat instances</h3>
      {!customer.workbenches.length && <p>No registered instance. Register the existing Serverless endpoint; do not create a duplicate.</p>}
      {customer.workbenches.map(value => <WorkbenchCard key={value.id} value={value} releases={releases} enabled={enabled} canManage={canManage} />)}
    </section>
    {canManage && <CustomerActions customer={customer} />}
  </>;
}

export function CustomersPage() {
  const { tenantId } = useParams();
  const { params, navigation, live, range } = useAdminTimeWindow();
  const { session } = useSession();
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [includeLegacy, setIncludeLegacy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The shared live clock advances every 15 seconds. Do not cancel an in-flight
  // seven-day aggregation by changing its query key on each tick.
  const query = useQuery({ queryKey: ["customers", tenantId, customerWindowKey(params, live, range)],
    refetchInterval: live ? 15000 : false,
    queryFn: ({ signal }) => envelopeRequest<CustomerList>(`/customers${tenantId ? `/${encodeURIComponent(tenantId)}` : ""}?${params}`, { signal }) });
  const canManage = Boolean(session && rolePermits(session.principal.role, "operator"));
  async function refresh() {
    setBusy(true); setError(null);
    try {
      await envelopeRequest("/workbench-inventory/refresh", { method: "POST" });
      await client.invalidateQueries({ queryKey: ["customers"] });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Inventory refresh failed"); }
    finally { setBusy(false); }
  }
  return <DataBoundary data={query.data} error={query.error} pending={query.isPending}>{({ data }) => {
    const rows = data.items.filter(customer => (includeLegacy || (!customer.profile.archived &&
      (customer.profile.purpose !== "legacy" || customer.workbenches.length > 0))) &&
      `${customer.tenant_id} ${customer.profile.display_name}`.toLowerCase().includes(search.toLowerCase()));
    return <div className="page-stack">
      <section className="panel section-heading"><div><h2>Customers</h2><p>Model usage, access, storage and Serverless LibreChat in one place.</p></div>
        <div className="button-row"><Link className="button" to={`/admin/customers-inventory?${navigation}`}>Cloud inventory</Link>
          {canManage && <button className="button" disabled={busy || !data.inventory_available} onClick={refresh}>{busy ? "Refreshing…" : "Refresh cloud state"}</button>}</div></section>
      {error && <p role="alert">{error}</p>}
      {data.inventory_error && <p role="status">Cloud observations may be unavailable or stale. Last refresh: {data.inventory_error}.</p>}
      {data.truncated && <p role="alert">User inventory is truncated; this is not a complete customer inventory.</p>}
      {tenantId && data.customer ? <CustomerDetail customer={data.customer} releases={data.releases ?? {}} enabled={data.lifecycle_executor_available} canManage={canManage} /> : <section className="panel section-stack">
        <label>Find customer <input value={search} onChange={event => setSearch(event.target.value)} placeholder="Name or tenant ID" /></label>
        <label><input type="checkbox" checked={includeLegacy} onChange={event => setIncludeLegacy(event.target.checked)} /> Include legacy and archived identities</label>
        <div className="table-scroll"><table><thead><tr><th>Customer</th><th>Purpose</th><th>Users / keys</th><th>Requests</th><th>Models used</th><th>Buckets</th><th>LibreChat</th><th>Last request</th></tr></thead>
          <tbody>{rows.map(customer => <tr key={customer.tenant_id}>
            <td><Link to={`/admin/customers/${encodeURIComponent(customer.tenant_id)}?${navigation}`}>{customer.profile.display_name}</Link></td>
            <td>{customer.profile.purpose}</td><td>{customer.users.length} / {customer.active_keys}</td><td>{customer.requests}</td>
            <td>{customer.model_usage.map(value => value.model_id).join(", ") || "None in window"}</td>
            <td>{customer.buckets.map(value => value.bucket_name).join(", ") || "Not bound"}</td>
            <td>{customer.workbenches.length ? customer.workbenches.map(value => `${value.name}: ${value.observation_state === "available" ? value.observation?.state : "Unknown"}`).join("; ") : "Not registered"}</td>
            <td>{customer.last_request_at ? formatTimestamp(customer.last_request_at) : "None in window"}</td>
          </tr>)}</tbody></table></div>
        {!rows.length && <p>No matching customers. Include legacy identities to find customers not yet classified.</p>}
        <p>{data.attribution}</p>
      </section>}
    </div>;
  }}</DataBoundary>;
}

export function CustomerInventoryPage() {
  const { params, navigation, live, range } = useAdminTimeWindow();
  const [kind, setKind] = useState("workbenches");
  const query = useQuery({ queryKey: ["workbench-inventory", customerWindowKey(params, live, range)],
    refetchInterval: live ? 15000 : false,
    queryFn: ({ signal }) => envelopeRequest<WorkbenchInventory>(`/workbench-inventory?${params}`, { signal }) });
  return <DataBoundary data={query.data} error={query.error} pending={query.isPending}>{({ data }) => {
    const rows = data.items.filter(item => kind === "all" || (kind === "buckets" ? item.kind === "bucket" : item.kind === "endpoint" && item.is_workbench));
    return <div className="page-stack"><section className="panel section-stack">
      <Link to={`/admin/customers?${navigation}`}>Customers</Link><h2>Cloud inventory</h2>
      <p>Includes unassigned and stopped resources. Unassigned does not mean safe to delete. Model-serving endpoints and infrastructure storage are separate from customer workbenches.</p>
      <label>Show <select value={kind} onChange={event => setKind(event.target.value)}><option value="workbenches">LibreChat endpoints</option><option value="buckets">Buckets</option><option value="all">All observed resources</option></select></label>
      <p>{rows.length} resources</p><div className="table-scroll"><table><thead><tr><th>Resource</th><th>Customer</th><th>State</th><th>Mounts / references</th><th>Last observed</th></tr></thead>
        <tbody>{rows.map(item => <tr key={item.resource_id}><td>{item.name}<br /><code>{item.resource_id}</code></td>
          <td>{item.tenant_id ?? "Unassigned"}{item.protected && " · Protected"}</td>
          <td>{item.observation_state === "available" ? item.state ?? bytes(item.size_bytes) : item.observation_state}</td>
          <td>{item.kind === "endpoint" ? item.buckets?.join(", ") || "No bucket" : `${item.mounted_by?.length ?? 0} endpoint references${item.purpose ? ` · ${item.purpose}` : ""}`}</td>
          <td>{formatTimestamp(item.observed_at)}</td></tr>)}</tbody></table></div>
    </section></div>;
  }}</DataBoundary>;
}
