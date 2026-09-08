import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { adminApi } from "../../api/client";
import { userApi } from "../../api/userClient";
import type { AdminApiKey, AdminApiKeyDisclosure } from "../../api/accessTypes";
import { useSession } from "../../auth/SessionContext";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { MetricCard } from "../../components/Measurement";
import { OneTimeSecretDialog } from "../../components/OneTimeSecretDialog";
import { TimeSeriesChart } from "../../components/TimeSeriesChart";
import { rolePermits } from "../../lib/access";
import { formatTimestamp } from "../../lib/format";
import {
  CreateKeyDialog,
  EditKeyDialog,
  RevokeKeyDialog,
  RotateKeyDialog,
} from "../access/AccessDialogs";
import { UserSettingsForm } from "./UserSettingsForm";

type Dialog =
  | { kind: "settings" | "create-key" }
  | { kind: "edit-key" | "rotate-key" | "revoke-key"; key: AdminApiKey }
  | null;

export function UserDetailPage() {
  const { userId = "" } = useParams();
  const { params, navigation } = useAdminTimeWindow();
  const { session } = useSession();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [disclosure, setDisclosure] = useState<AdminApiKeyDisclosure | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["inference-user", userId, params.toString()],
    queryFn: ({ signal }) => userApi.detail(userId, params, signal),
  });
  const canEdit = Boolean(
    session && rolePermits(session.principal.role, "operator"),
  );
  async function change(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      setDialog(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["inference-user", userId] }),
        queryClient.invalidateQueries({ queryKey: ["inference-users"] }),
      ]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "User action failed");
    } finally {
      setBusy(false);
    }
  }
  function open(next: Dialog) {
    setError(null);
    setDialog(next);
  }
  return (
    <DataBoundary
      data={query.data}
      error={query.error}
      pending={query.isPending}
    >
      {({ data }) => {
        const { user, keys, apps } = data;
        return (
          <div className="page-stack">
            <section className="panel section-heading">
              <div>
                <Link to={`/admin/users?${navigation}`}>Users</Link>
                <h2>{user.display_name}</h2>
                <p>
                  {user.principal_id} · {user.tenant_id} ·{" "}
                  {user.kind ?? "Not classified"}
                  {user.team ? ` · ${user.team}` : ""}
                </p>
                <p>
                  {user.enabled
                    ? "New inference enabled"
                    : "New inference disabled"}{" "}
                  · Academic eligibility:{" "}
                  {user.academic_eligible === null
                    ? "Existing key policy"
                    : user.academic_eligible
                      ? "Eligible"
                      : "Not eligible"}
                </p>
              </div>
              {canEdit && (
                <button
                  className="button"
                  onClick={() => open({ kind: "settings" })}
                >
                  User settings
                </button>
              )}
            </section>
            <section className="panel section-stack">
              <h3>Usage in selected window</h3>
              <p>{user.usage.attribution}</p>
              <div className="count-strip">
                <div>
                  <strong>{user.usage.requests}</strong>
                  <span>Requests</span>
                </div>
                <div>
                  <strong>{user.usage.succeeded}</strong>
                  <span>Succeeded</span>
                </div>
                <div>
                  <strong>{user.usage.failed}</strong>
                  <span>Failed</span>
                </div>
                <div>
                  <strong>{user.usage.pending + user.usage.running}</strong>
                  <span>In progress</span>
                </div>
                <div>
                  <strong>{user.usage.scientific_requests}</strong>
                  <span>Scientific runs</span>
                </div>
              </div>
              <div className="metrics-grid">
                <MetricCard
                  label="GPU occupied"
                  value={user.usage.scheduler_occupied_gpu_seconds}
                />
                <MetricCard
                  label="GPU active"
                  value={user.usage.active_gpu_seconds}
                />
                <MetricCard
                  label="GPU occupied idle"
                  value={user.usage.occupied_idle_gpu_seconds}
                />
                <MetricCard
                  label="Input tokens"
                  value={user.usage.input_tokens}
                />
                <MetricCard
                  label="Output tokens"
                  value={user.usage.output_tokens}
                />
              </div>
            </section>
            <TimeSeriesChart
              title="Logical requests over time"
              unit="requests"
              source="PostgreSQL operations"
              aggregation={`Accepted requests per ${user.usage.bucket_seconds}-second bucket, all keys`}
              state="available"
              reason={null}
              summary={{
                average: user.usage.request_series.length
                  ? user.usage.requests / user.usage.request_series.length
                  : null,
                maximum: user.usage.request_series.length
                  ? Math.max(
                      ...user.usage.request_series.map(
                        (point) => point.requests,
                      ),
                    )
                  : null,
              }}
              series={[
                {
                  id: "requests",
                  label: "Requests",
                  points: user.usage.request_series.map((point) => ({
                    at: point.at,
                    value: point.requests,
                  })),
                },
              ]}
            />
            <section className="panel section-stack">
              <header className="section-heading">
                <div>
                  <h3>API keys</h3>
                  <p>{data.policy_note}</p>
                </div>
                {canEdit && (
                  <button
                    className="button button--primary"
                    onClick={() => open({ kind: "create-key" })}
                  >
                    Create API key
                  </button>
                )}
              </header>
              <div className="table-frame">
                <table className="resource-table">
                  <thead>
                    <tr>
                      <th>Key</th>
                      <th>State</th>
                      <th>Key app policy</th>
                      <th>Limits</th>
                      <th>Last used</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {keys.map((key) => (
                      <tr key={key.id}>
                        <th>
                          {key.name ?? key.prefix}
                          <span className="secondary-line">{key.prefix}</span>
                        </th>
                        <td>{key.state}</td>
                        <td>{key.models.join(", ")}</td>
                        <td>
                          {key.max_concurrency} concurrent
                          <span className="secondary-line">
                            {key.request_budget ?? "Unlimited"} requests ·{" "}
                            {key.gpu_seconds_budget ?? "Unlimited"} GPU-s budget
                          </span>
                        </td>
                        <td>{formatTimestamp(key.last_used_at)}</td>
                        <td>
                          {canEdit && key.state === "active" && (
                            <div className="button-group">
                              <button
                                className="button"
                                onClick={() => open({ kind: "edit-key", key })}
                              >
                                Edit
                              </button>
                              <button
                                className="button"
                                onClick={() =>
                                  open({ kind: "rotate-key", key })
                                }
                              >
                                Rotate
                              </button>
                              <button
                                className="button"
                                onClick={() =>
                                  open({ kind: "revoke-key", key })
                                }
                              >
                                Revoke
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                    {!keys.length && (
                      <tr>
                        <td colSpan={6}>No keys for this user yet.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
            {dialog?.kind === "settings" && (
              <UserSettingsForm
                user={user}
                apps={apps}
                busy={busy}
                error={error}
                onClose={() => setDialog(null)}
                onSave={(payload) =>
                  change(() => userApi.update(userId, payload, params))
                }
              />
            )}
            {dialog?.kind === "create-key" && (
              <CreateKeyDialog
                principals={[
                  {
                    id: user.id,
                    subject: user.principal_id,
                    display_name: user.display_name,
                    enabled: true,
                    tenant_id: user.tenant_id,
                  },
                ]}
                tenant={user.tenant_id}
                fixedTenant
                busy={busy}
                error={error}
                onClose={() => setDialog(null)}
                onSave={(payload) =>
                  change(async () => {
                    const result = await userApi.createKey(
                      userId,
                      payload,
                      params,
                    );
                    setDisclosure(result.data);
                  })
                }
              />
            )}
            {dialog?.kind === "edit-key" && (
              <EditKeyDialog
                apiKey={dialog.key}
                busy={busy}
                error={error}
                onClose={() => setDialog(null)}
                onSave={(payload) =>
                  change(() => adminApi.updateKey(dialog.key.id, payload))
                }
              />
            )}
            {dialog?.kind === "rotate-key" && (
              <RotateKeyDialog
                apiKey={dialog.key}
                busy={busy}
                error={error}
                onClose={() => setDialog(null)}
                onSave={(payload) =>
                  change(async () => {
                    const result = await adminApi.rotateKey(
                      dialog.key.id,
                      payload,
                    );
                    setDisclosure(result.data);
                  })
                }
              />
            )}
            {dialog?.kind === "revoke-key" && (
              <RevokeKeyDialog
                apiKey={dialog.key}
                busy={busy}
                error={error}
                onClose={() => setDialog(null)}
                onConfirm={() =>
                  change(() => adminApi.revokeKey(dialog.key.id))
                }
              />
            )}
            {disclosure && (
              <OneTimeSecretDialog
                disclosure={disclosure}
                onDismiss={() => setDisclosure(null)}
              />
            )}
          </div>
        );
      }}
    </DataBoundary>
  );
}
