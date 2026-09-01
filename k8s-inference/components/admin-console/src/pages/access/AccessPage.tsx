import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { adminApi, AdminApiError } from "../../api/client";
import type {
  AdminApiKey,
  AdminApiKeyCreateInput,
  AdminApiKeyDisclosure,
  AdminApiKeyPolicyPatchInput,
  AdminApiKeyRotateInput,
  OperatorPrincipal,
  OperatorPrincipalCreateInput,
  OperatorPrincipalPatchInput,
} from "../../api/accessTypes";
import { useSession } from "../../auth/SessionContext";
import { DataBoundary } from "../../components/DataBoundary";
import { OneTimeSecretDialog } from "../../components/OneTimeSecretDialog";
import { formatTimestamp } from "../../lib/format";
import {
  formatAccessMeasurement,
  measurementDescription,
  rolePermits,
} from "../../lib/access";
import { sharedContextParams } from "../../lib/search";
import {
  CreateKeyDialog,
  CreatePrincipalDialog,
  EditKeyDialog,
  EditPrincipalDialog,
  RevokeKeyDialog,
  RotateKeyDialog,
} from "./AccessDialogs";

type DialogState =
  | { kind: "create-principal" }
  | { kind: "edit-principal"; principal: OperatorPrincipal }
  | { kind: "create-key" }
  | { kind: "edit-key"; apiKey: AdminApiKey }
  | { kind: "rotate-key"; apiKey: AdminApiKey }
  | { kind: "revoke-key"; apiKey: AdminApiKey }
  | null;

function stateClass(state: AdminApiKey["state"]): string {
  return state === "active" ? "operation-state--succeeded" : "operation-state--failed";
}

function boundedTenant(value: string | null): string | undefined {
  if (!value || value.length > 120 || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(value)) return undefined;
  return value;
}

function ErrorText(error: unknown): string {
  if (error instanceof AdminApiError) {
    return `${error.message}${error.requestId ? ` · request ${error.requestId}` : ""}`;
  }
  return error instanceof Error ? error.message : "The access operation failed";
}

function AccessMetric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="metric-card">
      <span className="metric-card__label">{label}</span>
      <strong className="metric-card__value">{value}</strong>
      <span className="metric-card__detail">{detail}</span>
    </div>
  );
}

function ScopeList({ values, label }: { values: string[]; label: string }) {
  return (
    <span aria-label={`${label}: ${values.join(", ")}`} className="chip-list">
      {values.map((value) => <span className="mini-chip" key={value}>{value}</span>)}
    </span>
  );
}

function UsageCell({ apiKey }: { apiKey: AdminApiKey }) {
  const inputDescription = measurementDescription(apiKey.usage.input_tokens);
  const outputDescription = measurementDescription(apiKey.usage.output_tokens);
  const gpuQualifier = apiKey.usage.estimated_gpu_seconds.state === "estimated"
    ? " estimated"
    : apiKey.usage.estimated_gpu_seconds.state === "unavailable"
      ? ""
      : " accounted";
  return (
    <div className="dense-stack">
      <strong>{apiKey.usage.terminal_operations.toLocaleString()} operations</strong>
      <span title={measurementDescription(apiKey.usage.estimated_gpu_seconds)}>
        {formatAccessMeasurement(apiKey.usage.estimated_gpu_seconds)}{gpuQualifier}
      </span>
      <span title={inputDescription}>In {formatAccessMeasurement(apiKey.usage.input_tokens)}</span>
      <span title={outputDescription}>Out {formatAccessMeasurement(apiKey.usage.output_tokens)}</span>
      {apiKey.usage.modality_state === "available"
        ? apiKey.usage.modality_units.map((unit) => (
            <span key={`${unit.modality}/${unit.direction}/${unit.unit}`}>
              {unit.direction} {unit.modality}: {unit.amount.toLocaleString()} {unit.unit}
            </span>
          ))
        : <span title={apiKey.usage.modality_reason ?? undefined}>Modalities —</span>}
    </div>
  );
}

