import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { envelopeRequest } from "../../api/client";
import type { Customer, Workbench, WorkbenchInventory } from "../../api/customerTypes";

export function CustomerActions({ customer }: { customer: Customer }) {
  const client = useQueryClient();
  const [name, setName] = useState(customer.profile.display_name);
  const [purpose, setPurpose] = useState(customer.profile.purpose);
  const [endpoint, setEndpoint] = useState("");
  const [principal, setPrincipal] = useState(customer.users[0]?.principal_id ?? "");
  const [protectedInstance, setProtectedInstance] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inventory = useQuery({ queryKey: ["workbench-inventory"],
    queryFn: ({ signal }) => envelopeRequest<WorkbenchInventory>("/workbench-inventory", { signal }) });
  const candidates = (inventory.data?.data.items ?? []).filter(value => value.kind === "endpoint" &&
    value.is_workbench && !value.workbench_id && value.observation_state === "available" &&
    value.buckets?.length === 1 && customer.buckets.some(bucket => bucket.bucket_name === value.buckets?.[0]));

  async function save(register = false) {
    setBusy(true); setError(null);
    try {
      if (register) {
        const selected = candidates.find(value => value.resource_id === endpoint);
        if (!selected) throw new Error("Choose a fresh observed endpoint belonging to this customer.");
        await envelopeRequest("/workbenches", { method: "POST", body: {
          tenant_id: customer.tenant_id, principal_ids: [principal], name: selected.name,
          project_id: selected.project_id, endpoint_id: selected.resource_id,
          management: "managed", protected: protectedInstance,
          protection_reason: protectedInstance ? "Owner hold: do not modify" : "",
        } });
        setEndpoint("");
      } else {
        await envelopeRequest(`/customers/${encodeURIComponent(customer.tenant_id)}/profile`, {
          method: "PUT", body: { display_name: name, purpose, archived: customer.profile.archived },
        });
      }
      await client.invalidateQueries({ queryKey: ["customers"] });
      await client.invalidateQueries({ queryKey: ["workbench-inventory"] });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Customer update failed"); }
    finally { setBusy(false); }
  }
  return <details className="panel section-stack"><summary>Customer settings and instance registration</summary>
    <label>Display name <input value={name} onChange={event => setName(event.target.value)} /></label>
    <label>Purpose <select value={purpose} onChange={event => setPurpose(event.target.value)}>
      {["customer", "internal", "system", "demo", "speech", "legacy"].map(value => <option key={value}>{value}</option>)}
    </select></label>
    <button className="button" disabled={busy || !name.trim()} onClick={() => save()}>Save customer</button>
    <h4>Register existing LibreChat</h4><p>This records ownership only. It does not restart the endpoint, create a bucket, or change keys.</p>
    <label>Existing endpoint <select value={endpoint} onChange={event => setEndpoint(event.target.value)}>
      <option value="">Choose an endpoint</option>{candidates.map(value =>
        <option key={value.resource_id} value={value.resource_id}>{value.name} — {value.state}</option>)}
    </select></label>
    <label>Inference key owner <select value={principal} onChange={event => setPrincipal(event.target.value)}>
      {customer.users.map(value => <option key={value.id} value={value.principal_id}>{value.display_name}</option>)}
    </select></label>
    <p>Separate LibreChat logins may intentionally share this inference key owner.</p>
    <label><input type="checkbox" checked={protectedInstance} onChange={event => setProtectedInstance(event.target.checked)} /> Protect this instance from lifecycle changes</label>
    <button className="button" disabled={busy || !endpoint || !principal} onClick={() => save(true)}>Register instance</button>
    {error && <p role="alert">{error}</p>}
  </details>;
}

export function WorkbenchUpgrade({ value, releases, enabled }: {
  value: Workbench; releases: Record<string, string>; enabled: boolean;
}) {
  const client = useQueryClient();
  const [release, setRelease] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (value.protected) return null;
  if (!value.state_filesystem_id) return <p>Migration required: chats and logins still use local endpoint storage. Do not stop or replace this instance before exporting its state.</p>;
  if (value.management !== "managed") return <p>Customer-managed instance. Use the same persistent filesystem when updating it in Serverless.</p>;
  if (!enabled || !Object.keys(releases).length) return <p>No qualified upgrade release is currently enabled.</p>;
  const active = value.operations.some(item => ["queued", "running", "awaiting_confirmation"].includes(item.state));
  async function upgrade() {
    setBusy(true); setError(null);
    try {
      await envelopeRequest(`/workbenches/${value.id}/operations`, { method: "POST", body: {
        kind: "upgrade", target_release: release, expected_revision: value.revision,
        idempotency_key: crypto.randomUUID(), confirm_interruption: true,
      } });
      setConfirmed(false);
      await client.invalidateQueries({ queryKey: ["customers"] });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Upgrade could not be requested"); }
    finally { setBusy(false); }
  }
  return <div className="section-stack"><label>Release <select value={release} onChange={event => setRelease(event.target.value)}>
    <option value="">Choose a qualified release</option>{Object.keys(releases).map(name => <option key={name}>{name}</option>)}
  </select></label><p>Replaces only the runtime. Accounts, chats, agent settings, uploads, keys and the workspace bucket remain. The Serverless URL changes; the current link is shown here.</p>
    <label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> Active chat turns are finished; a brief LibreChat interruption is acceptable.</label>
    <button className="button" disabled={busy || active || !confirmed || !release} onClick={upgrade}>
      {active ? "Upgrade in progress" : busy ? "Requesting…" : "Upgrade LibreChat"}
    </button>{error && <p role="alert">{error}</p>}
  </div>;
}
