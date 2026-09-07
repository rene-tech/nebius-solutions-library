import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import { DataBoundary } from "../components/DataBoundary";
import { sharedContextParams } from "../lib/search";

export function ModelInventoryPage() {
  const [params] = useSearchParams();
  const context = sharedContextParams(params);
  const [search, setSearch] = useState("");
  const [missingOnly, setMissingOnly] = useState(false);
  const query = useQuery({
    queryKey: ["admin-model-inventory", context.toString()],
    queryFn: ({ signal }) => adminApi.modelInventory(context, signal),
    refetchInterval: 30_000,
  });
  const data = query.data?.data;
  const needle = search.trim().toLowerCase();
  const items = (data?.items ?? []).filter((item) =>
    (!missingOnly || item.availability === "not-deployed")
    && `${item.model_id} ${item.display_name}`.toLowerCase().includes(needle),
  );
  return (
    <div className="page-stack">
      <p>Every known serving model and scientific profile, including models not deployed in this cluster.
        Cold models are configured at zero replicas; batch-ready models start GPU Jobs on demand.
        A catalog entry alone does not mean the model is available.</p>
      <div className="toolbar">
        <label>Search <input aria-label="Search inventory" type="search" maxLength={128} value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        <label><input type="checkbox" checked={missingOnly} onChange={(event) => setMissingOnly(event.target.checked)} /> Not deployed only</label>
        <span className="toolbar__summary">{data ? `${data.configured} configured / ${data.total} known · ${data.not_deployed} not deployed` : "Loading inventory…"}</span>
      </div>
      <DataBoundary data={query.data} error={query.error} pending={query.isPending} empty={!query.isPending && items.length === 0}>
        {() => (
          <div className="table-frame">
            <table className="resource-table">
              <caption className="sr-only">Complete model inventory</caption>
              <thead><tr><th scope="col">Model / profile</th><th scope="col">Availability</th><th scope="col">Serving replicas</th><th scope="col">Batch readiness</th><th scope="col">GPU snapshot</th><th scope="col">Manage</th></tr></thead>
              <tbody>{items.map((item) => (
                <tr key={item.model_id}>
                  <th scope="row">{item.display_name}<span className="secondary-line">{item.model_id}</span></th>
                  <td><span className="mini-chip">{item.availability}</span><span className="secondary-line">{item.reason}</span></td>
                  <td>{item.desired_replicas === null ? "—" : `${item.ready_replicas ?? "—"} / ${item.desired_replicas}`}<span className="secondary-line">ready / desired</span></td>
                  <td>{item.batch_readiness ?? "Not configured"}</td>
                  <td>{item.gpu_snapshot}<span className="secondary-line">{item.snapshot_reason}</span></td>
                  <td>{item.management_path ? <Link className="resource-link" to={{ pathname: item.management_path, search: context.toString() }}>Manage</Link> : <Link className="resource-link" to={{ pathname: "/admin/model-deployments/new", search: context.toString() }}>Configure</Link>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </DataBoundary>
    </div>
  );
}
