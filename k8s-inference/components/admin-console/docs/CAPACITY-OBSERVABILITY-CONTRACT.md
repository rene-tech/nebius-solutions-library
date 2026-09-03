# Capacity and observability read contract

This document is the implementation checkpoint for the read-only capacity and
observability slice. It extends the sealed `fs2.admin-api/v1` envelope; it does
not grant the browser Kubernetes, Prometheus, Loki, or cloud credentials.

## HTTP surface

Both routes use the existing server-authorized `project`, `cluster`, `region`,
`from`, `to`, and `timezone` parameters and return `{meta,data}`. Independent
source failure is a `200` partial response with a bounded warning. Invalid
context is an RFC 9457-style problem response. No route accepts PromQL, LogQL,
Kubernetes selectors, resource paths, or an observability URL from the caller.

### `GET /admin/api/v1/capacity`

`data` contains:

- `node_pools`: provider-neutral groups derived from configured label keys,
  never node-group IDs. A pool has a stable opaque ID, nullable dynamic
  `gpu_class`, `capacity_type` (`regular`, `preemptible`, or `unknown`),
  nullable instance type and pool label, exact node-state measurements, and a
  list of extended GPU resources.
- Each GPU resource keeps Kubernetes `capacity`, `allocatable`, and estimated
  scheduled `allocated` distinct. `healthy` is nullable and can become
  available only from explicit accepted GPU-health evidence; allocatable is
  not relabeled as healthy.
- `resource_flavors`, `cluster_queues`, `local_queues`, `cohorts`, and a bounded
  pending/recent `workloads` list. Kubernetes quantities remain canonical
  strings. Nominal quota, reservation, usage, and borrowed quantity are four
  separate nullable values; an absent Kueue status field is never zero.
- `autoscalers`: bounded HPA and KEDA ScaledObject summaries. An observed empty
  list is a valid zero-object result; an unavailable API is not.
- `node_scaler`: a provider-neutral capability state. It remains unavailable
  until a cloud/node-autoscaler adapter is configured and probed; Kubernetes
  node counts are not presented as desired/min/max cloud capacity.

Every numeric measurement uses the sealed `{value,unit,state,source,reason}`
contract. Verified zero is numeric zero with `state=available`. Unknown,
missing, stale, timed-out, or unsupported values are `null` with a reason.

### `GET /admin/api/v1/observability`

`data.components` contains stable capability rows for Grafana, Prometheus,
Loki, OTel Collector, DCGM, Kueue, KEDA, Alertmanager, and Tempo. Each row
separates installation discovery, health, data availability, observation time,
version, and launch state. A launch is enabled only when a server-configured
HTTPS destination passes the allow-list and the component's own health/data
probe succeeds. OTel has no launch action. Alertmanager and Tempo remain absent
until installed and independently probed. Loki launches through an approved
Grafana Explore URL, not a raw credential-bearing Loki URL.

Optional `model_id` and `operation_id` parameters are bounded selectors used
only to fill server-owned dashboard variables. They never become metric
labels, free-form queries, or URL authorities.

The response also contains bounded aggregate signal summaries for OTel
pipeline failures and GPU utilization/memory telemetry. Request, error, and
latency summaries remain in the existing overview/model routes. A signal whose
accepted series is absent is unavailable, not zero.

## Adapter boundaries

- `CapacityAdminAdapter.snapshot()` returns only typed aggregate node, Pod
  request, Kueue, HPA, and KEDA projections. The concrete Kubernetes REST
  adapter has an immutable path allow-list, pagination/item bounds, response
  byte limits, and re-reads the rotated projected token for every request.
- `ObservabilityAdminAdapter.snapshot()` runs only fixed server-owned
  Prometheus queries and configured component probes. It never proxies a
  browser query or returns raw label maps.
- The existing catalog, PostgreSQL, Kubernetes model-state, and Prometheus
  model-metric adapters remain separate authorities. The service joins typed
  results only after freshness evaluation.
- A provider node-scaling protocol is intentionally separate from Kubernetes
  inventory so future Nebius or other implementations do not leak provider
  resource IDs into the public contract.

