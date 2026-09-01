import { type FormEvent, useState } from "react";
import type {
  AdminApiKey,
  AdminApiKeyCreateInput,
  AdminApiKeyPolicyPatchInput,
  AdminApiKeyRotateInput,
  OperatorPrincipal,
  OperatorPrincipalCreateInput,
  OperatorPrincipalPatchInput,
  OperatorRole,
  PrincipalKind,
} from "../../api/accessTypes";
import { Modal } from "../../components/Modal";
import {
  datetimeLocal,
  optionalInteger,
  optionalIso,
  optionalNumber,
  splitCsv,
} from "../../lib/access";

export const SCOPE_OPTIONS = [
  "catalog.read",
  "inference.invoke",
  "mcp.invoke",
  "operations.read",
  "operations.result",
  "operations.cancel",
  "operations.acknowledge",
  "tokens.manage",
  "audit.read",
  "tenant.admin",
  "use.nonclinical",
  "use.noncommercial",
] as const;

function FormError({ local, remote }: { local: string | null; remote: string | null }) {
  const message = local ?? remote;
  return message ? <div className="inline-notice inline-notice--error" role="alert">{message}</div> : null;
}

interface CreatePrincipalProps {
  fixedTenant: string | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (payload: OperatorPrincipalCreateInput) => Promise<void>;
}

export function CreatePrincipalDialog({ fixedTenant, busy, error, onClose, onSave }: CreatePrincipalProps) {
  const [subject, setSubject] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [kind, setKind] = useState<PrincipalKind>("human");
  const [role, setRole] = useState<OperatorRole>("viewer");
  const [tenant, setTenant] = useState(fixedTenant ?? "");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSave({
      subject: subject.trim(),
      display_name: displayName.trim(),
      kind,
      role,
      tenant_id: fixedTenant ?? (tenant.trim() || null),
    });
  }

  return (
    <Modal description="Create a human or service operator identity. API runtime principals remain separate and are selected when a key is issued." onClose={onClose} title="Add principal">
      <form className="form-grid" onSubmit={(event) => void submit(event)}>
        <label>Display name<input maxLength={200} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label>
        <label>Subject<input autoComplete="off" maxLength={200} onChange={(event) => setSubject(event.target.value)} pattern="[A-Za-z0-9][A-Za-z0-9_.:@/-]*" required spellCheck={false} value={subject} /></label>
        <label>Kind<select onChange={(event) => setKind(event.target.value as PrincipalKind)} value={kind}><option value="human">Human</option><option value="service">Service</option></select></label>
        <label>Role<select onChange={(event) => setRole(event.target.value as OperatorRole)} value={role}><option value="viewer">Viewer</option><option value="operator">Operator</option><option value="admin">Admin</option></select></label>
        <label className="form-grid__wide">Tenant<input disabled={fixedTenant !== null} maxLength={120} onChange={(event) => setTenant(event.target.value)} pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" placeholder="Empty creates a global principal" value={tenant} /></label>
        <FormError local={null} remote={error} />
        <div className="modal-actions form-grid__wide"><button className="button" onClick={onClose} type="button">Cancel</button><button className="button button--primary" disabled={busy} type="submit">{busy ? "Creating…" : "Create principal"}</button></div>
      </form>
    </Modal>
  );
}

interface EditPrincipalProps {
  principal: OperatorPrincipal;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (payload: OperatorPrincipalPatchInput) => Promise<void>;
}

export function EditPrincipalDialog({ principal, busy, error, onClose, onSave }: EditPrincipalProps) {
  const [displayName, setDisplayName] = useState(principal.display_name);
  const [role, setRole] = useState<OperatorRole>(principal.role);
  const [enabled, setEnabled] = useState(principal.enabled);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSave({ display_name: displayName.trim(), role, enabled });
  }

  return (
    <Modal description={`${principal.subject} · ${principal.tenant_id ?? "global"}`} onClose={onClose} title="Manage principal">
      <form className="form-grid" onSubmit={(event) => void submit(event)}>
        <label className="form-grid__wide">Display name<input maxLength={200} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label>
        <label>Role<select onChange={(event) => setRole(event.target.value as OperatorRole)} value={role}><option value="viewer">Viewer</option><option value="operator">Operator</option><option value="admin">Admin</option></select></label>
        <label className="checkbox-field"><input checked={enabled} onChange={(event) => setEnabled(event.target.checked)} type="checkbox" />Enabled</label>
        <FormError local={null} remote={error} />
        <div className="modal-actions form-grid__wide"><button className="button" onClick={onClose} type="button">Cancel</button><button className="button button--primary" disabled={busy} type="submit">{busy ? "Saving…" : "Save changes"}</button></div>
      </form>
    </Modal>
  );
}

