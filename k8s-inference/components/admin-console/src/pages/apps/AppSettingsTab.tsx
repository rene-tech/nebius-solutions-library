import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { appsApi } from "../../api/appsClient";
import { adminApi, AdminApiError } from "../../api/client";
import type { AppSettings, AppSummary } from "../../api/appsTypes";
import type {
  ModelDeploymentConfigurationOption,
  ModelDeploymentSpec,
} from "../../api/modelDeploymentTypes";
import type { ScientificStageStartupPolicy } from "../../api/scientificTypes";
import { useSession } from "../../auth/SessionContext";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { chartValue } from "../../components/TimeSeriesChart";
import { draftUpdate } from "../scientific/ScientificModelPolicyPanel";
import {
  fastStartLevelLabel,
  modelDeploymentFastStartLevels,
  normalizedFastStartStatus,
  scaleToZeroWarning,
} from "../../lib/modelDeployment";
import { formatTimestamp } from "../../lib/format";

function SettingsEditor({
  app,
  initial,
  option,
  onSaved,
  onReload,
}: {
  app: AppSummary;
  initial: AppSettings;
  option?: ModelDeploymentConfigurationOption;
  onSaved: () => void;
  onReload: () => Promise<AppSettings>;
}) {
  const { params } = useAdminTimeWindow();
  const { session } = useSession();
  const [baseline, setBaseline] = useState(initial);
  const [name, setName] = useState(initial.display_name);
  const [academic, setAcademic] = useState(initial.academic_required);
  const [spec, setSpec] = useState(initial.serving?.spec ?? null);
  const [paused, setPaused] = useState(
    initial.scientific?.desired.paused ?? false,
  );
  const [cap, setCap] = useState(
    initial.scientific?.desired.max_active_runs?.toString() ?? "",
  );
  const [startup, setStartup] = useState(
    initial.scientific?.desired.startup_policies ?? {},
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [stale, setStale] = useState(false);
  const canEdit =
    session.principal.role !== "viewer" && initial.capabilities.live_settings;
  const conflict = stale || initial.app_revision !== baseline.app_revision;
  function update(change: (value: ModelDeploymentSpec) => void) {
    if (spec) {
      const next = structuredClone(spec);
      change(next);
      setSpec(next);
      setSaved(false);
    }
  }
  function reset(value: AppSettings) {
    setBaseline(value);
    setName(value.display_name);
    setAcademic(value.academic_required);
    setSpec(value.serving?.spec ?? null);
    setPaused(value.scientific?.desired.paused ?? false);
    setCap(value.scientific?.desired.max_active_runs?.toString() ?? "");
    setStartup(value.scientific?.desired.startup_policies ?? {});
    setError(null);
    setStale(false);
  }
  async function reload() {
    setBusy(true);
    try {
      reset(await onReload());
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not reload current settings.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      if (!name.trim()) throw new Error("App name is required.");
      if (
        spec &&
        (!Number.isInteger(spec.availability.minReplicas) ||
          !Number.isInteger(spec.availability.maxReplicas) ||
          spec.availability.minReplicas < 0 ||
          spec.availability.maxReplicas < spec.availability.minReplicas)
      )
        throw new Error(
          "Replica limits must be whole numbers with maximum at least minimum.",
        );
      const policy = baseline.scientific;
      const response = await appsApi.updateSettings(
        app.app_id,
        {
          expected_app_revision: baseline.app_revision,
          ...(name !== baseline.display_name
            ? { display_name: name.trim() }
            : {}),
          ...(academic !== baseline.academic_required
            ? { academic_required: academic }
            : {}),
          ...(spec &&
          baseline.serving &&
          JSON.stringify(spec) !== JSON.stringify(baseline.serving.spec)
            ? { serving_spec: spec, serving_base_etag: baseline.serving.etag }
            : {}),
          ...(policy &&
          (paused !== policy.desired.paused ||
            cap !== (policy.desired.max_active_runs?.toString() ?? "") ||
            JSON.stringify(startup) !==
              JSON.stringify(policy.desired.startup_policies ?? {}))
            ? {
                scientific_policy: draftUpdate(policy, {
                  paused,
                  maxActiveRuns: cap,
                  reason: policy.desired.reason ?? "",
                  startupPolicies: startup,
                }),
              }
            : {}),
        },
        params,
      );
      reset(response.data);
      setSaved(true);
      onSaved();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Settings could not be saved.",
      );
      if (caught instanceof AdminApiError && caught.status === 409)
        setStale(true);
    } finally {
      setBusy(false);
    }
  }
  const snapshotChoices =
    spec &&
    option &&
    spec.runtime.image === option.default_spec.runtime.image &&
    spec.artifact.revision === option.default_spec.artifact.revision &&
    spec.artifact.manifestDigest === option.default_spec.artifact.manifestDigest
      ? (option.gpu_snapshot_choices ?? [])
      : [];
  const qualifiedLevel = option?.fast_start_qualified_level ?? "Off";
  const selectedSnapshot =
    spec?.cache.snapshotPreference === "Never"
      ? ""
      : (spec?.cache.snapshotRef?.name ?? "");
  const cpuOnly = spec?.placement.acceleratorsPerReplica === 0;
  const cpuResources = spec?.placement.cpuResources;
  const startupStages = [
    ...new Set([
      ...Object.keys(initial.scientific?.startup_options ?? {}),
      ...Object.keys(startup),
    ]),
  ];
  return (
    <form
      className="app-settings-section"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      {conflict ? (
        <div role="status" className="inline-notice inline-notice--warning">
          Settings changed or conflict with the current runtime. Your draft is
          preserved. Reload settings before saving.{" "}
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={() => void reload()}
          >
            Reload settings
          </button>
        </div>
      ) : null}
      {error ? (
        <div role="alert" className="inline-notice inline-notice--error">
          {error}
        </div>
      ) : null}
      {saved ? (
        <div role="status" className="inline-notice">
          Settings saved to the runtime API. Actual readiness is reported
          separately below.
        </div>
      ) : null}
      {!canEdit ? (
        <p className="inline-notice">
          {initial.unsupported_reason ??
            (session.principal.role === "viewer"
              ? "Operator role is required to edit settings."
              : "This app does not publish live settings.")}
        </p>
      ) : null}
      <fieldset disabled={!canEdit || busy}>
        <legend>App identity and eligibility</legend>
        <div className="form-grid">
          <label>
            App name
            <input
              aria-label="App display name"
              value={name}
              required
              maxLength={160}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={academic}
              onChange={(event) => setAcademic(event.target.checked)}
            />
            Academic eligibility required
          </label>
        </div>
        <p className="supporting-copy">
          App ID and public route remain unchanged. Eligibility settings do not
          override model license requirements.
        </p>
      </fieldset>
      {spec ? (
        <>
          <fieldset disabled={!canEdit || busy}>
            <legend>Serving and scaling</legend>
            <p className="supporting-copy" aria-label="Resources per worker">
              {cpuOnly ? (
                <>
                  CPU-only worker ·{" "}
                  {cpuResources
                    ? `${cpuResources.cpuMillis / 1000} CPU cores and ${chartValue(cpuResources.memoryBytes, "bytes")} requested per worker.`
                    : "CPU and memory requests are unavailable."}{" "}
                  No GPU is reserved.
                </>
              ) : (
                <>
                  {spec.placement.acceleratorsPerReplica}{" "}
                  {spec.placement.acceleratorsPerReplica === 1 ? "GPU" : "GPUs"}{" "}
                  requested per worker. This is the configured request, not
                  current allocation or utilization.
                </>
              )}
            </p>
            <div className="form-grid">
              <label>
                Desired state
                <select
                  aria-label="Desired state"
                  value={spec.lifecycle.desiredState}
                  onChange={(event) =>
                    update((next) => {
                      next.lifecycle.desiredState = event.target
                        .value as ModelDeploymentSpec["lifecycle"]["desiredState"];
                    })
                  }
                >
                  {["Enabled", "Draining", "Disabled"].map((state) => (
                    <option key={state}>{state}</option>
                  ))}
                </select>
              </label>
              <label>
                Minimum ready workers
                <input
                  aria-label="Minimum ready workers"
                  type="number"
                  min={0}
                  required
                  value={spec.availability.minReplicas}
                  onChange={(event) =>
                    update((next) => {
                      next.availability.minReplicas = Number(
                        event.target.value,
                      );
                    })
                  }
                />
                <small>
                  Reusable serving workers kept ready. This is not batch
                  concurrency.
                </small>
                {scaleToZeroWarning(option) ? (
                  <small>{scaleToZeroWarning(option)}</small>
                ) : null}
              </label>
              <label>
                Maximum workers
                <input
                  aria-label="Maximum workers"
                  type="number"
                  min={spec.availability.minReplicas}
                  required
                  value={spec.availability.maxReplicas}
                  onChange={(event) =>
                    update((next) => {
                      next.availability.maxReplicas = Number(
                        event.target.value,
                      );
                    })
                  }
                />
              </label>
              <label>
                Idle retention (seconds)
                <input
                  aria-label="Idle retention (seconds)"
                  type="number"
                  min={0}
                  max={604800}
                  required
                  value={spec.availability.idleSeconds}
                  onChange={(event) =>
                    update((next) => {
                      next.availability.idleSeconds = Number(
                        event.target.value,
                      );
                    })
                  }
                />
              </label>
              <label>
                Autoscaler cooldown (seconds)
                <input
                  aria-label="Autoscaler cooldown (seconds)"
                  type="number"
                  min={5}
                  max={86400}
                  required
                  value={spec.availability.cooldownSeconds}
                  onChange={(event) =>
                    update((next) => {
                      next.availability.cooldownSeconds = Number(
                        event.target.value,
                      );
                    })
                  }
                />
                <small>
                  Autoscaler scale-down cooldown, separate from model idle and
                  startup retention. Changing it does not change the hot floor.
                </small>
              </label>
              <label>
                Startup retention (seconds)
                <input
                  aria-label="Startup retention (seconds)"
                  type="number"
                  min={60}
                  max={7200}
                  value={spec.availability.startupTimeoutSeconds ?? 900}
                  onChange={(event) =>
                    update((next) => {
                      next.availability.startupTimeoutSeconds = Number(
                        event.target.value,
                      );
                    })
                  }
                />
                <small>
                  Unset uses 900s. Unchanged values preserve the original stored
                  setting.
                </small>
              </label>
              <label>
                Queue target
                <input
                  aria-label="Queue target"
                  type="number"
                  min={1}
                  required
                  value={spec.availability.targetQueueDepth}
                  onChange={(event) =>
                    update((next) => {
                      next.availability.targetQueueDepth = Number(
                        event.target.value,
                      );
                    })
                  }
                />
              </label>
            </div>
            <p className="supporting-copy">
              Desired revision {baseline.serving?.revision}; compatible pools:{" "}
              {spec.placement.poolRefs.join(", ")}. Saving does not provision
              new infrastructure.
            </p>
          </fieldset>
          <fieldset disabled={!canEdit || busy}>
            <legend>Cache and startup</legend>
            <div className="form-grid">
              <label>
                Cache tier
                <select
                  aria-label="Cache tier"
                  value={spec.cache.tier}
                  onChange={(event) =>
                    update((next) => {
                      next.cache.tier = event.target
                        .value as ModelDeploymentSpec["cache"]["tier"];
                    })
                  }
                >
                  {[
                    "Disabled",
                    "ObjectStore",
                    "SharedFilesystem",
                    "NodeLocal",
                  ].map((tier) => (
                    <option key={tier}>{tier}</option>
                  ))}
                </select>
              </label>
              {cpuOnly ? (
                <p className="supporting-copy">
                  GPU snapshotting is not applicable to this CPU-only app.
                </p>
              ) : (
                <label>
                  GPU snapshot
                  <select
                    aria-label="GPU snapshot"
                    value={selectedSnapshot}
                    onChange={(event) =>
                      update((next) => {
                        const choice = snapshotChoices.find(
                          (item) => item.bundle_id === event.target.value,
                        );
                        next.cache.snapshotPreference = choice
                          ? "Prefer"
                          : "Never";
                        next.cache.snapshotRef = choice
                          ? {
                              name: choice.bundle_id,
                              digest: choice.digest,
                              strategy: "CudaCheckpoint",
                            }
                          : null;
                      })
                    }
                  >
                    <option value="">Normal loading</option>
                    {selectedSnapshot &&
                    !snapshotChoices.some(
                      (choice) => choice.bundle_id === selectedSnapshot,
                    ) ? (
                      <option value={selectedSnapshot} disabled>
                        {selectedSnapshot} · not currently selectable
                      </option>
                    ) : null}
                    {snapshotChoices.map((choice) => (
                      <option
                        key={choice.bundle_id}
                        value={choice.bundle_id}
                        disabled={spec.placement.poolRefs.some(
                          (pool) => !choice.pool_refs.includes(pool),
                        )}
                      >
                        {choice.bundle_id}
                      </option>
                    ))}
                  </select>
                  <small>
                    Only qualified bundles for compatible pools are offered.
                    Selection is not proof of an observed restore.
                  </small>
                </label>
              )}
              <label>
                Fast-start policy
                <select
                  aria-label="Fast-start policy"
                  value={spec.fastStart?.mode ?? "Fixed"}
                  onChange={(event) =>
                    update((next) => {
                      next.fastStart =
                        event.target.value === "Automatic"
                          ? {
                              mode: "Automatic",
                              minimumLevel: "Off",
                              maximumLevel:
                                option?.fast_start_qualified_level ?? "Off",
                              fallbackPolicy: "AllowLowerLevel",
                            }
                          : {
                              mode: "Fixed",
                              level: "Off",
                              fallbackPolicy: "AllowLowerLevel",
                            };
                    })
                  }
                >
                  <option>Fixed</option>
                  <option>Automatic</option>
                </select>
              </label>
              {spec.fastStart?.mode !== "Automatic" ? (
                <label>
                  Target startup level
                  <select
                    aria-label="Target startup level"
                    value={spec.fastStart?.level ?? "Off"}
                    onChange={(event) =>
                      update((next) => {
                        next.fastStart = {
                          mode: "Fixed",
                          level: event.target
                            .value as (typeof modelDeploymentFastStartLevels)[number],
                          fallbackPolicy:
                            next.fastStart?.fallbackPolicy ?? "AllowLowerLevel",
                        };
                      })
                    }
                  >
                    {modelDeploymentFastStartLevels.map((level) => (
                      <option
                        key={level}
                        value={level}
                        disabled={
                          modelDeploymentFastStartLevels.indexOf(level) >
                          modelDeploymentFastStartLevels.indexOf(qualifiedLevel)
                        }
                      >
                        {fastStartLevelLabel(level)}
                      </option>
                    ))}
                  </select>
                  <small>
                    Levels above the exact runtime's qualified level are
                    unavailable.
                  </small>
                </label>
              ) : (
                <label>
                  Maximum automatic startup level
                  <select
                    aria-label="Maximum automatic startup level"
                    value={spec.fastStart.maximumLevel ?? qualifiedLevel}
                    onChange={(event) =>
                      update((next) => {
                        next.fastStart = {
                          ...next.fastStart!,
                          maximumLevel: event.target
                            .value as (typeof modelDeploymentFastStartLevels)[number],
                        };
                      })
                    }
                  >
                    {modelDeploymentFastStartLevels.map((level) => (
                      <option
                        key={level}
                        value={level}
                        disabled={
                          modelDeploymentFastStartLevels.indexOf(level) >
                          modelDeploymentFastStartLevels.indexOf(qualifiedLevel)
                        }
                      >
                        {fastStartLevelLabel(level)}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <p className="supporting-copy">
              Requested cache/snapshot policy is retained independently of
              observed readiness and actual restored backend.
            </p>
          </fieldset>
        </>
      ) : null}
      {initial.scientific ? (
        <fieldset disabled={!canEdit || busy}>
          <legend>Batch execution</legend>
          <p className="inline-notice">
            This app runs per-job workers; minimum ready workers is not
            supported. Maximum active runs is a separate dispatch limit.
          </p>
          <div className="form-grid">
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={paused}
                onChange={(event) => setPaused(event.target.checked)}
              />
              Pause new dispatch
            </label>
            <label>
              Maximum active runs
              <input
                aria-label="Maximum active runs"
                type="number"
                min={1}
                max={64}
                placeholder="Unbounded"
                value={cap}
                onChange={(event) => setCap(event.target.value)}
              />
            </label>
            {startupStages.map((stage) => (
              <label key={stage}>
                Startup · {stage}
                <select
                  aria-label={`Startup for ${stage}`}
                  value={
                    startup[stage]?.backend === "cuda-criu"
                      ? (startup[stage].bundle_id ?? "")
                      : startup[stage]
                        ? "normal"
                        : "inherit"
                  }
                  onChange={(event) => {
                    const next = { ...startup };
                    const value = event.target.value;
                    if (value === "inherit") delete next[stage];
                    else
                      next[stage] = (
                        value === "normal"
                          ? { backend: "normal-load", bundle_id: null }
                          : { backend: "cuda-criu", bundle_id: value }
                      ) satisfies ScientificStageStartupPolicy;
                    setStartup(next);
                  }}
                >
                  <option value="inherit">Inherit default</option>
                  <option value="normal">Normal loading</option>
                  {initial.scientific!.startup_options?.[stage]?.map(
                    (bundle) => (
                      <option key={bundle} value={bundle}>
                        {bundle}
                      </option>
                    ),
                  )}
                </select>
              </label>
            ))}
          </div>
          <p className="supporting-copy">
            Effective: {initial.scientific.effective.state} ·{" "}
            {initial.scientific.counts.running} running ·{" "}
            {initial.scientific.counts.queued} queued. Existing runs keep their
            admitted execution policy.
          </p>
        </fieldset>
      ) : null}
      <div className="configuration-actions">
        <button
          className="button button--primary"
          disabled={!canEdit || busy || conflict}
          type="submit"
        >
          {busy ? "Saving…" : "Save settings"}
        </button>
        <button
          className="button"
          disabled={busy}
          type="button"
          onClick={() => reset(initial)}
        >
          Discard draft
        </button>
        <span>
          App revision {baseline.app_revision} · changes are checked against the
          current revision.
        </span>
      </div>
    </form>
  );
}

export function AppSettingsTab({ app }: { app: AppSummary }) {
  const { params } = useAdminTimeWindow();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["app-settings", app.app_id],
    queryFn: ({ signal }) => appsApi.settings(app.app_id, params, signal),
  });
  const options = useQuery({
    queryKey: ["app-setting-options"],
    queryFn: ({ signal }) => adminApi.modelDeploymentCapabilities(signal),
    enabled: app.execution_mode === "serving",
  });
  const serving = query.data?.data.serving;
  const status = useQuery({
    queryKey: ["app-serving-status", app.app_id],
    queryFn: ({ signal }) =>
      adminApi.modelDeploymentStatus(
        serving!.name,
        { namespace: serving!.namespace, tenantId: serving!.tenant_id },
        signal,
      ),
    enabled: Boolean(serving),
    refetchInterval: 5000,
  });
  const fastStart = status.data
    ? normalizedFastStartStatus(status.data.data)
    : null;
  const academic = useQuery({
    queryKey: ["app-academic-assets", app.model_ref],
    queryFn: ({ signal }) => adminApi.academicAssets(params, signal),
    enabled: app.academic_required,
  });
  return (
    <div className="page-stack">
      <DataBoundary
        data={query.data}
        error={query.error}
        pending={query.isPending}
      >
        {({ data }) => (
          <SettingsEditor
            app={app}
            initial={data}
            option={options.data?.data.configuration_options.find(
              (option) => option.model_ref === app.model_ref,
            )}
            onReload={async () => {
              const response = await query.refetch();
              if (!response.data || response.error)
                throw response.error ?? new Error("Settings not available.");
              return response.data.data;
            }}
            onSaved={() => {
              void queryClient.invalidateQueries({
                queryKey: ["app-settings", app.app_id],
              });
              void queryClient.invalidateQueries({
                queryKey: ["app-detail", app.app_id],
              });
              void queryClient.invalidateQueries({ queryKey: ["apps"] });
              if (serving) void status.refetch();
            }}
          />
        )}
      </DataBoundary>
      {serving ? (
        <section className="panel">
          <h3>Actual runtime</h3>
          <DataBoundary
            data={status.data}
            error={status.error}
            pending={status.isPending}
          >
            {({ data }) => (
              <>
                <dl className="definition-grid">
                  <div>
                    <dt>Observation state</dt>
                    <dd>{data.state}</dd>
                  </div>
                  <div>
                    <dt>Observed</dt>
                    <dd>
                      {formatTimestamp(data.observation?.observed_at ?? null)}
                    </dd>
                  </div>
                  <div>
                    <dt>Ready workers</dt>
                    <dd>{data.observation?.status.replicas?.ready ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>Desired workers</dt>
                    <dd>{data.observation?.status.replicas?.desired ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>Requested fast start</dt>
                    <dd>{fastStart?.requestedLevel ?? "Not configured"}</dd>
                  </div>
                  <div>
                    <dt>Effective fast start</dt>
                    <dd>{fastStart?.effectiveLevel ?? "Not observed"}</dd>
                  </div>
                </dl>
                <p className="supporting-copy">
                  {fastStart?.reason ??
                    "Only actual observations establish readiness; a saved setting alone does not."}
                </p>
              </>
            )}
          </DataBoundary>
        </section>
      ) : null}
      {app.academic_required ? (
        <section className="panel">
          <h3>Assets and eligibility</h3>
          <DataBoundary
            data={academic.data}
            error={academic.error}
            pending={academic.isPending}
          >
            {({ data }) => (
              <>
                {data.items
                  .filter((item) => item.model_id === app.model_ref)
                  .map((item) => (
                    <p key={item.asset_id}>
                      {item.display_name} · {item.state} ·{" "}
                      {item.formal_license_status}
                    </p>
                  ))}
                <p className="supporting-copy">
                  Eligibility and dependency readiness are separate from current
                  worker state.
                </p>
              </>
            )}
          </DataBoundary>
        </section>
      ) : null}
    </div>
  );
}
