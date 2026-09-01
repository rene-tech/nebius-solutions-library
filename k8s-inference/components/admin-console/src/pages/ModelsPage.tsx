import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import type { ModelState } from "../api/types";
import { DataBoundary } from "../components/DataBoundary";
import { Measurement } from "../components/Measurement";
import { StatusChip } from "../components/StatusChip";
import { sharedContextParams } from "../lib/search";

const states: Array<ModelState | "all"> = ["all", "hot", "loading", "queued", "cold", "unhealthy", "unsupported", "unknown"];
const modelStateSet = new Set<string>(states);

export function ModelsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedState = searchParams.get("state") ?? searchParams.get("status") ?? "all";
  const selectedState = modelStateSet.has(requestedState) ? requestedState as ModelState | "all" : "all";
  const requestedSearch = searchParams.get("search") ?? "";
  const search = requestedSearch.length <= 128 ? requestedSearch : "";
  const context = sharedContextParams(searchParams);
  const navigationParams = new URLSearchParams(context);
  if (selectedState !== "all") navigationParams.set("state", selectedState);
  if (search) navigationParams.set("search", search);
  const query = useQuery({
    queryKey: ["admin-models", context.toString(), selectedState, search],
    queryFn: ({ signal }) => adminApi.models(context, {
      limit: 256,
      search: search.trim() || undefined,
      state: selectedState === "all" ? undefined : selectedState,
    }, signal),
  });
  const items = query.data?.data.items ?? [];

  function updateState(state: string) {
    const next = new URLSearchParams(navigationParams);
    next.delete("status");
    state === "all" ? next.delete("state") : next.set("state", state);
    next.delete("cursor");
    setSearchParams(next);
  }

  function updateSearch(value: string) {
    const next = new URLSearchParams(navigationParams);
    value ? next.set("search", value) : next.delete("search");
    next.delete("cursor");
    setSearchParams(next, { replace: true });
  }

  return (
    <div className="page-stack">
      <div className="toolbar">
        <label>Search <input aria-label="Search models" maxLength={128} onChange={(event) => updateSearch(event.target.value)} placeholder="Model ID or name" type="search" value={search} /></label>
        <label>Runtime state <select value={selectedState} onChange={(event) => updateState(event.target.value)}>{states.map((state) => <option key={state} value={state}>{state}</option>)}</select></label>
        <span className="toolbar__summary">{items.length} of {query.data?.data.total ?? "—"} models</span>
      </div>
      {requestedState !== selectedState || requestedSearch !== search ? <div className="freshness-notice" role="status"><strong>Invalid filter ignored</strong><span>Select a published runtime state and keep model search within 128 characters.</span></div> : null}
      <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && items.length === 0}>
        {() => (
          <div className="table-frame">
            <table className="resource-table">
              <caption className="sr-only">Models and current serving state</caption>
              <thead><tr><th scope="col">Model</th><th scope="col">Runtime state</th><th scope="col">Catalog support</th><th scope="col">Replicas</th><th scope="col">GPU profile</th><th scope="col">Requests</th><th scope="col">Errors</th><th scope="col">Cold start</th></tr></thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.identity.id}>
                    <th scope="row"><Link className="resource-link" to={{ pathname: `/admin/models/${encodeURIComponent(item.identity.id)}`, search: context.toString() }}>{item.identity.display_name}</Link><span className="secondary-line">{item.identity.id} · {item.identity.runtime_kind}</span></th>
                    <td><StatusChip state={item.runtime.state} reason={item.runtime.reason} /><span className="secondary-line model-state-reason">{item.runtime.reason}</span></td>
                    <td><span className="mini-chip">{item.identity.support_state}</span><span className="secondary-line">{item.identity.enabled ? "Enabled in this cluster" : "Disabled in this cluster"}</span></td>
                    <td>{item.runtime.ready_replicas ?? "—"} / {item.runtime.desired_replicas ?? "—"}<span className="secondary-line">ready / desired</span></td>
                    <td>{item.identity.gpu_count} × {item.identity.gpu_class}<span className="secondary-line">{item.identity.execution_mode}</span></td>
                    <td><Measurement compact value={item.metrics.requests_per_second} /></td>
                    <td><Measurement compact value={item.metrics.error_rate} /></td>
                    <td><Measurement compact value={item.metrics.cold_start_seconds} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </DataBoundary>
    </div>
  );
}