Each adapter call is cancelled after a configurable 0.1–10 second timeout
(default 2 seconds). Freshness is at most 90 seconds by default with the
existing five-minute future-clock guard. Timeout/transport/schema failures are
sanitized to unavailable. Stale typed snapshots remain identified in
`meta.sources`, but values are returned unavailable rather than silently
serving them as current.

## Kubernetes read permissions

The optional runtime reader receives a projected, ten-minute API token; the
default runtime service account still has no token. Its implementation uses
only `list`: a ClusterRole covers cluster-scoped `nodes`, ResourceFlavors,
ClusterQueues, and Cohorts, while explicit namespace Roles cover:

- model-namespace `pods`, HPAs, LocalQueues, Workloads, and ScaledObjects;
- system-namespace HPAs;
- one Role per entry in `adminReadAdapters.capacity.kueueExtraNamespaces`,
  granting `list` on LocalQueues and Workloads in that namespace only.

## Kueue scheduler contract

LocalQueues and Workloads are namespaced. Scientific lanes such as
`fs2-academic-poc` and `fs2-reference-data` therefore have to be named in
`kueueExtraNamespaces`; reading only the model namespace silently omits their
queues and understates contention. Every configured namespace is listed, and a
single unreadable namespace fails the whole projection rather than returning a
queue set that quietly drops one lane.

The scheduler renders a ResourceFlavor with the stable accelerator class in
`spec.nodeLabels["accelerator.fs2.nebius/class"]` and the capacity type in the
`fs2-serve.nebius.ai/capacity-type` annotation, because Kueue bounds
`spec.nodeLabels` to eight entries and the flavor spends them on the class and
the pool identity. The reader consumes both, so a flavor reports its exact GPU
class and capacity type instead of `unknown`.

The projection intentionally omits `audience`, so Kubernetes selects the
apiserver's configured identifier. This is provider-neutral: the retained
Nebius issuer is cluster-specific, and a guessed `kubernetes.default.svc`
audience is not a portable substitute.

No Secret, ConfigMap, exec, log, proxy, event-write, status-write, scale, patch,
create, or delete permission is included. Tests use `kubectl auth can-i`-style
rule assertions and require the rendered verbs and resources to match exactly.

## Metric-label and query policy

Principal, tenant, token, API-key, request, operation, prompt, response,
trace-context, payload, GPU UUID, and provider-resource IDs are forbidden
metric dimensions. DCGM is the deliberate bounded exception for workload
identity: Pod UID, Pod, namespace, container, approved application labels, and
FS2 workload labels are retained solely to join GPU samples to a model. BFF
PromQL templates accept no caller query or model identifier; they use fixed
server-owned expressions and bounded windows, aggregate away identity before
returning typed scalar results.

The new extension scrape contract retains only dimensions required to
distinguish aggregate control-plane series:

- DCGM keeps dynamic GPU class (`modelName`), device ordinal, Pod UID, Pod,
  namespace, container, the approved application-label allow-list, and FS2
  workload labels so model attribution remains possible. It drops GPU UUID,
  node hostname, operation, principal, token, and tenant dimensions.
- Kueue drops samples carrying raw Workload identity, then removes forbidden
  request/user labels. Canonical LocalQueue/model dimensions remain so Grafana
  can show bounded queue history per model alongside ClusterQueue, flavor,
  resource, status, and reason.
- KEDA keeps canonical ScaledObject/model dimensions for bounded per-model
  autoscaling history. ScaledJob samples and forbidden request/user labels are
  dropped.

Cardinality acceptance checks bound series per component relative to observed
nodes, queues, and control-plane components. No API returns an arbitrary label
map.

## Helm/Terraform scrape repair plan

1. Kueue 0.17.8 cannot express metric relabeling in its chart-owned monitor.
   The kube-prometheus-stack release therefore owns one bounded additional
   ServiceMonitor and a dedicated binding to Kueue's existing `/metrics` GET
   role. Prometheus includes `kueue-system` in its namespace allow-list.
2. Enable pinned KEDA operator, metric-server, and webhook Prometheus endpoints
   and ServiceMonitors with 30-second intervals, 10-second timeouts, and the
   per-model sample-drop policy; include `keda` in the namespace allow-list.