interface KeyPolicyFieldsProps {
  name: string;
  setName: (value: string) => void;
  scopes: Set<string>;
  setScopes: (value: Set<string>) => void;
  models: string;
  setModels: (value: string) => void;
  expiry: string;
  setExpiry: (value: string) => void;
  requestBudget: string;
  setRequestBudget: (value: string) => void;
  gpuBudget: string;
  setGpuBudget: (value: string) => void;
  maxConcurrency: string;
  setMaxConcurrency: (value: string) => void;
  rateRequests: string;
  setRateRequests: (value: string) => void;
  rateWindow: string;
  setRateWindow: (value: string) => void;
}

function KeyPolicyFields(props: KeyPolicyFieldsProps) {
  function toggleScope(scope: string, checked: boolean) {
    const next = new Set(props.scopes);
    checked ? next.add(scope) : next.delete(scope);
    props.setScopes(next);
  }

  return (
    <>
      <label className="form-grid__wide">Key name<input maxLength={120} onChange={(event) => props.setName(event.target.value)} required value={props.name} /></label>
      <fieldset className="scope-fieldset form-grid__wide">
        <legend>Scopes</legend>
        <div className="checkbox-grid">
          {SCOPE_OPTIONS.map((scope) => <label key={scope}><input checked={props.scopes.has(scope)} onChange={(event) => toggleScope(scope, event.target.checked)} type="checkbox" />{scope}</label>)}
        </div>
      </fieldset>
      <label className="form-grid__wide">Allowed models<input aria-describedby="model-scope-help" maxLength={1024} onChange={(event) => props.setModels(event.target.value)} required spellCheck={false} value={props.models} /><small id="model-scope-help">Comma-separated model IDs, or <code>*</code> for all models.</small></label>
      <label>Expires at<input min={datetimeLocal(new Date(Date.now() + 60_000).toISOString())} onChange={(event) => props.setExpiry(event.target.value)} type="datetime-local" value={props.expiry} /></label>
      <label>Max concurrency<input max="100" min="1" onChange={(event) => props.setMaxConcurrency(event.target.value)} required step="1" type="number" value={props.maxConcurrency} /></label>
      <label>Request budget<input min="1" onChange={(event) => props.setRequestBudget(event.target.value)} placeholder="Unlimited" step="1" type="number" value={props.requestBudget} /></label>
      <label>GPU-seconds budget<input min="0.01" onChange={(event) => props.setGpuBudget(event.target.value)} placeholder="Unlimited" step="0.01" type="number" value={props.gpuBudget} /></label>
      <label>Rate-limit requests<input min="1" onChange={(event) => props.setRateRequests(event.target.value)} placeholder="Disabled" step="1" type="number" value={props.rateRequests} /></label>
      <label>Rate window seconds<input max="86400" min="1" onChange={(event) => props.setRateWindow(event.target.value)} placeholder="Disabled" step="1" type="number" value={props.rateWindow} /></label>
    </>
  );
}

interface ParsedKeyPolicy {
  name: string;
  scopes: string[];
  models: string[];
  expires_at: string | null;
  request_budget: number | null;
  gpu_seconds_budget: number | null;
  max_concurrency: number;
  rate_limit_requests: number | null;
  rate_window_seconds: number | null;
}

