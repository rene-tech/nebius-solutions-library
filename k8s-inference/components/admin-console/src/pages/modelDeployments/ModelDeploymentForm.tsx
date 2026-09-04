import type { ReactNode } from "react";
import type {
  ModelDeploymentAdoptionMode,
  ModelDeploymentCacheTier,
  ModelDeploymentFastStartMechanism,
  ModelDeploymentConfigurationOption,
  ModelDeploymentDesiredState,
  ModelDeploymentFastStartFallbackPolicy,
  ModelDeploymentFastStartLevel,
  ModelDeploymentFastStartMode,
  ModelDeploymentRolloutStrategy,
  ModelDeploymentSnapshotPreference,
  ModelDeploymentSnapshotStrategy,
  ModelDeploymentSpec,
  ModelDeploymentTopologyPolicy,
  ModelDeploymentVisibility,
} from "../../api/modelDeploymentTypes";
import {
  fastStartLevelLabel,
  fastStartTarget,
  modelDeploymentFastStartLevels,
  normalizeFastStartPolicy,
  uniqueCsv,
} from "../../lib/modelDeployment";

interface TextFieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  placeholder?: string;
  hint?: string;
  type?: "text" | "number";
  min?: number;
  max?: number;
  disabled?: boolean;
}

function TextField({ label, value, onChange, required, placeholder, hint, type = "text", min, max, disabled }: TextFieldProps) {
  return (
    <label>{label}
      <input aria-label={label} disabled={disabled} max={max} min={min} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} required={required} spellCheck={false} type={type} value={value} />
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

function NumberField({ label, value, onChange, min = 0, max, hint, disabled }: Omit<TextFieldProps, "value" | "onChange" | "type"> & { value: number; onChange: (value: number) => void }) {
  return <TextField disabled={disabled} hint={hint} label={label} max={max} min={min} onChange={(value) => onChange(Number(value))} required type="number" value={String(value)} />;
}

function SelectField<T extends string>({ label, value, values, onChange, hint, disabled, formatOption = (item) => item }: { label: string; value: T; values: readonly T[]; onChange: (value: T) => void; hint?: string; disabled?: boolean; formatOption?: (value: T) => string }) {
  return (
    <label>{label}
      <select aria-label={label} disabled={disabled} onChange={(event) => onChange(event.target.value as T)} value={value}>{values.map((item) => <option key={item} value={item}>{formatOption(item)}</option>)}</select>
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

function FormSection({ title, detail, children, disabled }: { title: string; detail: string; children: ReactNode; disabled: boolean }) {
  return (
    <fieldset className="model-deployment-form-section" disabled={disabled}>
      <legend>{title}</legend>
      <p>{detail}</p>
      <div className="model-deployment-form-grid">{children}</div>
    </fieldset>
  );
}

interface Props {
  name: string;
  namespace: string;
  spec: ModelDeploymentSpec;
  identityLocked: boolean;
  disabled: boolean;
  configurationOption?: ModelDeploymentConfigurationOption | null;
  onNameChange: (value: string) => void;
  onNamespaceChange: (value: string) => void;
  onChange: (value: ModelDeploymentSpec) => void;
}

export function ModelDeploymentForm({ name, namespace, spec, identityLocked, disabled, configurationOption, onNameChange, onNamespaceChange, onChange }: Props) {
  const presetLocked = Boolean(configurationOption);
  const fastStart = normalizeFastStartPolicy(spec.fastStart);
  const selectableMechanisms: Array<ModelDeploymentFastStartMechanism | ""> = [
    "",
    ...(configurationOption?.fast_start_mechanism_choices.map((choice) => choice.mechanism)
      ?? ["conventional", "regional-cache", "host-memory-residency"]),
  ];
  if (spec.cache.mechanism && !selectableMechanisms.includes(spec.cache.mechanism)) {
    selectableMechanisms.push(spec.cache.mechanism);
  }
  function update(change: (next: ModelDeploymentSpec) => void) {
    const next = structuredClone(spec);
    change(next);
    onChange(next);
  }

  function updateWarmWindow(index: number, patch: Partial<ModelDeploymentSpec["availability"]["warmWindows"][number]>) {
    update((next) => Object.assign(next.availability.warmWindows[index], patch));
  }

  function fastStartIndex(level: ModelDeploymentFastStartLevel) {
    return modelDeploymentFastStartLevels.indexOf(level);
  }

  return (
    <form className="model-deployment-form" onSubmit={(event) => event.preventDefault()}>
      <FormSection disabled={disabled} title="Identity and lifecycle" detail="Identity and runtime profile become immutable after creation. Lifecycle changes are planned against the current ETag.">
        <TextField disabled={identityLocked} label="Deployment name" onChange={onNameChange} required value={name} />
        <TextField disabled={identityLocked || presetLocked} label="Namespace" onChange={onNamespaceChange} required value={namespace} />
        <TextField disabled={identityLocked || presetLocked} label="Model reference" onChange={(value) => update((next) => { next.modelRef = value; })} required value={spec.modelRef} />
        {configurationOption ? (
          <SelectField label="Tenant ID" onChange={(value) => update((next) => { next.tenantId = value; })} value={spec.tenantId} values={configurationOption.tenant_choices} />
        ) : (
          <TextField disabled={identityLocked} label="Tenant ID" onChange={(value) => update((next) => { next.tenantId = value; })} required value={spec.tenantId} />
        )}
        <SelectField<ModelDeploymentDesiredState> label="Desired state" onChange={(value) => update((next) => { next.lifecycle.desiredState = value; if (value !== "Enabled") next.availability.minReplicas = 0; else if (configurationOption?.scale_to_zero_qualified === false) next.availability.minReplicas = Math.max(1, next.availability.minReplicas); })} value={spec.lifecycle.desiredState} values={["Enabled", "Draining", "Disabled"]} />
        {identityLocked ? <div className="inline-notice form-grid__wide" role="status">Deployment name, namespace, model reference, tenant and runtime profile are immutable for an existing deployment.</div> : null}
      </FormSection>

      <FormSection disabled={disabled} title="Artifact and runtime" detail="Every executable and template identity is digest-pinned. Credentials remain in Kubernetes references and are never entered here.">
        <TextField disabled={presetLocked} label="Artifact revision" onChange={(value) => update((next) => { next.artifact.revision = value; })} required value={spec.artifact.revision} />
        <TextField disabled={presetLocked} label="Artifact manifest digest" onChange={(value) => update((next) => { next.artifact.manifestDigest = value; })} placeholder="sha256:…" required value={spec.artifact.manifestDigest} />
        <SelectField disabled={presetLocked} label="Artifact storage" onChange={(value) => update((next) => { next.artifact.storageRef = value === "None" ? null : { kind: value, name: next.artifact.storageRef?.name ?? "" }; })} value={spec.artifact.storageRef?.kind ?? "None"} values={["None", "ObjectStore", "PersistentVolumeClaim", "LocalModelCache"] as const} />
        <TextField disabled={presetLocked || !spec.artifact.storageRef} label="Storage reference name" onChange={(value) => update((next) => { if (next.artifact.storageRef) next.artifact.storageRef.name = value; })} placeholder={spec.artifact.storageRef ? "Kubernetes reference" : "Not used"} value={spec.artifact.storageRef?.name ?? ""} />
        <TextField disabled={identityLocked || presetLocked} label="Runtime profile" onChange={(value) => update((next) => { next.runtime.profile = value; })} required value={spec.runtime.profile} />
        <TextField disabled={presetLocked} label="Runtime image digest" onChange={(value) => update((next) => { next.runtime.image = value; })} placeholder="registry/repository@sha256:…" required value={spec.runtime.image} />
        <TextField disabled={presetLocked} label="Runtime template name" onChange={(value) => update((next) => { next.runtime.templateRef.name = value; })} required value={spec.runtime.templateRef.name} />
        <TextField disabled={presetLocked} label="Runtime template digest" onChange={(value) => update((next) => { next.runtime.templateRef.digest = value; })} placeholder="sha256:…" required value={spec.runtime.templateRef.digest} />
      </FormSection>

      <FormSection disabled={disabled} title="Placement and elasticity" detail="Pool references remain accelerator-neutral. Select reserved pools for the hot floor and compatible preemptible pools for elastic burst; the server bounds their combined ceiling.">
        {configurationOption ? (
          <fieldset className="accelerator-pool-fieldset form-grid__wide">
            <legend>Accelerator pools</legend>
            <div className="checkbox-stack">
              {configurationOption.pool_choices.map((choice) => (
                <label className="checkbox-field" key={choice.pool_ref}>
                  <input
                    aria-label={`Use ${choice.pool_ref}`}
                    checked={spec.placement.poolRefs.includes(choice.pool_ref)}
                    onChange={(event) => update((next) => {
                      const selected = new Set(next.placement.poolRefs);
                      if (event.target.checked) selected.add(choice.pool_ref);
                      else selected.delete(choice.pool_ref);
                      next.placement.poolRefs = configurationOption.pool_choices
                        .map((candidate) => candidate.pool_ref)
                        .filter((poolRef) => selected.has(poolRef));
                    })}
                    type="checkbox"
                  />
                  <span><code>{choice.pool_ref}</code> · {choice.accelerator_class} · {choice.capacity_type} · {choice.accelerators_per_node} GPUs/node · up to {choice.maximum_replicas} replicas</span>
                </label>
              ))}
            </div>
            <small>Only pools qualified for this exact model/runtime tuple are offered.</small>
          </fieldset>
        ) : (
          <TextField hint="Comma-separated Terraform pool IDs." label="Accelerator pools" onChange={(value) => update((next) => { next.placement.poolRefs = uniqueCsv(value); })} required value={spec.placement.poolRefs.join(", ")} />
        )}
        <NumberField disabled={presetLocked} label="Accelerators per replica" max={64} min={1} onChange={(value) => update((next) => { next.placement.acceleratorsPerReplica = value; })} value={spec.placement.acceleratorsPerReplica} />
        <SelectField<ModelDeploymentTopologyPolicy> disabled={presetLocked} label="Topology policy" onChange={(value) => update((next) => { next.placement.topologyPolicy = value; })} value={spec.placement.topologyPolicy} values={["Any", "SingleNode", "HighBandwidthDomain"]} />
        <NumberField hint={configurationOption ? configurationOption.scale_to_zero_qualified ? "This exact tuple is qualified for a zero hot floor." : "This tuple is not qualified for scale-to-zero while enabled; draining and disabled states use zero." : "Minimum replicas kept hot; zero enables cold-only operation."} label="Hot floor" max={10000} min={spec.lifecycle.desiredState === "Enabled" && configurationOption?.scale_to_zero_qualified === false ? 1 : 0} onChange={(value) => update((next) => { next.availability.minReplicas = value; })} value={spec.availability.minReplicas} />
        <NumberField label="Replica ceiling" max={10000} onChange={(value) => update((next) => { next.availability.maxReplicas = value; })} value={spec.availability.maxReplicas} />
        <NumberField label="Idle before scale-to-zero (seconds)" max={604800} onChange={(value) => update((next) => { next.availability.idleSeconds = value; })} value={spec.availability.idleSeconds} />
        <NumberField label="Target queue depth" max={100000} min={1} onChange={(value) => update((next) => { next.availability.targetQueueDepth = value; })} value={spec.availability.targetQueueDepth} />
        <NumberField label="Polling interval (seconds)" max={60} min={1} onChange={(value) => update((next) => { next.availability.pollingIntervalSeconds = value; })} value={spec.availability.pollingIntervalSeconds} />
        <NumberField label="Cooldown (seconds)" max={86400} min={5} onChange={(value) => update((next) => { next.availability.cooldownSeconds = value; })} value={spec.availability.cooldownSeconds} />
      </FormSection>

      <fieldset className="model-deployment-form-section" disabled={disabled}>
        <legend>Warm windows</legend>
        <p>Optional recurring windows temporarily raise the hot floor. Schedules and time zones are retained as explicit policy.</p>
        <div className="warm-window-list">
          {spec.availability.warmWindows.map((window, index) => (
            <div className="model-deployment-form-grid warm-window" key={`${window.name}-${index}`}>
              <TextField label="Window name" onChange={(value) => updateWarmWindow(index, { name: value })} required value={window.name} />
              <TextField hint="Cron expression" label="Schedule" onChange={(value) => updateWarmWindow(index, { schedule: value })} required value={window.schedule} />
              <TextField label="Time zone" onChange={(value) => updateWarmWindow(index, { timeZone: value })} required value={window.timeZone} />
              <NumberField label="Duration (seconds)" max={604800} min={60} onChange={(value) => updateWarmWindow(index, { durationSeconds: value })} value={window.durationSeconds} />
              <NumberField label="Window hot floor" max={10000} min={1} onChange={(value) => updateWarmWindow(index, { minReplicas: value })} value={window.minReplicas} />
              <button className="button" onClick={() => update((next) => { next.availability.warmWindows.splice(index, 1); })} type="button">Remove window</button>
            </div>
          ))}
          {spec.availability.warmWindows.length === 0 ? <p className="empty-copy">No warm windows configured; the ordinary hot floor applies at all times.</p> : null}
        </div>
        <button className="button" onClick={() => update((next) => { next.availability.warmWindows.push({ name: `window-${next.availability.warmWindows.length + 1}`, schedule: "0 8 * * 1-5", timeZone: "UTC", durationSeconds: 3600, minReplicas: Math.max(1, next.availability.minReplicas) }); })} type="button">Add warm window</button>
      </fieldset>

      <FormSection disabled={disabled} title="Fast start" detail="Choose the model-ready target customers can understand. Hot is derived from a currently serving replica and is not a selectable cache level.">
        {!fastStart.configured ? <div className="inline-notice inline-notice--warning form-grid__wide" role="status"><strong>Legacy policy.</strong> This revision has no explicit fast-start class. Select a mode or level to migrate it; no qualification is inferred from its cache fields.</div> : null}
        <SelectField<ModelDeploymentFastStartMode>
          label="Fast-start mode"
          onChange={(value) => update((next) => {
            next.fastStart = value === "Fixed"
              ? { mode: value, level: fastStart.mode === "Fixed" ? fastStart.level : fastStart.maximumLevel, fallbackPolicy: fastStart.fallbackPolicy }
              : { mode: value, minimumLevel: fastStart.mode === "Automatic" ? fastStart.minimumLevel : "Off", maximumLevel: fastStart.mode === "Automatic" ? fastStart.maximumLevel : fastStart.level === "Off" ? "L4" : fastStart.level, fallbackPolicy: fastStart.fallbackPolicy };
          })}
          value={fastStart.mode}
          values={["Fixed", "Automatic"]}
          formatOption={(value) => value === "Fixed" ? "Fixed target" : "Automatic from qualified paths"}
        />
        {fastStart.mode === "Fixed" ? (
          <SelectField<ModelDeploymentFastStartLevel>
            hint="Hot appears automatically while a replica is ready."
            label="Fast-start level"
            onChange={(value) => update((next) => { next.fastStart = { mode: "Fixed", level: value, fallbackPolicy: fastStart.fallbackPolicy }; })}
            value={fastStart.level}
            values={modelDeploymentFastStartLevels}
            formatOption={fastStartLevelLabel}
          />
        ) : (
          <>
            <SelectField<ModelDeploymentFastStartLevel>
              label="Minimum fast-start level"
              onChange={(value) => update((next) => {
                const maximumLevel = fastStartIndex(value) > fastStartIndex(fastStart.maximumLevel) ? value : fastStart.maximumLevel;
                next.fastStart = { mode: "Automatic", minimumLevel: value, maximumLevel, fallbackPolicy: fastStart.fallbackPolicy };
              })}
              value={fastStart.minimumLevel}
              values={modelDeploymentFastStartLevels}
              formatOption={fastStartLevelLabel}
            />
            <SelectField<ModelDeploymentFastStartLevel>
              label="Maximum fast-start level"
              onChange={(value) => update((next) => {
                const minimumLevel = fastStartIndex(value) < fastStartIndex(fastStart.minimumLevel) ? value : fastStart.minimumLevel;
                next.fastStart = { mode: "Automatic", minimumLevel, maximumLevel: value, fallbackPolicy: fastStart.fallbackPolicy };
              })}
              value={fastStart.maximumLevel}
              values={modelDeploymentFastStartLevels}
              formatOption={fastStartLevelLabel}
            />
          </>
        )}
        <SelectField<ModelDeploymentFastStartFallbackPolicy>
          hint="Require target blocks an unqualified slower path."
          label="When the target is unavailable"
          onChange={(value) => update((next) => {
            next.fastStart = fastStart.mode === "Fixed"
              ? { mode: "Fixed", level: fastStart.level, fallbackPolicy: value }
              : { mode: "Automatic", minimumLevel: fastStart.minimumLevel, maximumLevel: fastStart.maximumLevel, fallbackPolicy: value };
          })}
          value={fastStart.fallbackPolicy}
          values={["AllowLowerLevel", "RequireTarget"]}
          formatOption={(value) => value === "AllowLowerLevel" ? "Allow a slower qualified level" : "Require the selected target"}
        />
        <div className="fast-start-policy-summary form-grid__wide" role="status">
          <strong>{fastStart.mode === "Fixed" ? fastStartLevelLabel(fastStart.level) : `Automatic ${fastStart.minimumLevel}–${fastStart.maximumLevel}`}</strong>
          <span>{fastStart.mode === "Fixed" ? fastStartTarget(fastStart.level) : `Best target ${fastStartTarget(fastStart.maximumLevel)}; never below ${fastStart.minimumLevel}.`}</span>
          <small>The target clock starts when compatible accelerator capacity is available. Capacity wait and total request-to-ready time are reported separately.</small>
        </div>
        <details className="fast-start-mechanisms form-grid__wide">
          <summary>Operator mechanism details</summary>
          <p>These implementation controls remain visible for diagnosis and backwards compatibility. A cache or snapshot setting alone does not prove a fast-start level.</p>
          <div className="model-deployment-form-grid">
            <SelectField<ModelDeploymentFastStartMechanism | "">
              hint={configurationOption ? "Only mechanisms declared for this installed model tuple are offered. Selecting one applies its pool, cache-tier and hot-capacity requirements." : undefined}
              label="Cold-start mechanism"
              onChange={(value) => update((next) => {
                next.cache.mechanism = value === "" ? null : value;
                const choice = configurationOption?.fast_start_mechanism_choices.find((candidate) => candidate.mechanism === value);
                if (!choice) return;
                const compatiblePools = next.placement.poolRefs.filter((poolRef) => choice.pool_refs.includes(poolRef));
                next.placement.poolRefs = compatiblePools.length > 0 ? compatiblePools : [choice.pool_refs[0]!];
                if (choice.required_cache_tier) next.cache.tier = choice.required_cache_tier;
                next.availability.minReplicas = Math.max(next.availability.minReplicas, choice.minimum_hot_replicas);
                next.availability.maxReplicas = Math.max(next.availability.maxReplicas, choice.minimum_max_replicas);
              })}
              value={spec.cache.mechanism ?? ""}
              values={selectableMechanisms}
              formatOption={(value) => value || "Automatic from qualified evidence"}
            />
            <SelectField<ModelDeploymentCacheTier> disabled={presetLocked} label="Cache tier" onChange={(value) => update((next) => { next.cache.tier = value; })} value={spec.cache.tier} values={["Disabled", "ObjectStore", "SharedFilesystem", "NodeLocal"]} />
            <SelectField<ModelDeploymentSnapshotPreference> disabled={presetLocked} label="Snapshot preference" onChange={(value) => update((next) => { next.cache.snapshotPreference = value; next.cache.snapshotRef = value === "Never" ? null : next.cache.snapshotRef ?? { name: "", digest: "", strategy: "Weights" }; })} value={spec.cache.snapshotPreference} values={["Never", "Prefer", "Require"]} />
            <TextField disabled={presetLocked || !spec.cache.snapshotRef} label="Snapshot name" onChange={(value) => update((next) => { if (next.cache.snapshotRef) next.cache.snapshotRef.name = value; })} placeholder={spec.cache.snapshotRef ? "Qualified snapshot" : "Not used"} value={spec.cache.snapshotRef?.name ?? ""} />
            <TextField disabled={presetLocked || !spec.cache.snapshotRef} label="Snapshot digest" onChange={(value) => update((next) => { if (next.cache.snapshotRef) next.cache.snapshotRef.digest = value; })} placeholder={spec.cache.snapshotRef ? "sha256:…" : "Not used"} value={spec.cache.snapshotRef?.digest ?? ""} />
            <SelectField<ModelDeploymentSnapshotStrategy> disabled={presetLocked || !spec.cache.snapshotRef} label="Snapshot strategy" onChange={(value) => update((next) => { if (next.cache.snapshotRef) next.cache.snapshotRef.strategy = value; })} value={spec.cache.snapshotRef?.strategy ?? "Weights"} values={["Weights", "RuntimeNative", "CudaCheckpoint"]} />
          </div>
        </details>
      </FormSection>

      <FormSection disabled={disabled} title="Queue and rollout" detail="Queue admission remains separate from replica autoscaling; rollout bounds protect serving availability.">
        {configurationOption ? <SelectField label="Local queue" onChange={(value) => update((next) => { next.queue.localQueue = value; })} value={spec.queue.localQueue} values={configurationOption.local_queue_choices} /> : <TextField label="Local queue" onChange={(value) => update((next) => { next.queue.localQueue = value; })} required value={spec.queue.localQueue} />}
        {configurationOption ? <SelectField label="Priority class" onChange={(value) => update((next) => { next.queue.priorityClass = value; })} value={spec.queue.priorityClass} values={configurationOption.priority_class_choices} /> : <TextField label="Priority class" onChange={(value) => update((next) => { next.queue.priorityClass = value; })} required value={spec.queue.priorityClass} />}
        <NumberField label="Maximum queue time (seconds)" max={604800} min={1} onChange={(value) => update((next) => { next.queue.maxQueueSeconds = value; })} value={spec.queue.maxQueueSeconds} />
        <SelectField<ModelDeploymentRolloutStrategy> label="Rollout strategy" onChange={(value) => update((next) => { next.rollout.strategy = value; if (value === "Recreate") { next.rollout.maxUnavailable = 1; next.rollout.maxSurge = 0; } })} value={spec.rollout.strategy} values={["Rolling", "Recreate"]} />
        <NumberField label="Maximum unavailable" max={10000} onChange={(value) => update((next) => { next.rollout.maxUnavailable = value; })} value={spec.rollout.maxUnavailable} />
        <NumberField label="Maximum surge" max={10000} onChange={(value) => update((next) => { next.rollout.maxSurge = value; })} value={spec.rollout.maxSurge} />
        <NumberField label="Progress deadline (seconds)" max={86400} min={60} onChange={(value) => update((next) => { next.rollout.progressDeadlineSeconds = value; })} value={spec.rollout.progressDeadlineSeconds} />
      </FormSection>

      <FormSection disabled={disabled} title="Publication and tenant access" detail="Publication controls route exposure. This release implements tenant-default.v1 plus inline principal restrictions; request, concurrency, and budget limits remain configurable on API keys.">
        <label className="checkbox-field"><input checked={spec.exposure.openAI} onChange={(event) => update((next) => { next.exposure.openAI = event.target.checked; if (!event.target.checked) next.exposure.openAIAliases = []; })} type="checkbox" />Publish OpenAI-compatible route</label>
        <TextField disabled={!spec.exposure.openAI} hint="Comma-separated public model aliases." label="OpenAI aliases" onChange={(value) => update((next) => { next.exposure.openAIAliases = uniqueCsv(value); })} value={spec.exposure.openAIAliases.join(", ")} />
        <label className="checkbox-field"><input checked={spec.exposure.mcp} onChange={(event) => update((next) => { next.exposure.mcp = event.target.checked; if (!event.target.checked) next.exposure.mcpToolName = null; })} type="checkbox" />Expose MCP tool</label>
        <TextField disabled={!spec.exposure.mcp} label="MCP tool name" onChange={(value) => update((next) => { next.exposure.mcpToolName = value || null; })} placeholder={spec.exposure.mcp ? "model_tool_name" : "Not exposed"} value={spec.exposure.mcpToolName ?? ""} />
        <SelectField<ModelDeploymentVisibility> label="Visibility" onChange={(value) => update((next) => { next.policy.visibility = value; })} value={spec.policy.visibility} values={["Private", "Tenant"]} />
        <TextField disabled hint="Only tenant-default.v1 is implemented in this release." label="Tenant policy reference" onChange={() => undefined} required value="tenant-default.v1" />
        <TextField hint="Comma-separated principal IDs; empty means the referenced policy decides." label="Allowed principals" onChange={(value) => update((next) => { next.policy.allowedPrincipalIds = uniqueCsv(value); })} value={spec.policy.allowedPrincipalIds.join(", ")} />
        <TextField disabled hint="Use API-key request/concurrency/budget limits; per-model rate policies are not implemented yet." label="Rate policy reference" onChange={() => undefined} placeholder="Not supported in this release" value="" />
      </FormSection>

      <FormSection disabled={disabled} title="Existing-resource adoption" detail="Observe is read-only. Claim requires an immutable, independently verified inventory receipt and remains mutation-gated.">
        <SelectField<ModelDeploymentAdoptionMode> label="Adoption mode" onChange={(value) => update((next) => { next.adoption.mode = value; next.adoption.receiptRef = value === "Claim" ? next.adoption.receiptRef ?? { name: "", digest: "" } : null; })} value={spec.adoption.mode} values={["None", "Observe", "Claim"]} />
        <TextField disabled={!spec.adoption.receiptRef} label="Receipt name" onChange={(value) => update((next) => { if (next.adoption.receiptRef) next.adoption.receiptRef.name = value; })} placeholder={spec.adoption.receiptRef ? "Verified receipt" : "Not used"} value={spec.adoption.receiptRef?.name ?? ""} />
        <TextField disabled={!spec.adoption.receiptRef} label="Receipt digest" onChange={(value) => update((next) => { if (next.adoption.receiptRef) next.adoption.receiptRef.digest = value; })} placeholder={spec.adoption.receiptRef ? "sha256:…" : "Not used"} value={spec.adoption.receiptRef?.digest ?? ""} />
      </FormSection>
    </form>
  );
}