3. Keep the pinned DCGM ServiceMonitor and its bounded Kubernetes enrichment:
   Pod UID, Pod, namespace, container, the four approved `app.kubernetes.io`
   labels, and FS2 workload labels are retained so GPU samples can be joined to
   a model. Drop GPU UUID, hostname, operation, principal, token, and tenant
   dimensions.
   A profile without an FS2 DCGM exporter remains unavailable
   until that release is enabled and a target plus non-empty `DCGM_FI_*` query
   pass.
4. Acceptance requires active `up` targets plus non-empty bounded queries for
   each component. A rendered ServiceMonitor alone is not success. Rollback is
   the prior Terraform/Helm release; no retained rollout occurs from this card
   before exact diff review.

## Internal reads and external launch links

The control plane reads Prometheus over its private ClusterIP URL. That URL is
never returned to a browser and does not prove an operator UI is externally
reachable. Helm creates a non-secret observability config containing only a
link whose URL is HTTPS, whose exact hostname is allow-listed, and whose
`verifiedExternalRoute` value is true. The adapter still requires a successful
bounded health and data probe before setting `launch.enabled=true`.

For the retained topology, Grafana is the authenticated single pane and the
expected URL shape is
`https://<public-authority>/observability/grafana/`. Grafana must set
`[server] root_url` to that public subpath and
`serve_from_sub_path=true`, as described in the
[official Grafana server configuration](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/).
The integration release owns the cross-namespace `HTTPRoute`, prefix rewrite,
and `ReferenceGrant`; the relevant upstream contracts are the
[Gateway API rewrite guide](https://gateway-api.sigs.k8s.io/guides/user-guides/http-redirect-rewrite/)
and [ReferenceGrant API](https://gateway-api.sigs.k8s.io/api-types/referencegrant/).

Terraform exposes the values as `admin_observability_links`. Until that route
exists and an external authenticated request is verified, all URLs remain
empty and all flags false. At rollout, set only the Grafana URL/flag and its
exact host. Keep Prometheus and Loki URL values empty/false; they remain
private Grafana data sources. Native Grafana login supplies the initial access
control. Central SSO and finer Grafana team/role policy are a documented
follow-up and must not be inferred from the route alone.

Grafana's egress isolation is owned by the workloads Terraform policy
`fs2-grafana-to-observability-egress`. It selects only Grafana and allows DNS,
the exact configured Kubernetes API `/32` or `/128` destinations needed by the
datasource sidecar, the `fs2-control-db` reporting database, Prometheus on TCP
9090, and the exact Loki single-binary instance on TCP 3100. The Loki instance
is derived from the foundation's Grafana Service contract, producing
`fs2-<run>-loki` on fresh runs and `fs2-loki` retained. Prometheus uses the
stable `app.kubernetes.io/name=prometheus` label: kube-prometheus-stack is the
single Prometheus owner in that namespace, while its instance label is
`fs2-monitoring-prometheus` on the retained release and run-scoped on fresh
Terraform releases. Alertmanager has no route, datasource acceptance, or added
egress because it is absent.

## Retained read-only checkpoint, 2026-08-30

The explicit `fs2-serve-usn1` context reported 13/13 Ready nodes and 38
allocatable GPUs across dynamic one- and eight-GPU preemptible pools. Kueue
v0.17.8 reported one active ClusterQueue with 24 nominal GPU quota and explicit
zero reservation/usage; KEDA v2.20.2 had no ScaledObjects; one HPA was active.

Prometheus selected ServiceMonitor/PodMonitor namespaces do not include
`kueue-system` or `keda`. There were no active DCGM, Kueue, or KEDA scrape
targets and no `DCGM_FI_*`, `kueue_*`, or `keda_*` names. OTel series were
present. This confirms a wiring gap rather than a zero signal. No Kubernetes
object was changed.

Bounded live probes returned one Grafana target/build series, one Prometheus
build series, and two Loki target/build series. Those are the retained positive
acceptance expectations; DCGM, Kueue, and KEDA remain data-absent until the
scrape repair is rolled out and verified. Grafana already has Prometheus and
Loki data sources, but their health checks timed out under the existing egress
policy; the additive Terraform policy is the functional fix and does not
replace the separate PostgreSQL/DNS policy.

The retained edge also had no observability `HTTPRoute` at this checkpoint.
Consequently no Grafana, Prometheus, or Loki launch URL is currently verified
or launchable from this card alone.