function policyPayload(fields: {
  name: string;
  scopes: Set<string>;
  models: string;
  expiry: string;
  requestBudget: string;
  gpuBudget: string;
  maxConcurrency: string;
  rateRequests: string;
  rateWindow: string;
}): ParsedKeyPolicy {
  const scopes = [...fields.scopes];
  const models = splitCsv(fields.models);
  if (scopes.length === 0) throw new Error("Select at least one scope");
  if (models.length === 0) throw new Error("Enter at least one model or wildcard");
  const maxConcurrency = optionalInteger(fields.maxConcurrency);
  if (maxConcurrency === null || maxConcurrency > 100) throw new Error("Max concurrency must be between 1 and 100");
  const rateLimit = optionalInteger(fields.rateRequests);
  const rateWindow = optionalInteger(fields.rateWindow);
  if ((rateLimit === null) !== (rateWindow === null)) throw new Error("Set both rate-limit fields or leave both empty");
  if (rateLimit !== null && rateLimit > 1_000_000) throw new Error("Rate-limit requests exceeds 1,000,000");
  if (rateWindow !== null && rateWindow > 86_400) throw new Error("Rate window exceeds 86,400 seconds");
  return {
    name: fields.name.trim(),
    scopes,
    models,
    expires_at: optionalIso(fields.expiry),
    request_budget: optionalInteger(fields.requestBudget),
    gpu_seconds_budget: optionalNumber(fields.gpuBudget),
    max_concurrency: maxConcurrency,
    rate_limit_requests: rateLimit,
    rate_window_seconds: rateWindow,
  };
}

interface CreateKeyProps {
  principals: OperatorPrincipal[];
  tenant: string;
  fixedTenant: boolean;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (payload: AdminApiKeyCreateInput) => Promise<void>;
}

export function CreateKeyDialog({ principals, tenant: initialTenant, fixedTenant, busy, error, onClose, onSave }: CreateKeyProps) {
  const enabledPrincipals = principals.filter((principal) => principal.enabled);
  const defaultPrincipal = enabledPrincipals.find((principal) => principal.tenant_id === initialTenant && initialTenant !== "")
    ?? enabledPrincipals.find((principal) => principal.tenant_id !== null)
    ?? enabledPrincipals[0];
  const [principalId, setPrincipalId] = useState(defaultPrincipal?.subject ?? "");
  const [tenant, setTenant] = useState(initialTenant || defaultPrincipal?.tenant_id || "");
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState(new Set<string>(["inference.invoke", "mcp.invoke"]));
  const [models, setModels] = useState("*");
  const [expiry, setExpiry] = useState("");
  const [requestBudget, setRequestBudget] = useState("");
  const [gpuBudget, setGpuBudget] = useState("");
  const [maxConcurrency, setMaxConcurrency] = useState("1");
  const [rateRequests, setRateRequests] = useState("");
  const [rateWindow, setRateWindow] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);
    try {
      const policy = policyPayload({ name, scopes, models, expiry, requestBudget, gpuBudget, maxConcurrency, rateRequests, rateWindow });
      if (!principalId) throw new Error("Select an enabled principal");
      if (!tenant.trim()) throw new Error("Tenant is required for an inference API key");
      await onSave({
        ...policy,
        name: name.trim(),
        principal_id: principalId,
        tenant_id: tenant.trim(),
        max_concurrency: policy.max_concurrency ?? 1,
      });
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : "Key policy is invalid");
    }
  }

  function changePrincipal(subject: string) {
    setPrincipalId(subject);
    const selected = enabledPrincipals.find((principal) => principal.subject === subject);
    if (!fixedTenant && selected?.tenant_id) setTenant(selected.tenant_id);
  }

  return (
    <Modal description="The credential is displayed once after the server creates it. Only non-secret metadata appears in future views." onClose={onClose} title="Create API key">
      <form className="form-grid" onSubmit={(event) => void submit(event)}>
        <label>Principal<select disabled={enabledPrincipals.length === 0} onChange={(event) => changePrincipal(event.target.value)} required value={principalId}>{enabledPrincipals.length === 0 ? <option value="">No enabled principals available</option> : enabledPrincipals.map((principal) => <option key={principal.id} value={principal.subject}>{principal.display_name} · {principal.subject}</option>)}</select></label>
        <label>Tenant<input disabled={fixedTenant} maxLength={120} onChange={(event) => setTenant(event.target.value)} pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" required value={tenant} /></label>
        <KeyPolicyFields {...{ name, setName, scopes, setScopes, models, setModels, expiry, setExpiry, requestBudget, setRequestBudget, gpuBudget, setGpuBudget, maxConcurrency, setMaxConcurrency, rateRequests, setRateRequests, rateWindow, setRateWindow }} />
        <FormError local={localError} remote={error} />
        <div className="modal-actions form-grid__wide"><button className="button" onClick={onClose} type="button">Cancel</button><button className="button button--primary" disabled={busy || enabledPrincipals.length === 0} type="submit">{busy ? "Creating…" : "Create key"}</button></div>
      </form>
    </Modal>
  );
}

