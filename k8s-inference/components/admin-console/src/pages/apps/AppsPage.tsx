import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { appsApi } from "../../api/appsClient";
import { useAdminTimeWindow } from "../../components/AdminTimeWindow";
import { DataBoundary } from "../../components/DataBoundary";
import { useSession } from "../../auth/SessionContext";
import { formatTimestamp } from "../../lib/format";

export function AppsPage() {
  const { params, navigation } = useAdminTimeWindow();
  const [searchParams, setSearchParams] = useSearchParams();
  const search = (searchParams.get("search") ?? "").slice(0, 128);
  const cursor = searchParams.get("cursor") ?? undefined;
  const query = useQuery({
    queryKey: ["apps", params.toString(), search, cursor],
    queryFn: ({ signal }) =>
      appsApi.list(params, { search: search || undefined, cursor }, signal),
  });
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const session = useSession();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const create = useMutation({
    mutationFn: async () => {
      const app = query.data?.data.items.find((item) => item.app_id === source);
      if (!app || !app.capabilities.duplicate)
        throw new Error(
          "Choose an app that supports an independent deployment.",
        );
      return appsApi.create({
        model_ref: app.model_ref,
        display_name: name.trim(),
        source_app_id: app.app_id,
      });
    },
    onSuccess: (response) => {
      void queryClient.invalidateQueries({ queryKey: ["apps"] });
      navigate(
        `/admin/apps/${encodeURIComponent(response.data.app_id)}/settings`,
      );
    },
  });
  return (
    <div className="page-stack">
      <div className="toolbar">
        <label>
          Search apps
          <input
            aria-label="Search apps"
            value={search}
            maxLength={128}
            placeholder="App name or model"
            onChange={(event) => {
              const next = new URLSearchParams(searchParams);
              next.delete("cursor");
              event.target.value
                ? next.set("search", event.target.value)
                : next.delete("search");
              setSearchParams(next, { replace: true });
            }}
          />
        </label>
        <span className="context-spacer" />
        <button
          className="button button--primary"
          disabled={session.session.principal.role === "viewer"}
          onClick={() => setCreating(!creating)}
          type="button"
        >
          Create app
        </button>
      </div>
      <p className="window-caption">
        Each app has its own route, settings and usage. Runs count logical
        inference operations in {formatTimestamp(params.get("from"))} –{" "}
        {formatTimestamp(params.get("to"))}, not polls or idempotent replays.
        Last used is retained across windows.
      </p>
      {creating ? (
        <form
          className="panel form-grid"
          aria-label="Create app"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <label>
            App name
            <input
              aria-label="App name"
              required
              maxLength={160}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            Base deployment
            <select
              aria-label="Base deployment"
              required
              value={source}
              onChange={(event) => setSource(event.target.value)}
            >
              <option value="">Select a supported app</option>
              {query.data?.data.items
                .filter((app) => app.capabilities.duplicate)
                .map((app) => (
                  <option key={app.app_id} value={app.app_id}>
                    {app.display_name} · {app.model_ref}
                  </option>
                ))}
            </select>
          </label>
          <p className="supporting-copy form-grid__wide">
            Creates a separate app ID and public route. It does not rename the
            source app or merge its history.
          </p>
          {create.error ? (
            <p role="alert" className="inline-notice inline-notice--error">
              {create.error.message}
            </p>
          ) : null}
          <div>
            <button
              className="button button--primary"
              type="submit"
              disabled={create.isPending || !name.trim() || !source}
            >
              {create.isPending ? "Creating…" : "Create independent app"}
            </button>{" "}
            <button
              className="button"
              type="button"
              onClick={() => setCreating(false)}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      <DataBoundary
        data={query.data}
        error={query.error}
        pending={query.isPending}
        empty={query.data?.data.items.length === 0}
        emptyLabel="No apps match this search."
      >
        {({ data }) => (
          <>
            <div className="table-frame">
              <table className="resource-table">
                <caption className="sr-only">Apps and logical usage</caption>
                <thead>
                  <tr>
                    <th>App</th>
                    <th>Model / route</th>
                    <th>Status</th>
                    <th>Execution</th>
                    <th>Runs in window</th>
                    <th>Last used</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((app) => (
                    <tr key={app.app_id}>
                      <th scope="row">
                        <Link
                          className="resource-link"
                          to={{
                            pathname: `/admin/apps/${encodeURIComponent(app.app_id)}/runs`,
                            search: navigation.toString(),
                          }}
                        >
                          {app.display_name}
                        </Link>
                        <span className="secondary-line app-id">
                          {app.app_id}
                        </span>
                      </th>
                      <td>
                        {app.model_ref}
                        <span className="secondary-line">
                          <code>{app.public_model_id}</code>
                        </span>
                      </td>
                      <td>
                        <span className="mini-chip">{app.status}</span>
                        <span className="secondary-line">
                          {app.status_reason}
                        </span>
                      </td>
                      <td>
                        {app.execution_mode === "scientific"
                          ? "Batch workflow"
                          : "Serving"}
                        {app.academic_required ? (
                          <span className="secondary-line">
                            Academic eligibility required
                          </span>
                        ) : null}
                      </td>
                      <td>{app.logical_run_count ?? "—"}</td>
                      <td>{formatTimestamp(app.last_used_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="pagination-actions">
              {cursor ? (
                <button
                  className="button"
                  onClick={() => {
                    const next = new URLSearchParams(searchParams);
                    next.delete("cursor");
                    setSearchParams(next);
                  }}
                >
                  Newest apps
                </button>
              ) : null}
              {data.next_cursor ? (
                <button
                  className="button"
                  onClick={() => {
                    const next = new URLSearchParams(searchParams);
                    next.set("cursor", data.next_cursor!);
                    setSearchParams(next);
                  }}
                >
                  More apps
                </button>
              ) : null}
            </div>
          </>
        )}
      </DataBoundary>
    </div>
  );
}