function LimitsCell({ apiKey }: { apiKey: AdminApiKey }) {
  const requestLimit = apiKey.request_budget === null
    ? "unlimited"
    : `${apiKey.requests_used.toLocaleString()} / ${apiKey.request_budget.toLocaleString()} requests`;
  const gpuLimit = apiKey.gpu_seconds_budget === null
    ? "unlimited GPU"
    : `${apiKey.gpu_seconds_used.toLocaleString()} used + ${apiKey.gpu_seconds_reserved.toLocaleString()} reserved / ${apiKey.gpu_seconds_budget.toLocaleString()} GPU-s`;
  const rate = apiKey.rate_limit_requests === null || apiKey.rate_window_seconds === null
    ? "rate limit off"
    : `${apiKey.rate_window_requests.toLocaleString()} / ${apiKey.rate_limit_requests.toLocaleString()} per ${apiKey.rate_window_seconds}s`;
  return (
    <div className="dense-stack">
      <span>{requestLimit}</span>
      <span>{gpuLimit}</span>
      <span>{rate}</span>
      <span>Concurrency {apiKey.max_concurrency}</span>
    </div>
  );
}

interface PrincipalTableProps {
  principals: OperatorPrincipal[];
  keys: AdminApiKey[] | null;
  activePrincipalId: string;
  canAdminister: boolean;
  onEdit: (principal: OperatorPrincipal) => void;
}

