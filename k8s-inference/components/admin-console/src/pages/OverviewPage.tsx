import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../api/client";
import type { ModelState } from "../api/types";
import { DataBoundary } from "../components/DataBoundary";
import { MetricCard, Measurement } from "../components/Measurement";
import { StatusChip } from "../components/StatusChip";

export function OverviewPage() {
  const [searchParams] = useSearchParams();
  const query = useQuery({
    queryKey: ["admin-overview", searchParams.toString()],
    queryFn: ({ signal }) => adminApi.overview(searchParams, signal),
  });

  return (
    <DataBoundary data={query.data} error={query.error} pending={query.isPending}>
      {({ data }) => (
        <div className="page-stack">
          <section aria-labelledby="fleet-heading">
            <div className="section-heading">
              <div><span className="eyebrow">Fleet now</span><h2 id="fleet-heading">Inference at a glance</h2></div>
              <Link to={{ pathname: "/admin/models", search: searchParams.toString() }} className="text-link">View models</Link>
            </div>
            <div className="metric-grid">
              <MetricCard label="Requests" value={data.requests_per_second} detail="Current observed rate" />
              <MetricCard label="Tokens" value={data.tokens_per_second} detail="Across text runtimes" />
              <MetricCard label="Error rate" value={data.error_rate} detail="Terminal operations" />
              <MetricCard label="Queued" value={data.queued_operations} detail="Durable pending work" />
            </div>
          </section>

          <div className="split-grid">
            <section className="panel" aria-labelledby="state-heading">
              <div className="section-heading"><div><span className="eyebrow">Models</span><h2 id="state-heading">Runtime state</h2></div></div>
              <div className="state-list">
                {data.model_states.map((item) => (
                  <div className="state-list__row" key={item.state}>
                    <StatusChip state={item.state as ModelState} reason={`${item.models} models in this state`} />
                    <strong>{item.models}</strong>
                  </div>
                ))}
              </div>
            </section>
            <section className="panel" aria-labelledby="capacity-heading">
              <div className="section-heading"><div><span className="eyebrow">Capacity</span><h2 id="capacity-heading">GPU pools</h2></div></div>
              <dl className="definition-grid">
                <div><dt>Allocatable GPUs</dt><dd><Measurement value={data.capacity.allocatable_gpus} /></dd></div>
                <div><dt>Ready GPU nodes</dt><dd><Measurement value={data.capacity.ready_gpu_nodes} /></dd></div>
                <div><dt>Preemptible nodes</dt><dd><Measurement value={data.capacity.preemptible_gpu_nodes} /></dd></div>
                <div><dt>Active replicas</dt><dd><Measurement value={data.capacity.active_gpu_replicas} /></dd></div>
              </dl>
            </section>
          </div>

          <section className="panel" aria-labelledby="performance-heading">
            <div className="section-heading"><div><span className="eyebrow">Performance</span><h2 id="performance-heading">Latency and consumption</h2></div></div>
            <div className="metric-grid metric-grid--small">
              <MetricCard label="Latency p50" value={data.latency.p50_seconds} />
              <MetricCard label="Latency p95" value={data.latency.p95_seconds} />
              <MetricCard label="TTFT p95" value={data.latency.ttft_p95_seconds} />
              <MetricCard label="GPU consumption" value={data.measured_gpu_seconds} />
            </div>
          </section>
        </div>
      )}
    </DataBoundary>
  );
}
