import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import type { ModelState } from "../api/types";
import { DataBoundary } from "../components/DataBoundary";
import { Measurement } from "../components/Measurement";
import { StatusChip } from "../components/StatusChip";

const states: Array<ModelState | "all"> = ["all", "hot", "loading", "queued", "cold", "unhealthy", "unsupported", "unknown"];

export function ModelsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const query = useQuery({
    queryKey: ["admin-models", searchParams.toString()],
    queryFn: ({ signal }) => adminApi.models(searchParams, signal),
  });
  const selectedState = searchParams.get("status") ?? "all";
  const items = query.data?.data.items.filter((item) => selectedState === "all" || item.runtime.state === selectedState) ?? [];

  function updateState(state: string) {
    const next = new URLSearchParams(searchParams);
    state === "all" ? next.delete("status") : next.set("status", state);
    next.delete("cursor");
    setSearchParams(next);
  }

  return (
    <div className="page-stack">
      <div className="toolbar">
        <label>Status <select value={selectedState} onChange={(event) => updateState(event.target.value)}>{states.map((state) => <option key={state} value={state}>{state}</option>)}</select></label>
        <span className="toolbar__summary">{items.length} of {query.data?.data.total ?? "—"} models</span>
      </div>
      <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && items.length === 0}>
        {() => (
          <div className="table-frame">
            <table className="resource-table">
              <caption className="sr-only">Models and current serving state</caption>
              <thead><tr><th scope="col">Model</th><th scope="col">State</th><th scope="col">Replicas</th><th scope="col">GPU profile</th><th scope="col">Requests</th><th scope="col">Errors</th><th scope="col">Cold start</th></tr></thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.identity.id}>
                    <th scope="row"><Link className="resource-link" to={{ pathname: `/admin/models/${encodeURIComponent(item.identity.id)}`, search: searchParams.toString() }}>{item.identity.display_name}</Link><span className="secondary-line">{item.identity.id} · {item.identity.runtime_kind}</span></th>
                    <td><StatusChip state={item.runtime.state} reason={item.runtime.reason} /></td>
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
