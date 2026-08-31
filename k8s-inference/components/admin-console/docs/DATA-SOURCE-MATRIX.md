# Data-source and API matrix

## Authority rules

PostgreSQL is durable truth for operations, terminal usage facts, tokens,
activation intent, and audit. The catalog is immutable model/runtime identity
and compatibility truth. Kubernetes/Kueue provide current desired/observed
state. Prometheus and OTel provide bounded operational time series. DCGM is the
source for measured GPU telemetry only after scrape acceptance. The BFF performs
all joins; the browser queries none of these systems directly.

Principal, tenant, token, prompt, and response values are forbidden Prometheus
labels. Aggregate metric identities are joined to the durable ledger server-side.

| Product field | Primary source/API | Live state | Required BFF behavior / gap |
|---|---|---|---|
| Model identity, revision, runtime, immutable image/model, GPU compatibility, endpoint/MCP exposure | Catalog package and contracts | Available | Preserve immutable identities; treat compatibility outcome separately from live health. |
| Hot/loading/queued/cold/unhealthy/unsupported | Catalog + PostgreSQL activation/queue + Kubernetes replicas + semantic health | Adapter required | Apply documented status precedence and freshness; replica readiness alone is not hotness. |
| Requests/s and outcomes | Prometheus `fs2_serve_requests_total`; reconcile terminal total with `fs2_usage_facts` | Available | Bounded model/outcome labels only. |
| End-to-end latency | `fs2_serve_request_duration_seconds`; per-operation timestamps | Aggregate available | Quantiles are operational estimates; do not assign histogram quantiles to individual operations. |
| TTFT | First-output timestamp + histogram | Missing | Schema and instrumentation do not exist; return `null`/`not_instrumented`. |
| Token/s and per-operation input/output tokens | Durable operation/usage columns plus bounded histograms/counters | Missing | No token columns exist; never infer tokens from request count. |
| Cold-start total | Prometheus cold-start histogram + durable `cold_start_seconds` | Available | Label measured total; phase breakdown is not recorded. |
| Cold-start phase breakdown | Activation/download/load/restore/readiness timestamps | Missing | Extend operation/activation events before adding phase charts. |
| Errors/retries | Prometheus outcomes + durable operation error/retry fields | Available | Normalize bounded error classes; redact backend detail before display. |
| Operation detail | Existing `GET /v1/operations/{id}` plus PostgreSQL | Available by ID | Admin detail needs server-side authorization and redaction. |
| Operation history/search | PostgreSQL `fs2_operations` + events | Rows exist, list API missing | Add cursor pagination and indexed bounded filters; never expose encrypted payloads. |
| Human/service principals | Identity provider + local role projection | Missing | Token principal strings are not a directory. Decide IdP and role synchronization. |
| API keys | `fs2_tokens`, current admin token API | Partial | Issue/list/revoke exist. Add name, last use, rotation lineage, rate window; secret is one-time/no-store. |
| Quota and attributed usage | Token budgets + `fs2_usage_facts` reporting views | Available with limitations | GPU seconds are explicitly estimated; input/output/modality units are absent. |
| Durable inference queue | PostgreSQL operation queue + `fs2_serve_operations`/oldest-age gauges | Available | Reconcile aggregate gauge to durable count for the selected window. |
| Kueue quota/reservation/workloads | ResourceFlavor, ClusterQueue, LocalQueue, Workload APIs | Available via read-only adapter | Show spec nominal quota separately from status reservation; do not treat missing status as zero capacity. |
| GPU/node-pool inventory | Kubernetes Node labels/capacity/conditions | Available | Project dynamic `gpu_class`/`capacity_type`; heterogeneous pools are first-class. |
| GPU utilization/health | DCGM -> Prometheus/Grafana | Blocked on scrape | DCGM workload exists but no `DCGM_FI_*` series were observed; show unavailable. |
| HPA/KEDA | Kubernetes HPA/ScaledObject APIs | Partial | Control-plane HPA is live; KEDA has zero ScaledObjects. |
| Node autoscaler | Provider/Kubernetes autoscaler objects and events | Not verified | Do not invent a state; add a provider-agnostic adapter contract. |
| Preemption events | Node/pod events + durable operation retry/error + provider event | Not normalized | Define bounded event class and retention before KPI. |
| Audit | `fs2_audit_events`, current `GET /admin/v1/audit` | Available | Keep admin changes separate from inference operation history. |
| Grafana | Runtime-configured authenticated URL | Healthy | Enable contextual model/cluster/time link after allow-list validation. |
| Prometheus | Server-side PromQL templates or authenticated deep link | Healthy; 19 FS2 metric names observed | Browser never receives credentials or free-form unrestricted query proxy. |
| Loki | Server-side LogQL template or authenticated Grafana Explore link | Healthy | Bound time/namespace/model/operation; exclude prompt/response/key data. |
| Tempo/traces | OTel trace pipeline + trace backend | Backend absent | OTel collector health is not proof of stored/queryable traces; disable link. |
| Alertmanager | Alertmanager API/UI | Absent | PrometheusRule objects do not constitute an alert workflow; disable link. |
| OTel collector | Collector self-metrics | Healthy, no UI | Render pipeline health projection; no launch action. |
| DCGM views | Prometheus DCGM series + Grafana panel | Not ingested | Existing dashboard query is not evidence of data; enable only after a non-empty acceptance query. |
| Kueue view | Typed BFF over Kueue APIs | Controller/queue healthy; no Prometheus series | Render native table first; do not expose cluster credentials or an unverified UI. |

## Current exact control-plane API coverage

The retained FastAPI application has health/metrics, catalog/inference,
operation detail/result/cancel/acknowledge, token issue/list/revoke, audit list,
and ext-authz routes. It does not have overview projections, model observed-state
detail, operations list/search, principal inventory, capacity/queue projections,
observability discovery, or configuration plan/apply/rollback endpoints.

The recommended admin endpoints live under `/admin/api/v1`; existing
`/admin/v1` token endpoints remain compatibility routes until the typed BFF
contract and operator session are implemented.

## Sanitized live receipt

At `2026-08-30T07:47:04Z`, the allowed context reported 13 nodes: 10
preemptible B300 nodes with 38 allocatable GPUs and three CPU nodes. No regular
GPU capacity was observed. Sixteen model Deployments were desired and ready.
Kueue's one active queue had 24 nominal preemptible GPU quota and no live
workloads; nominal quota and observed reservation must be separate columns.

Grafana 13.2.0, Prometheus, Loki 3.6.12, and OTel Collector 0.158.0 passed
read-only health probes. Alertmanager and Tempo were absent. DCGM, Kueue, and
KEDA had no matching Prometheus series; KEDA also had no ScaledObjects. These
are capability states, not errors to coerce to zero.
