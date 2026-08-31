import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { adminApi, AdminApiError } from "../../api/client";
import type {
  ConfigurationDiff,
  ConfigurationPlan,
  ConfigurationValidation,
  ModelConfiguration,
  PlatformConfiguration,
  ReconciliationPhase,
  TerraformHandoff,
} from "../../api/configurationTypes";
import { useSession } from "../../auth/SessionContext";
import { DataBoundary } from "../../components/DataBoundary";
import { TerraformHandoffDialog } from "../../components/TerraformHandoffDialog";
import { rolePermits } from "../../lib/access";
import {
  handoffSafetyProblem,
  localConfigurationProblem,
  sameConfiguration,
  type EditableAutoscalingField,
} from "../../lib/configuration";
import { formatTimestamp } from "../../lib/format";

type HandoffSummary = Omit<TerraformHandoff, "tfvars_json" | "variables">;

interface PlanSummary {
  plan_id: string;
  state: ConfigurationPlan["state"];
  base_revision: number;
  base_etag: string;
  proposed_etag: string;
  validation: ConfigurationValidation;
  diff: ConfigurationDiff;
  artifacts: ConfigurationPlan["artifacts"];
  terraform: HandoffSummary;
  created_at: string;
  expires_at: string;
  created_by: string;
  purpose: "change" | "rollback";
  rollback_target: number | null;
}

const terminalPhases = new Set<ReconciliationPhase>(["succeeded", "failed", "rolled-back"]);

const fieldDetails: Record<EditableAutoscalingField, { label: string; min: number; max: number; suffix: string }> = {
  min_replicas: { label: "Minimum replicas", min: 0, max: 10_000, suffix: "Always-hot floor; zero permits cold capacity" },
  max_replicas: { label: "Maximum replicas", min: 0, max: 10_000, suffix: "Upper elastic replica bound" },
  target_queue_depth: { label: "Target queue depth", min: 1, max: 100_000, suffix: "KEDA queue target per replica" },
  polling_interval_seconds: { label: "Polling interval", min: 1, max: 60, suffix: "Seconds between KEDA polls" },
  cooldown_seconds: { label: "Cooldown", min: 5, max: 86_400, suffix: "Seconds before scale-down" },
};

function summarizePlan(plan: ConfigurationPlan, purpose: PlanSummary["purpose"], rollbackTarget: number | null): PlanSummary {
  const { tfvars_json: _tfvars, variables: _variables, ...terraform } = plan.terraform;
  return {
    plan_id: plan.plan_id,
    state: plan.state,
    base_revision: plan.base_revision,
    base_etag: plan.base_etag,
    proposed_etag: plan.proposed_etag,
    validation: plan.validation,
    diff: plan.diff,
    artifacts: plan.artifacts,
    terraform,
    created_at: plan.created_at,
    expires_at: plan.expires_at,
    created_by: plan.created_by,
    purpose,
    rollback_target: rollbackTarget,
  };
}

function shortHash(value: string | null): string {
  return value ? `${value.slice(0, 16)}…` : "Not published";
}