function PrincipalTable({ principals, keys, activePrincipalId, canAdminister, onEdit }: PrincipalTableProps) {
  return (
    <div className="table-frame">
      <table className="resource-table resource-table--access">
        <caption className="sr-only">Operator users and service principals</caption>
        <thead>
          <tr>
            <th scope="col">Principal</th>
            <th scope="col">Kind</th>
            <th scope="col">Role</th>
            <th scope="col">Tenant</th>
            <th scope="col">Key usage</th>
            <th scope="col">State</th>
            <th scope="col">Updated</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {principals.map((principal) => {
            const principalKeys = keys?.filter((apiKey) => apiKey.principal_id === principal.subject) ?? null;
            const operations = principalKeys?.reduce((sum, apiKey) => sum + apiKey.usage.terminal_operations, 0) ?? null;
            const self = principal.id === activePrincipalId;
            return (
              <tr key={principal.id}>
                <th scope="row">
                  {principal.display_name}
                  <span className="secondary-line">{principal.subject}</span>
                </th>
                <td>{principal.kind}</td>
                <td><span className="mini-chip">{principal.role}</span></td>
                <td>{principal.tenant_id ?? "Global"}</td>
                <td>
                  {principalKeys === null ? "—" : `${principalKeys.length.toLocaleString()} keys`}
                  <span className="secondary-line">{operations === null ? "Accounting unavailable" : `${operations.toLocaleString()} terminal operations`}</span>
                </td>
                <td>
                  <span className={`operation-state ${principal.enabled ? "operation-state--succeeded" : "operation-state--failed"}`}>
                    {principal.enabled ? "enabled" : "disabled"}
                  </span>
                </td>
                <td>
                  {formatTimestamp(principal.updated_at)}
                  <span className="secondary-line">by {principal.created_by}</span>
                </td>
                <td>
                  {canAdminister ? (
                    <button
                      className="text-button"
                      disabled={self}
                      onClick={() => onEdit(principal)}
                      title={self ? "The active principal cannot edit itself in this console" : undefined}
                      type="button"
                    >
                      Manage
                    </button>
                  ) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface KeyTableProps {
  keys: AdminApiKey[];
  canOperate: boolean;
  onEdit: (apiKey: AdminApiKey) => void;
  onRotate: (apiKey: AdminApiKey) => void;
  onRevoke: (apiKey: AdminApiKey) => void;
}

function KeyTable({ keys, canOperate, onEdit, onRotate, onRevoke }: KeyTableProps) {
  return (
    <div className="table-frame">
      <table className="resource-table resource-table--keys">
        <caption className="sr-only">Scoped inference API keys and usage</caption>
        <thead>
          <tr>
            <th scope="col">Key</th>
            <th scope="col">Principal</th>
            <th scope="col">Access</th>
            <th scope="col">Accounting</th>
            <th scope="col">Limits</th>
            <th scope="col">State</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((apiKey) => (
            <tr key={apiKey.id}>
              <th scope="row">
                {apiKey.name ?? "Unnamed key"}
                <code className="secondary-line">{apiKey.prefix}</code>
                <span className="secondary-line" title={apiKey.fingerprint ?? undefined}>
                  {apiKey.fingerprint ? `fingerprint ${apiKey.fingerprint.slice(0, 12)}…` : "fingerprint unavailable"}
                </span>
              </th>
              <td>
                {apiKey.principal_id}
                <span className="secondary-line">{apiKey.tenant_id}</span>
                <span className="secondary-line">Last used {formatTimestamp(apiKey.last_used_at)}</span>
              </td>
              <td><div className="dense-stack"><ScopeList label="Scopes" values={apiKey.scopes} /><ScopeList label="Models" values={apiKey.models} /></div></td>
              <td><UsageCell apiKey={apiKey} /></td>
              <td><LimitsCell apiKey={apiKey} /></td>
              <td>
                <span className={`operation-state ${stateClass(apiKey.state)}`}>{apiKey.state}</span>
                <span className="secondary-line">Expires {formatTimestamp(apiKey.expires_at)}</span>
                {apiKey.rotation_parent_id ? <span className="secondary-line">Rotated from {apiKey.rotation_parent_id.slice(0, 8)}…</span> : null}
              </td>
              <td>
                {canOperate && apiKey.state === "active" ? (
                  <div className="row-actions">
                    <button className="text-button" onClick={() => onEdit(apiKey)} type="button">Edit</button>
                    <button className="text-button" onClick={() => onRotate(apiKey)} type="button">Rotate</button>
                    <button className="text-button text-button--danger" onClick={() => onRevoke(apiKey)} type="button">Revoke</button>
                  </div>
                ) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AccessPage() {
  const queryClient = useQueryClient();
  const { session } = useSession();
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const initialLocationKey = useRef(location.key);
  const [search, setSearch] = useState("");
  const [dialog, setDialog] = useState<DialogState>(null);
  const [disclosure, setDisclosure] = useState<AdminApiKeyDisclosure | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const fixedTenant = session.principal.tenant_id;
  const requestedTenant = searchParams.get("tenant");
  const selectedTenant = fixedTenant ?? boundedTenant(requestedTenant);
  const canOperate = rolePermits(session.principal.role, "operator");
  const canAdminister = rolePermits(session.principal.role, "admin");

  useEffect(() => {
    if (initialLocationKey.current !== location.key) {
      setDisclosure(null);
      initialLocationKey.current = location.key;
    }
  }, [location.key]);

  const principalQuery = useQuery({
    queryKey: ["admin-access-principals", selectedTenant ?? "all"],
    queryFn: ({ signal }) => adminApi.principals(selectedTenant, signal),
  });
  const keyQuery = useQuery({
    queryKey: ["admin-access-keys", selectedTenant ?? "all"],
    queryFn: ({ signal }) => adminApi.keys(selectedTenant, signal),
  });

  const normalizedSearch = search.trim().toLocaleLowerCase();
  const principals = principalQuery.data?.data.items.filter((principal) => !normalizedSearch || [principal.display_name, principal.subject, principal.kind, principal.role, principal.tenant_id ?? "global"].some((value) => value.toLocaleLowerCase().includes(normalizedSearch))) ?? [];
  const keys = keyQuery.data?.data.items.filter((apiKey) => !normalizedSearch || [apiKey.name ?? "", apiKey.prefix, apiKey.principal_id, apiKey.tenant_id, ...apiKey.scopes, ...apiKey.models].some((value) => value.toLocaleLowerCase().includes(normalizedSearch))) ?? [];
  const allKeys = keyQuery.data?.data.items ?? [];
  const keyDataAvailable = keyQuery.data !== undefined;
  const principalDataAvailable = principalQuery.data !== undefined;
  const enabledPrincipalCount = principalQuery.data?.data.items.filter((principal) => principal.enabled).length ?? 0;
  const activeKeys = allKeys.filter((apiKey) => apiKey.state === "active").length;
  const operations = allKeys.reduce((sum, apiKey) => sum + apiKey.usage.terminal_operations, 0);
  const gpuUsageAvailable = keyDataAvailable && allKeys.every((apiKey) => apiKey.usage.estimated_gpu_seconds.value !== null);
  const gpuSeconds = gpuUsageAvailable
    ? allKeys.reduce((sum, apiKey) => sum + (apiKey.usage.estimated_gpu_seconds.value as number), 0)
    : null;
  const accessNavigation = sharedContextParams(searchParams);
  if (selectedTenant) accessNavigation.set("tenant", selectedTenant);

  function open(next: DialogState) {
    setActionError(null);
    setDialog(next);
  }

  function close() {
    setActionError(null);
    setDialog(null);
  }

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["admin-access-principals"] }),
      queryClient.invalidateQueries({ queryKey: ["admin-access-keys"] }),
      queryClient.invalidateQueries({ queryKey: ["admin-audit"] }),
    ]);
  }

  async function createPrincipal(payload: OperatorPrincipalCreateInput) {
    setBusy(true);
    setActionError(null);
    try {
      await adminApi.createPrincipal(payload);
      close();
      await refresh();
    } catch (error) {
      setActionError(ErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function updatePrincipal(principal: OperatorPrincipal, payload: OperatorPrincipalPatchInput) {
    setBusy(true);
    setActionError(null);
    try {
      await adminApi.updatePrincipal(principal.id, payload);
      close();
      await refresh();
    } catch (error) {
      setActionError(ErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function issueKey(payload: AdminApiKeyCreateInput) {
    setBusy(true);
    setActionError(null);
    try {
      const response = await adminApi.issueKey(payload);
      setDialog(null);
      setDisclosure(response.data);
      await refresh();
    } catch (error) {
      setActionError(ErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function updateKey(apiKey: AdminApiKey, payload: AdminApiKeyPolicyPatchInput) {
    setBusy(true);
    setActionError(null);
    try {
      await adminApi.updateKey(apiKey.id, payload);
      close();
      await refresh();
    } catch (error) {
      setActionError(ErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function rotateKey(apiKey: AdminApiKey, payload: AdminApiKeyRotateInput) {
    setBusy(true);
    setActionError(null);
    try {
      const response = await adminApi.rotateKey(apiKey.id, payload);
      setDialog(null);
      setDisclosure(response.data);
      await refresh();
    } catch (error) {
      setActionError(ErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function revokeKey(apiKey: AdminApiKey) {
    setBusy(true);
    setActionError(null);
    try {
      await adminApi.revokeKey(apiKey.id);
      close();
      await refresh();
    } catch (error) {
      setActionError(ErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  function changeTenant(value: string) {
    const next = new URLSearchParams(sharedContextParams(searchParams));
    value ? next.set("tenant", value) : next.delete("tenant");
    setSearchParams(next);
  }

  return (
    <div className="page-stack">
      <section className="metric-grid" aria-label="Access summary">
        <AccessMetric detail="Visible in the selected tenant scope" label="Principals" value={principalDataAvailable ? principals.length.toLocaleString() : "—"} />
        <AccessMetric detail={keyDataAvailable ? `${allKeys.length.toLocaleString()} total keys` : "Key projection unavailable"} label="Active keys" value={keyDataAvailable ? activeKeys.toLocaleString() : "—"} />
        <AccessMetric detail="Terminal ledger facts" label="Operations" value={keyDataAvailable ? operations.toLocaleString() : "—"} />
        <AccessMetric detail={gpuUsageAvailable ? "Admission accounting estimate" : "GPU accounting is unavailable for one or more visible keys"} label="GPU usage" value={gpuSeconds === null ? "—" : `${gpuSeconds.toLocaleString()} GPU-s`} />
      </section>

      <div className="toolbar toolbar--wrap">
        <label>Search<input autoComplete="off" onChange={(event) => setSearch(event.target.value)} placeholder="Principal, key, scope or model" type="search" value={search} /></label>
        {fixedTenant === null ? <label>Tenant<input maxLength={120} onChange={(event) => changeTenant(event.target.value)} placeholder="All tenants" value={selectedTenant ?? ""} /></label> : <span className="quiet-chip">Tenant {fixedTenant}</span>}
        <span className="toolbar__summary">Signed in as {session.principal.role}</span>
        {canAdminister ? <button className="button" onClick={() => open({ kind: "create-principal" })} type="button">Add principal</button> : null}
        {canOperate ? <button className="button button--primary" disabled={!principalDataAvailable || enabledPrincipalCount === 0} onClick={() => open({ kind: "create-key" })} title={!principalDataAvailable ? "Principal inventory must load before a key can be issued" : enabledPrincipalCount === 0 ? "Create or enable a principal before issuing a key" : undefined} type="button">Create API key</button> : null}
      </div>

      {!canOperate ? <div className="inline-notice" role="status">Viewer access is read-only. An operator can create, edit, rotate and revoke API keys; an administrator can also manage principals.</div> : null}
      {canOperate && principalDataAvailable && enabledPrincipalCount === 0 ? <div className="inline-notice inline-notice--warning" role="status">No enabled principal is available in this tenant scope. Create or enable a principal before issuing an API key.</div> : null}
      {fixedTenant === null && requestedTenant !== null && selectedTenant === undefined ? <div className="freshness-notice" role="status"><strong>Invalid tenant filter ignored</strong><span>Tenant identifiers must be 1–120 letters, numbers, dots, underscores or hyphens.</span></div> : null}

      <section className="page-stack" aria-labelledby="principals-heading">
        <div className="section-heading"><div><span className="eyebrow">Identity</span><h2 id="principals-heading">Users and service principals</h2></div><span className="quiet-chip">{principals.length} shown</span></div>
        <DataBoundary data={principalQuery.data} error={principalQuery.error} pending={principalQuery.isPending} empty={!principalQuery.isPending && principals.length === 0}>
          {() => (
            <PrincipalTable
              activePrincipalId={session.principal.id}
              canAdminister={canAdminister}
              keys={keyDataAvailable ? allKeys : null}
              onEdit={(principal) => open({ kind: "edit-principal", principal })}
              principals={principals}
            />
          )}
        </DataBoundary>
      </section>

      <section className="page-stack" aria-labelledby="keys-heading">
        <div className="section-heading"><div><span className="eyebrow">Runtime access</span><h2 id="keys-heading">Scoped API keys</h2></div><Link className="text-link" to={{ pathname: "/admin/audit", search: accessNavigation.toString() }}>View access audit</Link></div>
        <DataBoundary data={keyQuery.data} error={keyQuery.error} pending={keyQuery.isPending} empty={!keyQuery.isPending && keys.length === 0}>
          {() => (
            <KeyTable
              canOperate={canOperate}
              keys={keys}
              onEdit={(apiKey) => open({ kind: "edit-key", apiKey })}
              onRevoke={(apiKey) => open({ kind: "revoke-key", apiKey })}
              onRotate={(apiKey) => open({ kind: "rotate-key", apiKey })}
            />
          )}
        </DataBoundary>
      </section>

      {dialog?.kind === "create-principal" ? <CreatePrincipalDialog busy={busy} error={actionError} fixedTenant={fixedTenant} onClose={close} onSave={createPrincipal} /> : null}
      {dialog?.kind === "edit-principal" ? <EditPrincipalDialog busy={busy} error={actionError} onClose={close} onSave={(payload) => updatePrincipal(dialog.principal, payload)} principal={dialog.principal} /> : null}
      {dialog?.kind === "create-key" ? <CreateKeyDialog busy={busy} error={actionError} fixedTenant={fixedTenant !== null} onClose={close} onSave={issueKey} principals={principalQuery.data?.data.items ?? []} tenant={selectedTenant ?? ""} /> : null}
      {dialog?.kind === "edit-key" ? <EditKeyDialog apiKey={dialog.apiKey} busy={busy} error={actionError} onClose={close} onSave={(payload) => updateKey(dialog.apiKey, payload)} /> : null}
      {dialog?.kind === "rotate-key" ? <RotateKeyDialog apiKey={dialog.apiKey} busy={busy} error={actionError} onClose={close} onSave={(payload) => rotateKey(dialog.apiKey, payload)} /> : null}
      {dialog?.kind === "revoke-key" ? <RevokeKeyDialog apiKey={dialog.apiKey} busy={busy} error={actionError} onClose={close} onConfirm={() => revokeKey(dialog.apiKey)} /> : null}
      {disclosure ? <OneTimeSecretDialog disclosure={disclosure} onDismiss={() => setDisclosure(null)} /> : null}
    </div>
  );
}
