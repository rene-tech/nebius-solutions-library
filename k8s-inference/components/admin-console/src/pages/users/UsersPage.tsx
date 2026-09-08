import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { userApi } from "../../api/userClient";
import type { UserCreate } from "../../api/userTypes";
import { useSession } from "../../auth/SessionContext";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { Measurement } from "../../components/Measurement";
import { rolePermits } from "../../lib/access";
import { formatTimestamp } from "../../lib/format";
import { UserSettingsForm } from "./UserSettingsForm";

export function UsersPage() {
  const { params, navigation } = useAdminTimeWindow();
  const [search, setSearch] = useSearchParams();
  const [tenantFilter, setTenantFilter] = useState(
    search.get("tenant_id") ?? "",
  );
  const queryParams = new URLSearchParams(params);
  if (search.get("tenant_id"))
    queryParams.set("tenant_id", search.get("tenant_id")!);
  const { session } = useSession();
  const queryClient = useQueryClient();
  const [create, setCreate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["inference-users", queryParams.toString()],
    queryFn: ({ signal }) => userApi.list(queryParams, signal),
  });
  const canEdit = Boolean(
    session && rolePermits(session.principal.role, "operator"),
  );
  async function save(payload: UserCreate) {
    setBusy(true);
    setError(null);
    try {
      await userApi.create(payload, params);
      setCreate(false);
      await queryClient.invalidateQueries({ queryKey: ["inference-users"] });
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "User creation failed",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="page-stack">
      <section className="panel section-heading">
        <div>
          <span className="eyebrow">Inference owners</span>
          <h2>Users</h2>
          <p>
            People and service accounts, with usage across every key. Console
            roles remain separate.
          </p>
        </div>
        {canEdit && (
          <button
            className="button button--primary"
            onClick={() => setCreate(true)}
          >
            Create user
          </button>
        )}
      </section>
      {!session?.principal.tenant_id && (
        <form
          className="panel filter-bar"
          onSubmit={(event) => {
            event.preventDefault();
            const next = new URLSearchParams(search);
            if (tenantFilter.trim()) next.set("tenant_id", tenantFilter.trim());
            else next.delete("tenant_id");
            setSearch(next);
          }}
        >
          <label>
            Tenant filter
            <input
              value={tenantFilter}
              maxLength={120}
              onChange={(event) => setTenantFilter(event.target.value)}
              placeholder="All tenants"
            />
          </label>
          <button className="button" type="submit">
            Apply filter
          </button>
        </form>
      )}
      <DataBoundary
        data={query.data}
        error={query.error}
        pending={query.isPending}
      >
        {({ data }) => (
          <section className="panel section-stack">
            <p>
              Logical requests accepted in the selected window. Polling and key
              rotation do not add requests.
            </p>
            <div className="table-frame">
              <table className="resource-table">
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Team / tenant</th>
                    <th>Keys</th>
                    <th>Requests</th>
                    <th>Occupied GPU time</th>
                    <th>Last request</th>
                    <th>Access</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((user) => (
                    <tr key={user.id}>
                      <th>
                        <Link to={`/admin/users/${user.id}?${navigation}`}>
                          {user.display_name}
                        </Link>
                        <span className="secondary-line">
                          {user.principal_id} · {user.kind ?? "not classified"}
                        </span>
                      </th>
                      <td>
                        {user.team ?? "—"}
                        <span className="secondary-line">{user.tenant_id}</span>
                      </td>
                      <td>
                        {user.active_key_count} active / {user.key_count}
                      </td>
                      <td>
                        {user.usage.requests}
                        <span className="secondary-line">
                          {user.usage.succeeded} succeeded · {user.usage.failed}{" "}
                          failed
                        </span>
                      </td>
                      <td>
                        <Measurement
                          value={user.usage.scheduler_occupied_gpu_seconds}
                        />
                      </td>
                      <td>{formatTimestamp(user.usage.last_request_at)}</td>
                      <td>{user.enabled ? "Enabled" : "Disabled"}</td>
                    </tr>
                  ))}
                  {!data.items.length && (
                    <tr>
                      <td colSpan={7}>
                        No inference users yet. Create one or issue an
                        owner-attributed API key.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {data.truncated && (
              <p role="status">
                Showing the first {data.limit} owners. Narrow by tenant to see
                additional owners.
              </p>
            )}
          </section>
        )}
      </DataBoundary>
      {create && (
        <UserSettingsForm
          apps={query.data?.data.apps}
          fixedTenant={session?.principal.tenant_id}
          busy={busy}
          error={error}
          onClose={() => setCreate(false)}
          onSave={(payload) => save(payload as UserCreate)}
        />
      )}
    </div>
  );
}
