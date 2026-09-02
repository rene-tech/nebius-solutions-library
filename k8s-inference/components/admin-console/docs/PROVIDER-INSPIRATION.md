# Provider-console patterns used by FS2 Serve

This console borrows information-architecture patterns from current provider
documentation, not source code, trademarks, artwork, or private design tokens.
The links below were reviewed on 2026-08-30 and are implementation evidence,
not claims of visual equivalence.

## Adopted patterns

| Provider pattern | FS2 Serve decision |
|---|---|
| Nebius organizes resources by project and region, exposes IAM beneath Administration, and gives Kubernetes cluster and node-group dashboards time and refresh controls. | Keep project, cluster, region, time range, timezone, and freshness in one persistent context bar. Put users, roles, keys, and audit under Access rather than mixing identity with model configuration. |
| Baseten's per-model metrics distinguish request volume by outcome, response time, ready/not-ready replicas, restarts, CPU/GPU use, concurrency, and asynchronous queue state. | Make Models the primary resource table; give every model a detail page with runtime state, latency/throughput, replica placement, queue/cold-start, snapshot/cache, semantic health, and error evidence. Never derive `hot` from replica count alone. |
| Baseten distinguishes personal and team credentials, offers permission/model/environment scope, and discloses a new key once. | Model human and service principals separately. Create scoped, named API keys with role, tenant, model, operation, budget, concurrency and rate-window policy; disclose the secret once and retain only verifier material and non-secret metadata. |
| Together uses project-scoped keys and key identifiers to segment usage by workload, model, and time. | Attribute every admitted operation and durable usage fact to tenant, principal and key ID. Provide per-key operations, reported tokens/modalities, estimated GPU-seconds, last accepted use, limits, rotation lineage, and revocation state. |
| Fireworks exposes dedicated deployments and autoscaling controls for request rate and concurrency. | Keep deployment/hotness, min/max replicas, concurrency, queue, cooldown, placement, and capacity type visible together. Normal tfvars deployments become the authoritative baseline; the reviewed Terraform plan/reconcile workflow remains available from the console. |

## Deliberate differences

- FS2 serves heterogeneous OpenAI-compatible, BioNeMo, medical-imaging, native,
  and MCP workloads. Token totals therefore remain unavailable unless a runtime
  explicitly reports them; imaging and molecular modalities use typed units.
- Estimated GPU-seconds are labeled as estimates derived from admitted GPU
  allocation. DCGM utilization and memory are separate measured signals.
- Prometheus, Loki, Kubernetes, PostgreSQL, and cloud credentials never enter
  the browser. Grafana is the authenticated external pane for the installed
  observability stack; raw Prometheus and Loki stay private.
- The browser can produce a validated diff, reconcile receipt, rollback target,
  and Terraform handoff. Direct tfvars changes need no receipt and are durably
  adopted as the Terraform baseline. The browser is not a second cloud control
  plane and does not patch Terraform-owned resources directly.
- Unknown, stale, unsupported, and unavailable states remain distinct from
  numeric zero or a healthy state.

## Official sources

- [Nebius IAM and resource hierarchy](https://docs.nebius.com/iam/overview)
- [Nebius Kubernetes monitoring](https://docs.nebius.com/kubernetes/monitoring)
- [Nebius logs in Grafana](https://docs.nebius.com/observability/logs/grafana)
- [Baseten model metrics](https://docs.baseten.co/observability/metrics)
- [Baseten API keys](https://docs.baseten.co/organization/api-keys)
- [Baseten deployment management API](https://docs.baseten.co/reference/management-api/overview)
- [Together API-key attribution](https://docs.together.ai/docs/api-keys-authentication)
- [Together usage analytics](https://docs.together.ai/docs/billing-usage-limits)
- [Fireworks on-demand deployments](https://docs.fireworks.ai/getting-started/ondemand-quickstart)
- [Grafana subpath configuration](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/)
- [Gateway API path rewrite](https://gateway-api.sigs.k8s.io/guides/user-guides/http-redirect-rewrite/)
- [Gateway API ReferenceGrant](https://gateway-api.sigs.k8s.io/reference/api-types/referencegrant/)
