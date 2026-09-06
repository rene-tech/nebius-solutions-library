import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { adminApi, AdminApiError } from "../../api/client";
import type {
  ScientificCapabilities,
  ScientificModelPolicy,
  ScientificModelPolicyList,
  ScientificModelPolicyUpdate,
  ScientificModelReadiness,
} from "../../api/scientificTypes";
import type { AdminEnvelope } from "../../api/types";
import { DataBoundary } from "../../components/DataBoundary";
import { formatTimestamp } from "../../lib/format";
import { ScientificStatusChip } from "./ScientificPresentation";

export const MAX_ACTIVE_RUNS = 64;

/** Why the policy controls are read-only, or null when the operator may change them. */
export function policyBlocker(capabilities: ScientificCapabilities | undefined, canOperate: boolean): string | null {
  if (!capabilities?.model_policy.available) {
    return capabilities?.model_policy.reason ?? "This build does not publish a scientific model policy command.";
  }
  if (!canOperate) return "Operator role required to change dispatch policy.";
  return null;
}

/** Validate the operator's draft before it becomes a durable request. */
export function draftUpdate(
  policy: ScientificModelPolicy,
  draft: { paused: boolean; maxActiveRuns: string; reason: string },
): ScientificModelPolicyUpdate {
  const trimmedCap = draft.maxActiveRuns.trim();
  let maxActiveRuns: number | null = null;
  if (trimmedCap) {
    const parsed = Number(trimmedCap);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > MAX_ACTIVE_RUNS) {
      throw new Error(`Max active runs must be a whole number between 1 and ${MAX_ACTIVE_RUNS}, or empty for no cap.`);
    }
    maxActiveRuns = parsed;
  }
  const reason = draft.reason.trim();
  if (reason.length > 300) throw new Error("The reason must be at most 300 characters.");
  return {
    expected_revision: policy.desired.revision,
    paused: draft.paused,
    max_active_runs: maxActiveRuns,
    reason: reason || null,
  };
}

function dispatchChipState(state: ScientificModelPolicy["effective"]["state"]) {
  return state === "open" ? "succeeded" : state === "paused" ? "blocked" : "pending";
}

interface PolicyRowProps {
  policy: ScientificModelPolicy;
  readiness: ScientificModelReadiness | undefined;
  blocker: string | null;
  onSave: (modelId: string, update: ScientificModelPolicyUpdate) => Promise<void>;
}