function displayValue(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "not present";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function ReadOnlyBadge() {
  return <span className="config-readonly-badge">Read-only · not applicable</span>;
}

function ConfigurationDiffPanel({ diff }: { diff: ConfigurationDiff }) {
  return (
    <section className="panel section-stack" aria-labelledby="configuration-diff-title">
      <header className="section-heading">
        <div><span className="eyebrow">Server-computed proposal</span><h2 id="configuration-diff-title">Configuration diff</h2></div>
        <span className="quiet-chip">{diff.changes.length} change{diff.changes.length === 1 ? "" : "s"}</span>
      </header>
      <p className="supporting-copy config-copy">{diff.terraform_change_count} Terraform-owned · {diff.runtime_change_count} runtime-owned. Current contract routes every implemented change through reviewed Terraform.</p>
      <div className="table-frame" role="region" aria-label="Configuration changes" tabIndex={0}>
        <table className="resource-table resource-table--configuration-diff">
          <caption className="sr-only">Server-computed configuration changes</caption>
          <thead><tr><th scope="col">Path</th><th scope="col">Owner</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>
          <tbody>
            {diff.changes.length === 0 ? <tr><td colSpan={4}>No changes.</td></tr> : diff.changes.map((change) => (
              <tr key={change.path}>
                <th scope="row"><code>{change.path}</code></th>
                <td><span className="mini-chip">{change.owner}</span></td>
                <td><code className="config-value">{displayValue(change.before)}</code></td>
                <td><code className="config-value">{displayValue(change.after)}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ValidationPanel({ validation, heading = "Validation" }: { validation: ConfigurationValidation; heading?: string }) {
  return (
    <section className="panel section-stack" aria-labelledby="configuration-validation-title">
      <header className="section-heading">
        <div><span className="eyebrow">Fail-closed contract checks</span><h2 id="configuration-validation-title">{heading}</h2></div>
        <span className={`capability-chip capability-chip--${validation.valid ? "healthy" : "unhealthy"}`}>{validation.valid ? "valid" : "rejected"}</span>
      </header>
      {validation.issues.length === 0 ? <div className="inline-notice">No validation issues were reported.</div> : (
        <ul className="configuration-issues">
          {validation.issues.map((issue, index) => (
            <li className={`configuration-issue configuration-issue--${issue.severity}`} key={`${issue.path}/${issue.code}/${index}`}>
              <strong>{issue.code}</strong><code>{issue.path}</code><span>{issue.message}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PoolCard({ id, pool }: { id: string; pool: PlatformConfiguration["pools"][string] }) {
  return (
    <article className="subpanel configuration-readonly-card">
      <header><div><span className="eyebrow">Accelerator pool</span><h3>{id}</h3></div><ReadOnlyBadge /></header>
      <dl className="definition-grid">
        <div><dt>Resource</dt><dd><code>{pool.resource_name}</code></dd></div>
        <div><dt>GPU class</dt><dd><code>{pool.accelerator_class}</code></dd></div>
        <div><dt>Capacity type</dt><dd>{pool.capacity_type}</dd></div>
        <div><dt>GPUs / node</dt><dd>{pool.accelerators_per_node}</dd></div>
        <div><dt>Node range</dt><dd>{pool.min_nodes}–{pool.max_nodes}</dd></div>
        <div><dt>Node selectors</dt><dd><code>{JSON.stringify(pool.node_selector)}</code></dd></div>
        <div><dt>Tolerations</dt><dd>{pool.tolerations.length ? <code>{JSON.stringify(pool.tolerations)}</code> : "None"}</dd></div>
      </dl>
    </article>
  );
}

interface ModelEditorProps {
  model: ModelConfiguration;
  disabled: boolean;
  onChange: (field: EditableAutoscalingField, value: number) => void;
}

function ModelEditor({ model, disabled, onChange }: ModelEditorProps) {
  return (
    <article className="panel section-stack configuration-model" aria-labelledby={`configuration-${model.model_id}`}>
      <header className="section-heading">
        <div><span className="eyebrow">Model desired state</span><h2 id={`configuration-${model.model_id}`}>{model.model_id}</h2></div>
        <span className="capability-chip capability-chip--healthy">Autoscaling editable</span>
      </header>
      <fieldset className="configuration-autoscaling" disabled={disabled}>
        <legend>Terraform-consumed autoscaling fields</legend>
        {Object.entries(fieldDetails).map(([field, detail]) => {
          const key = field as EditableAutoscalingField;
          const inputId = `${model.model_id}-${key}`;
          return (
            <div className="configuration-field" key={key}>
              <label htmlFor={inputId}>{detail.label}</label>
              <input
                aria-describedby={`${model.model_id}-${key}-help`}
                id={inputId}
                inputMode="numeric"
                max={detail.max}
                min={detail.min}
                onChange={(event) => {
                  const value = event.currentTarget.valueAsNumber;
                  if (Number.isFinite(value)) onChange(key, value);
                }}
                step={1}
                type="number"
                value={model.autoscaling[key]}
              />
              <small id={`${model.model_id}-${key}-help`}>{detail.suffix}</small>
            </div>
          );
        })}
      </fieldset>
      <details className="configuration-readonly">
        <summary>Review all read-only typed fields <ReadOnlyBadge /></summary>
        <p>These values are represented by the API for architecture review, but changing them has no proven consumer in the current Terraform root and is rejected before handoff.</p>
        <div className="split-grid">
          <section><h3>Placement and lifecycle</h3><dl className="definition-grid">
            <div><dt>Enabled</dt><dd>{model.enabled ? "Yes" : "No"}</dd></div>
            <div><dt>Pool IDs</dt><dd><code>{model.placement.pool_ids.join(", ")}</code></dd></div>
            <div><dt>Accelerators</dt><dd>{model.placement.accelerators}</dd></div>
            <div><dt>Topology</dt><dd>{model.placement.topology_policy}</dd></div>
            <div><dt>Queue</dt><dd><code>{model.queue.local_queue}</code></dd></div>
            <div><dt>Priority</dt><dd><code>{model.queue.priority_class}</code></dd></div>
            <div><dt>Max queue</dt><dd>{model.queue.max_queue_seconds}s</dd></div>
          </dl></section>
          <section><h3>Snapshot and MCP</h3><dl className="definition-grid">
            <div><dt>Snapshot strategy</dt><dd>{model.snapshot.strategy}</dd></div>
            <div><dt>Cache tier</dt><dd>{model.snapshot.cache_tier}</dd></div>
            <div><dt>Restore timeout</dt><dd>{model.snapshot.restore_timeout_seconds}s</dd></div>
            <div><dt>Parallelism</dt><dd>{model.snapshot.parallelism}</dd></div>
            <div><dt>Semantic check</dt><dd>{model.snapshot.require_semantic_check ? "Required" : "Not required"}</dd></div>
            <div><dt>MCP exposure</dt><dd>{model.mcp.exposed ? "Exposed" : "Not exposed"}</dd></div>
            <div><dt>MCP tool</dt><dd><code>{model.mcp.tool_name ?? "Not assigned"}</code></dd></div>
          </dl></section>
          <section><h3>Rate policy</h3><dl className="definition-grid">
            <div><dt>Requests / minute</dt><dd>{model.rate.requests_per_minute ?? "Unlimited"}</dd></div>
            <div><dt>Concurrent requests</dt><dd>{model.rate.concurrent_requests}</dd></div>
            <div><dt>GPU seconds / day</dt><dd>{model.rate.accelerator_seconds_per_day ?? "Unlimited"}</dd></div>
          </dl></section>
          <section><h3>Immutable artifact identity</h3><dl className="definition-grid">
            <div><dt>Image repository</dt><dd><code>{model.artifact.image_repository}</code></dd></div>
            <div><dt>Image digest</dt><dd><code title={model.artifact.image_digest}>{shortHash(model.artifact.image_digest)}</code></dd></div>
            <div><dt>Model revision</dt><dd><code>{model.artifact.model_revision}</code></dd></div>
            <div><dt>Artifact manifest</dt><dd><code title={model.artifact.artifact_manifest_sha256 ?? undefined}>{shortHash(model.artifact.artifact_manifest_sha256)}</code></dd></div>
            <div><dt>Acquisition contract</dt><dd><code title={model.artifact.acquisition_contract_sha256}>{shortHash(model.artifact.acquisition_contract_sha256)}</code></dd></div>
            <div><dt>Provenance</dt><dd><code title={model.artifact.provenance_sha256}>{shortHash(model.artifact.provenance_sha256)}</code></dd></div>
            <div><dt>Semantic contract</dt><dd><code title={model.artifact.semantic_health_contract_sha256}>{shortHash(model.artifact.semantic_health_contract_sha256)}</code></dd></div>
          </dl></section>
        </div>
      </details>
    </article>
  );
}

export function ConfigurationPage() {
  const queryClient = useQueryClient();
  const { session } = useSession();
  const [draft, setDraft] = useState<PlatformConfiguration | null>(null);
  const [diff, setDiff] = useState<ConfigurationDiff | null>(null);
  const [validation, setValidation] = useState<ConfigurationValidation | null>(null);
  const [plan, setPlan] = useState<PlanSummary | null>(null);
  const [handoff, setHandoff] = useState<TerraformHandoff | null>(null);
  const [statusId, setStatusId] = useState<string | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState(1);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const canPlan = rolePermits(session.principal.role, "operator");
  const canRollback = rolePermits(session.principal.role, "admin");

  const currentQuery = useQuery({
    queryKey: ["admin-configuration"],
    queryFn: ({ signal }) => adminApi.configuration(signal),
  });
  const current = currentQuery.data?.data;

  const statusQuery = useQuery({
    queryKey: ["admin-configuration-status", statusId],
    queryFn: ({ signal }) => adminApi.reconciliationStatus(statusId as string, signal),
    enabled: Boolean(statusId),
    refetchInterval: (query) => {
      const phase = query.state.data?.data.phase;
      return phase && !terminalPhases.has(phase) ? 2_500 : false;
    },
  });
  const reconciliation = statusQuery.data?.data;

  useEffect(() => {
    if (!current) return;
    setDraft(structuredClone(current.desired));
    setRollbackTarget(current.previous_revision ?? Math.max(1, current.revision - 1));
    setDiff(null);
    setValidation(null);
    setPlan(null);
    setHandoff(null);
    setActionError(null);
    setConflict(false);
    if (current.reconciliation_id) setStatusId(current.reconciliation_id);
  }, [current?.etag]);

  function clearPlannedState() {
    setDiff(null);
    setValidation(null);
    setPlan(null);
    setHandoff(null);
    setActionError(null);
    setConflict(false);
  }

  function updateAutoscaling(modelId: string, field: EditableAutoscalingField, value: number) {
    if (!draft) return;
    const next = structuredClone(draft);
    next.models[modelId].autoscaling[field] = value;
    setDraft(next);
    clearPlannedState();
  }

  function proposal() {
    if (!current || !draft) throw new Error("Current configuration is unavailable.");
    const localProblem = localConfigurationProblem(draft);
    if (localProblem) throw new Error(localProblem);
    return { base_etag: current.etag, desired: draft };
  }

  function handleFailure(caught: unknown) {
    if (caught instanceof AdminApiError && caught.status === 409) {
      setConflict(true);
      setActionError("Configuration changed on the server. Refresh the current revision before reviewing or planning again.");
      return;
    }
    setActionError(caught instanceof Error ? caught.message : "Configuration action failed.");
  }

  async function reviewDiff() {
    setBusy("diff"); setActionError(null);
    try {
      const response = await adminApi.configurationDiff(proposal());
      setDiff(response.data);
    } catch (caught) { handleFailure(caught); } finally { setBusy(null); }
  }

  async function validate() {
    setBusy("validate"); setActionError(null);
    try {
      const response = await adminApi.validateConfiguration(proposal());
      setValidation(response.data);
    } catch (caught) { handleFailure(caught); } finally { setBusy(null); }
  }

  function acceptPlan(value: ConfigurationPlan, purpose: PlanSummary["purpose"], target: number | null) {
    setDiff(value.diff);
    setValidation(value.validation);
    setPlan(null);
    setHandoff(null);
    if (value.state !== "valid") {
      setPlan(summarizePlan(value, purpose, target));
      return;
    }
    if (!value.validation.valid) {
      setActionError("The server marked an invalid configuration plan as valid. Terraform handoff tracking is blocked.");
      return;
    }
    if (!value.terraform?.required || value.terraform.state !== "review-required") {
      setActionError("The valid plan did not include the required reviewed Terraform handoff. Tracking is blocked.");
      return;
    }
    const safetyProblem = handoffSafetyProblem(value.terraform);
    if (safetyProblem) {
      setActionError(safetyProblem);
      return;
    }
    setPlan(summarizePlan(value, purpose, target));
    setHandoff(value.terraform);
  }

  async function createPlan() {
    setBusy("plan"); setActionError(null);
    try {
      const response = await adminApi.planConfiguration(proposal());
      acceptPlan(response.data, "change", null);
    } catch (caught) { handleFailure(caught); } finally { setBusy(null); }
  }

  async function startHandoffTracking() {
    if (
      !plan ||
      plan.state !== "valid" ||
      !plan.validation.valid ||
      !plan.terraform.required ||
      plan.terraform.state !== "review-required"
    ) return;
    setBusy("reconcile"); setActionError(null);
    try {
      const response = await adminApi.reconcileConfiguration({ plan_id: plan.plan_id, base_etag: plan.base_etag });
      queryClient.setQueryData(["admin-configuration-status", response.data.reconciliation_id], response);
      setStatusId(response.data.reconciliation_id);
    } catch (caught) { handleFailure(caught); } finally { setBusy(null); }
  }

  async function createRollbackPlan() {
    if (!current) return;
    setBusy("rollback"); setActionError(null);
    try {
      const response = await adminApi.rollbackConfiguration({ target_revision: rollbackTarget, base_etag: current.etag });
      acceptPlan(response.data.plan, "rollback", response.data.target_revision);
    } catch (caught) { handleFailure(caught); } finally { setBusy(null); }
  }

  async function refreshCurrent() {
    setBusy("refresh");
    const response = await currentQuery.refetch();
    if (response.data) {
      setDraft(structuredClone(response.data.data.desired));
      clearPlannedState();
    }
    setBusy(null);
  }

  return (
    <DataBoundary data={currentQuery.data} error={currentQuery.error} pending={currentQuery.isPending}>
      {({ data: revision }) => {
        const desired = draft ?? revision.desired;
        const dirty = !sameConfiguration(desired, revision.desired);
        const effectiveAligned = sameConfiguration(revision.desired, revision.effective);
        const trackingLocked = reconciliation != null && !terminalPhases.has(reconciliation.phase);
        const editorDisabled = busy !== null || conflict || trackingLocked;
        return (
          <div className="page-stack configuration-page">
            <section className="panel configuration-intro">
              <div><span className="eyebrow">Declarative desired state</span><h2>Configuration planning</h2><p>Read, diff and validate as a viewer. Operators create a deterministic Terraform handoff; administrators can plan rollbacks. The browser never applies infrastructure.</p></div>
              <dl>
                <div><dt>Revision</dt><dd>{revision.revision}</dd></div>
                <div><dt>ETag</dt><dd><code title={revision.etag}>{shortHash(revision.etag)}</code></dd></div>
                <div><dt>Desired / effective</dt><dd><span className={`capability-chip capability-chip--${effectiveAligned ? "healthy" : "degraded"}`}>{effectiveAligned ? "aligned" : "diverged"}</span></dd></div>
                <div><dt>Created</dt><dd>{formatTimestamp(revision.created_at)}</dd></div>
                <div><dt>Actor</dt><dd>{revision.created_by}</dd></div>
              </dl>
            </section>

            <div className="inline-notice" role="status">
              <strong>Implemented edit boundary:</strong> only minimum/maximum replicas, target queue depth, polling interval and cooldown are editable. Pool, placement, queue, snapshot, MCP, rate, artifact and model membership fields are read-only and rejected if changed through another client.
            </div>
            {session.principal.role === "viewer" ? <div className="inline-notice inline-notice--warning">Viewer access can inspect, edit a local draft, diff and validate. An operator or administrator must create and track a Terraform handoff.</div> : null}
            {conflict ? (
              <div className="inline-notice inline-notice--error" role="alert">
                <strong>Concurrent revision conflict.</strong> {actionError}
                <button className="button" disabled={busy !== null} onClick={() => void refreshCurrent()} type="button">Refresh current revision</button>
              </div>
            ) : actionError ? <div className="inline-notice inline-notice--error" role="alert">{actionError}</div> : null}

            <section className="panel section-stack" aria-labelledby="capacity-contract-title">
              <header className="section-heading"><div><span className="eyebrow">Heterogeneous capacity contract</span><h2 id="capacity-contract-title">Accelerator pools</h2></div><ReadOnlyBadge /></header>
              <p className="config-copy">GPU classes, capacity types and resource names are dynamic and provider-neutral. They are reviewable here but currently changed only in version-controlled Terraform.</p>
              <div className="configuration-pool-grid">{Object.entries(desired.pools).map(([id, pool]) => <PoolCard id={id} key={id} pool={pool} />)}</div>
            </section>

            {Object.values(desired.models).map((model) => (
              <ModelEditor disabled={editorDisabled} key={model.model_id} model={model} onChange={(field, value) => updateAutoscaling(model.model_id, field, value)} />
            ))}

            <section className="panel configuration-deferred" aria-labelledby="add-model-title">
              <div><span className="eyebrow">Catalog extension</span><h2 id="add-model-title">Add or remove a model</h2><p>Deferred / not applicable in this UI. Model membership requires catalog identity, acquisition, provenance and semantic-health contracts before a Terraform consumer can be proven.</p></div>
              <ReadOnlyBadge />
            </section>

            <section className="panel section-stack" aria-labelledby="review-actions-title">
              <header className="section-heading"><div><span className="eyebrow">No direct mutation</span><h2 id="review-actions-title">Review and handoff</h2></div><span className={`quiet-chip ${dirty ? "quiet-chip--warning" : "quiet-chip--healthy"}`}>{dirty ? "Local draft changed" : "No local changes"}</span></header>
              <div className="configuration-actions">
                <button className="button" disabled={!dirty || editorDisabled} onClick={() => void reviewDiff()} type="button">{busy === "diff" ? "Reviewing…" : "Review diff"}</button>
                <button className="button" disabled={!dirty || editorDisabled} onClick={() => void validate()} type="button">{busy === "validate" ? "Validating…" : "Validate proposal"}</button>
                {canPlan ? <button className="button button--primary" disabled={!dirty || editorDisabled} onClick={() => void createPlan()} type="button">{busy === "plan" ? "Planning…" : "Create Terraform handoff"}</button> : null}
                <button className="text-button" disabled={busy !== null || !dirty} onClick={() => { setDraft(structuredClone(revision.desired)); clearPlannedState(); }} type="button">Discard local draft</button>
              </div>
            </section>

            {diff ? <ConfigurationDiffPanel diff={diff} /> : null}
            {validation ? <ValidationPanel heading={plan ? "Plan validation" : "Validation"} validation={validation} /> : null}

            {plan ? (
              <section className="panel section-stack" aria-labelledby="configuration-plan-title">
                <header className="section-heading">
                  <div><span className="eyebrow">Immutable reviewed artifact</span><h2 id="configuration-plan-title">{plan.purpose === "rollback" ? `Rollback plan to revision ${plan.rollback_target}` : "Terraform plan handoff"}</h2></div>
                  <span className={`capability-chip capability-chip--${plan.state === "valid" ? "healthy" : "unhealthy"}`}>{plan.state}</span>
                </header>
                <dl className="definition-grid">
                  <div><dt>Plan ID</dt><dd><code>{plan.plan_id}</code></dd></div>
                  <div><dt>Base revision</dt><dd>{plan.base_revision}</dd></div>
                  <div><dt>Proposed ETag</dt><dd><code title={plan.proposed_etag}>{shortHash(plan.proposed_etag)}</code></dd></div>
                  <div><dt>Expires</dt><dd>{formatTimestamp(plan.expires_at)}</dd></div>
                  <div><dt>Rendered artifacts</dt><dd>{plan.artifacts.length}</dd></div>
                  <div><dt>Handoff digest</dt><dd><code title={plan.terraform.tfvars_sha256}>{shortHash(plan.terraform.tfvars_sha256)}</code></dd></div>
                </dl>
                {plan.state === "valid" ? (
                  <div className="configuration-actions">
                    <button className="button button--primary" disabled={busy !== null || trackingLocked} onClick={() => void startHandoffTracking()} type="button">{busy === "reconcile" ? "Recording…" : "Start Terraform handoff tracking"}</button>
                    <span>The tfvars document is disclosed once. This action records <code>AWAITING_TERRAFORM</code>; it does not execute Terraform.</span>
                  </div>
                ) : <div className="inline-notice inline-notice--error">Rejected plans cannot enter reconciliation or produce tfvars.</div>}
              </section>
            ) : null}

            {statusId ? (
              <section className="panel reconciliation-panel" aria-labelledby="reconciliation-title">
                <div>
                  <span className="eyebrow">Durable reconciliation status</span>
                  <h2 id="reconciliation-title">{reconciliation?.phase === "awaiting-terraform-plan-apply" ? "AWAITING_TERRAFORM" : reconciliation?.phase === "succeeded" ? "RECEIPT COMPLETE" : reconciliation?.phase ?? "Loading status…"}</h2>
                  <p>{reconciliation?.phase === "awaiting-terraform-plan-apply" ? "The reviewed handoff is waiting for an independently executed Terraform plan/apply and its correlated receipt." : reconciliation?.phase === "succeeded" ? `Terraform receipt accepted atomically${reconciliation.applied_revision ? ` as revision ${reconciliation.applied_revision}` : ""}.` : "The console is reading the durable status; no browser mutation is running."}</p>
                  {reconciliation ? <code>{reconciliation.phase}</code> : null}
                </div>
                <dl>
                  <div><dt>Reconciliation ID</dt><dd><code>{statusId}</code></dd></div>
                  <div><dt>Started</dt><dd>{formatTimestamp(reconciliation?.started_at ?? null)}</dd></div>
                  <div><dt>Completed</dt><dd>{formatTimestamp(reconciliation?.completed_at ?? null)}</dd></div>
                  <div><dt>Applied revision</dt><dd>{reconciliation?.applied_revision ?? "Awaiting receipt"}</dd></div>
                </dl>
                <div className="configuration-actions">
                  <button className="button" disabled={statusQuery.isFetching} onClick={() => void statusQuery.refetch()} type="button">Refresh status</button>
                  {reconciliation?.phase === "succeeded" ? <button className="button" onClick={() => void refreshCurrent()} type="button">Load accepted revision</button> : null}
                </div>
                {statusQuery.error ? <div className="inline-notice inline-notice--error" role="alert">Status unavailable: {statusQuery.error.message}</div> : null}
              </section>
            ) : null}

            <section className="panel rollback-panel" aria-labelledby="rollback-title">
              <div><span className="eyebrow">Terraform-owned recovery</span><h2 id="rollback-title">Rollback</h2><p>Rollback creates another reviewed Terraform plan. It never changes effective state from the browser.</p></div>
              {canRollback ? (
                <div className="rollback-control">
                  <label>Target revision<input min={1} onChange={(event) => setRollbackTarget(event.currentTarget.valueAsNumber || 1)} type="number" value={rollbackTarget} /></label>
                  <button className="button" disabled={busy !== null || conflict || revision.revision <= 1} onClick={() => void createRollbackPlan()} type="button">{busy === "rollback" ? "Planning rollback…" : "Create rollback handoff"}</button>
                </div>
              ) : <div className="inline-notice inline-notice--warning">Administrator role required to create a rollback plan.</div>}
            </section>

            {handoff ? <TerraformHandoffDialog handoff={handoff} onDismiss={() => setHandoff(null)} /> : null}
          </div>
        );
      }}
    </DataBoundary>
  );
}