interface EditKeyProps {
  apiKey: AdminApiKey;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (payload: AdminApiKeyPolicyPatchInput) => Promise<void>;
}

export function EditKeyDialog({ apiKey, busy, error, onClose, onSave }: EditKeyProps) {
  const [name, setName] = useState(apiKey.name ?? apiKey.prefix);
  const [scopes, setScopes] = useState(new Set(apiKey.scopes));
  const [models, setModels] = useState(apiKey.models.join(", "));
  const [expiry, setExpiry] = useState(datetimeLocal(apiKey.expires_at));
  const [requestBudget, setRequestBudget] = useState(apiKey.request_budget?.toString() ?? "");
  const [gpuBudget, setGpuBudget] = useState(apiKey.gpu_seconds_budget?.toString() ?? "");
  const [maxConcurrency, setMaxConcurrency] = useState(apiKey.max_concurrency.toString());
  const [rateRequests, setRateRequests] = useState(apiKey.rate_limit_requests?.toString() ?? "");
  const [rateWindow, setRateWindow] = useState(apiKey.rate_window_seconds?.toString() ?? "");
  const [localError, setLocalError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);
    try {
      await onSave(policyPayload({ name, scopes, models, expiry, requestBudget, gpuBudget, maxConcurrency, rateRequests, rateWindow }));
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : "Key policy is invalid");
    }
  }

  return (
    <Modal description={`${apiKey.prefix} · ${apiKey.principal_id} · ${apiKey.tenant_id}`} onClose={onClose} title="Edit API-key policy">
      <form className="form-grid" onSubmit={(event) => void submit(event)}>
        <KeyPolicyFields {...{ name, setName, scopes, setScopes, models, setModels, expiry, setExpiry, requestBudget, setRequestBudget, gpuBudget, setGpuBudget, maxConcurrency, setMaxConcurrency, rateRequests, setRateRequests, rateWindow, setRateWindow }} />
        <FormError local={localError} remote={error} />
        <div className="modal-actions form-grid__wide"><button className="button" onClick={onClose} type="button">Cancel</button><button className="button button--primary" disabled={busy} type="submit">{busy ? "Saving…" : "Save policy"}</button></div>
      </form>
    </Modal>
  );
}

interface RotateKeyProps {
  apiKey: AdminApiKey;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (payload: AdminApiKeyRotateInput) => Promise<void>;
}

export function RotateKeyDialog({ apiKey, busy, error, onClose, onSave }: RotateKeyProps) {
  const [name, setName] = useState(`${apiKey.name ?? apiKey.prefix} rotated`);
  const [expiry, setExpiry] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);
    try {
      await onSave({ name: name.trim() || undefined, expires_at: optionalIso(expiry) });
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : "Rotation request is invalid");
    }
  }

  return (
    <Modal description="Rotation immediately retires the predecessor. Active operations are fenced, and the successor credential is displayed once." onClose={onClose} title="Rotate API key">
      <form className="form-grid" onSubmit={(event) => void submit(event)}>
        <label className="form-grid__wide">Successor name<input maxLength={120} onChange={(event) => setName(event.target.value)} value={name} /></label>
        <label className="form-grid__wide">Successor expiry<input onChange={(event) => setExpiry(event.target.value)} type="datetime-local" value={expiry} /></label>
        <FormError local={localError} remote={error} />
        <div className="modal-actions form-grid__wide"><button className="button" onClick={onClose} type="button">Cancel</button><button className="button button--primary" disabled={busy} type="submit">{busy ? "Rotating…" : "Rotate and reveal successor"}</button></div>
      </form>
    </Modal>
  );
}

interface RevokeKeyProps {
  apiKey: AdminApiKey;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

export function RevokeKeyDialog({ apiKey, busy, error, onClose, onConfirm }: RevokeKeyProps) {
  return (
    <Modal description={`${apiKey.name ?? apiKey.prefix} · ${apiKey.principal_id}`} onClose={onClose} title="Revoke API key">
      <div className="inline-notice inline-notice--warning"><strong>This cannot be undone.</strong> New inference and MCP requests using this key will be denied.</div>
      <FormError local={null} remote={error} />
      <div className="modal-actions"><button className="button" onClick={onClose} type="button">Cancel</button><button className="button button--danger" disabled={busy} onClick={() => void onConfirm()} type="button">{busy ? "Revoking…" : "Revoke key"}</button></div>
    </Modal>
  );
}