function PolicyRow({ policy, readiness, blocker, onSave }: PolicyRowProps) {
  const [editing, setEditing] = useState(false);
  const [paused, setPaused] = useState(policy.desired.paused);
  const [maxActiveRuns, setMaxActiveRuns] = useState(policy.desired.max_active_runs === null ? "" : String(policy.desired.max_active_runs));
  const [reason, setReason] = useState(policy.desired.reason ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [saved, setSaved] = useState(false);

  function beginEdit() {
    setPaused(policy.desired.paused);
    setMaxActiveRuns(policy.desired.max_active_runs === null ? "" : String(policy.desired.max_active_runs));
    setReason(policy.desired.reason ?? "");
    setError(null);
    setStale(false);
    setSaved(false);
    setEditing(true);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setStale(false);
    try {
      const update = draftUpdate(policy, { paused, maxActiveRuns, reason });
      await onSave(policy.model_id, update);
      setEditing(false);
      setSaved(true);
    } catch (caught) {
      if (caught instanceof AdminApiError && caught.status === 409) {
        setStale(true);
        setError(`${caught.message}${caught.requestId ? ` · request ${caught.requestId}` : ""}`);
      } else if (caught instanceof AdminApiError) {
        setError(`${caught.message}${caught.requestId ? ` · request ${caught.requestId}` : ""}`);
      } else if (caught instanceof Error) {
        setError(caught.message);
      } else {
        setError("The policy change failed.");
      }
    } finally {
      setBusy(false);
    }
  }

  const scope = policy.scope_tenant_id ? `tenant ${policy.scope_tenant_id}` : "all tenants";
  const desired = policy.desired;
  const desiredSummary = desired.revision === 0
    ? "No policy row · open by default"
    : `${desired.paused ? "Paused" : "Dispatching"} · ${desired.max_active_runs === null ? "no cap" : `cap ${desired.max_active_runs}`} · r${desired.revision}`;
  const rowLabel = readiness?.display_name ?? policy.model_id;
  return (
    <tr>
      <th scope="row">
        {rowLabel}
        <span className="secondary-line">{policy.model_id}{policy.catalog_known ? "" : " · not in the current catalog"}</span>
        {readiness ? <span className="secondary-line scientific-secondary">{readiness.execution_mode ?? "no published profile"}</span> : null}
      </th>
      <td>
        <ScientificStatusChip state={dispatchChipState(policy.effective.state)} label={policy.effective.state} reason={policy.effective.reason} />
        <span className="secondary-line scientific-secondary">{policy.effective.reason}</span>
        <span className="secondary-line">Effective cap {policy.effective.max_active_runs === null ? "none (Kueue quota only)" : policy.effective.max_active_runs}</span>
      </td>
      <td>
        <strong>{policy.counts.running}</strong> running · <strong>{policy.counts.queued}</strong> queued
        <span className="secondary-line">Counted for {scope}</span>
        {policy.scope_tenant_id ? <span className="secondary-line">All tenants: {policy.all_tenants_counts.running} running · {policy.all_tenants_counts.queued} queued</span> : null}
      </td>
      <td>
        {desiredSummary}
        {desired.reason ? <span className="secondary-line scientific-secondary">{desired.reason}</span> : null}
        {desired.updated_by ? <span className="secondary-line">{desired.updated_by} · {formatTimestamp(desired.updated_at)}</span> : null}
        {policy.inherited ? (
          <span className="secondary-line scientific-secondary">
            Inherits all-tenants r{policy.inherited.revision}: {policy.inherited.paused ? "paused" : "dispatching"}, {policy.inherited.max_active_runs === null ? "no cap" : `cap ${policy.inherited.max_active_runs}`}
          </span>
        ) : policy.scope_tenant_id ? <span className="secondary-line">No all-tenants policy row</span> : null}
      </td>
      <td>
        {saved ? <div className="inline-notice" role="status"><strong>Policy applied.</strong> The controller enforces it on its next poll.</div> : null}
        {blocker ? <p className="supporting-copy scientific-policy-blocker">{blocker}</p> : editing ? (
          <form aria-label={`Dispatch policy for ${rowLabel}`} className="scientific-policy-form" noValidate onSubmit={(event) => void submit(event)}>
            <label className="checkbox-field"><input checked={paused} disabled={busy} onChange={(event) => setPaused(event.target.checked)} type="checkbox" />Pause new dispatch</label>
            <label>Max active runs<input disabled={busy} inputMode="numeric" max={MAX_ACTIVE_RUNS} min={1} onChange={(event) => setMaxActiveRuns(event.target.value)} placeholder="No cap" step={1} type="number" value={maxActiveRuns} /></label>
            <label>Reason<input disabled={busy} maxLength={300} onChange={(event) => setReason(event.target.value)} placeholder="Optional operator note" value={reason} /></label>
            {error ? <div className="inline-notice inline-notice--error" role="alert"><strong>{stale ? "Policy changed elsewhere." : "Policy was not applied."}</strong> {error}{stale ? " Reload the list and re-apply your change." : ""}</div> : null}
            <div className="configuration-actions">
              <button className="button button--primary" disabled={busy} type="submit">{busy ? "Applying…" : "Apply policy"}</button>
              <button className="button" disabled={busy} onClick={() => setEditing(false)} type="button">Cancel</button>
              <span>Replaces revision {desired.revision} for {scope}.</span>
            </div>
          </form>
        ) : (
          <div className="configuration-actions">
            <button className="button" onClick={beginEdit} type="button">{desired.revision === 0 ? "Set policy" : "Edit policy"}</button>
          </div>
        )}
      </td>
    </tr>
  );
}

interface PanelProps {
  context: URLSearchParams;
  capabilities: ScientificCapabilities | undefined;
  capabilitiesPending: boolean;
  scopeTenantId: string | undefined;
  canOperate: boolean;
  readiness: ScientificModelReadiness[];
}

export function ScientificModelPolicyPanel({ context, capabilities, capabilitiesPending, scopeTenantId, canOperate, readiness }: PanelProps) {
  const queryClient = useQueryClient();
  const available = capabilities?.model_policy.available === true;
  const queryKey = ["admin-scientific-model-policies", context.toString(), scopeTenantId ?? "all-tenants"];
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => adminApi.scientificModelPolicies(context, scopeTenantId, signal),
    enabled: available,
  });
  const readinessById = new Map(readiness.map((item) => [item.model_id, item]));
  const blocker = policyBlocker(capabilities, canOperate);

  async function save(modelId: string, update: ScientificModelPolicyUpdate) {
    const refreshed = await adminApi.setScientificModelPolicy(modelId, update, context, scopeTenantId);
    queryClient.setQueryData<AdminEnvelope<ScientificModelPolicyList>>(queryKey, (current) => {
      if (!current) return current;
      return {
        ...current,
        data: {
          ...current.data,
          items: current.data.items.map((item) => (item.model_id === modelId ? refreshed.data : item)),
        },
      };
    });
    await queryClient.invalidateQueries({ queryKey: ["admin-scientific-runs"] });
  }

  const items = query.data?.data.items ?? [];
  return (
    <section className="section-stack" aria-labelledby="scientific-model-policy-title">
      <div className="section-heading">
        <div><span className="eyebrow">Model-level control</span><h2 id="scientific-model-policy-title">Scientific model dispatch policy</h2></div>
        <span className="section-heading__meta">Scope {scopeTenantId ? `tenant ${scopeTenantId}` : "all tenants"}</span>
      </div>
      {capabilitiesPending ? (
        <div className="state-panel state-panel--loading" role="status">Checking scientific model policy capability…</div>
      ) : available ? (
        <>
          <p className="supporting-copy scientific-policy-copy">
            Pausing stops the controller from dispatching new runs of a model; a cap limits how many dispatched runs may be active at once. Accepted work stays queued durably in Kueue priority order, running work drains, and results still publish. A policy never raises Kueue quota, node-pool bounds, per-run resources, or input limits, and there is no always-hot resident scientific runtime.
          </p>
          <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && items.length === 0} loadingLabel="Loading scientific model dispatch policy…" emptyLabel="No scientific models are known to this deployment.">
            {({ data }) => (
              <div className="table-frame">
                <table className="resource-table resource-table--scientific-policies">
                  <caption className="sr-only">Scientific model dispatch policy with effective state, live counts, desired settings, and controls</caption>
                  <thead><tr><th scope="col">Model</th><th scope="col">Effective dispatch</th><th scope="col">Runs now</th><th scope="col">Desired policy</th><th scope="col">Change</th></tr></thead>
                  <tbody>{data.items.map((policy) => (
                    <PolicyRow blocker={blocker} key={policy.model_id} onSave={save} policy={policy} readiness={readinessById.get(policy.model_id)} />
                  ))}</tbody>
                </table>
              </div>
            )}
          </DataBoundary>
        </>
      ) : (
        <div className="state-panel" role="status">
          <strong>Scientific model dispatch policy is not enabled</strong>
          <span>{capabilities?.model_policy.reason ?? "No durable scientific model policy repository is configured."}</span>
        </div>
      )}
    </section>
  );
}
