import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { envelopeRequest } from "../../api/client";
import { DataBoundary } from "../../components/DataBoundary";

type Campaign = {
  id: string; name: string; total: number; queued: number; running: number;
  succeeded: number; failed: number; unsupported: number; capacity_unavailable: number;
};
type Metric = { count: number; median: number | null; min: number | null; max: number | null };
type Profile = {
  model_id: string; workload_class: string; cache_condition: string;
  hardware: { pool: string; gpu_product: string } | null;
  valid_samples: number; placement_evidence: string;
  metrics: Record<string, Metric>; outcomes: Record<string, number>;
};

function Timing({ value }: { value: Metric }) {
  if (value.median === null) return <span title="No measurement was recorded">—</span>;
  return <span title={`Range ${value.min?.toFixed(2)}–${value.max?.toFixed(2)} s; ${value.count} measurements`}>
    {value.median.toFixed(2)} s
  </span>;
}

export function BenchmarksPage() {
  const [selected, setSelected] = useState("");
  const campaigns = useQuery({
    queryKey: ["performance-campaigns"],
    queryFn: ({ signal }) => envelopeRequest<{ items: Campaign[] }>("/performance/campaigns", { signal }),
    refetchInterval: 15000,
  });
  const id = selected || campaigns.data?.data.items[0]?.id;
  const details = useQuery({
    queryKey: ["performance-campaign", id], enabled: Boolean(id),
    queryFn: ({ signal }) => envelopeRequest<{ profiles: Profile[] }>(`/performance/campaigns/${id}`, { signal }),
    refetchInterval: 15000,
  });
  return <div className="page-stack">
    <section className="panel section-heading">
      <div><span className="eyebrow">Measured performance</span><h2>Benchmarks</h2>
        <p>Durable experiment results. Advisory only: benchmarks do not change placement or scaling.</p>
      </div><Link className="button" to="/admin/capacity">Capacity</Link>
    </section>
    <DataBoundary data={campaigns.data} error={campaigns.error} pending={campaigns.isPending}>
      {({ data }) => <section className="panel">
        {data.items.length === 0 ? <p>No benchmark campaigns have been submitted.</p> : <>
          <label>Campaign <select value={id} onChange={(event) => setSelected(event.target.value)}>
            {data.items.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
          </select></label>
          {data.items.filter((row) => row.id === id).map((row) => <p key={row.id} role="status">
            {row.succeeded}/{row.total} passed · {row.running} running · {row.queued} queued · {row.failed} failed
            {" · "}{row.unsupported} unsupported · {row.capacity_unavailable} capacity unavailable
          </p>)}
        </>}
      </section>}
    </DataBoundary>
    {id && <DataBoundary data={details.data} error={details.error} pending={details.isPending}>
      {({ data }) => <section className="panel">
        <p>Times are medians of validated results. “—” means unmeasured, not zero. Small sample sets do not establish tail latency.
          Uncontrolled cache runs are not cold-start benchmarks.</p>
        <div className="table-wrap"><table><thead><tr>
          <th>App / workload</th><th>Observed hardware</th><th>Cache</th><th>Valid samples</th>
          <th>End to end</th><th>Queue</th><th>Startup</th><th>Execution</th><th>Placement evidence</th>
        </tr></thead><tbody>{data.profiles.map((profile, index) => <tr key={`${profile.model_id}-${index}`}>
          <td>{profile.model_id}<br /><small>{profile.workload_class}</small></td>
          <td>{profile.hardware ? `${profile.hardware.gpu_product} / ${profile.hardware.pool}` : "Unobserved"}</td>
          <td>{profile.cache_condition}</td><td>{profile.valid_samples}</td>
          {["elapsed_seconds", "queue_seconds", "startup_seconds", "execution_seconds"].map((metric) =>
            <td key={metric}><Timing value={profile.metrics[metric]} /></td>)}
          <td>{profile.placement_evidence}</td>
        </tr>)}</tbody></table></div>
      </section>}
    </DataBoundary>}
  </div>;
}
