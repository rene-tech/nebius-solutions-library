# Kubernetes inference observability

> Import disposition: optional operational package. It is not wired into the
> canonical release or Terraform apply path and must be installed explicitly.

This directory owns the `fs2-observability` namespace on the selected cluster.
It installs a standard CPU-first observability path from official Helm charts:
kube-prometheus-stack, Grafana, Loki, and two OpenTelemetry Collector modes.

## Pinned release

`versions.lock.yaml` pins each chart version and downloaded chart-archive
SHA-256. The first lean install intentionally accepts the official image tags
rendered by those pinned charts; registry digest resolution is deferred and no
post-renderer or deployment lock gate is present.

| Release | Chart | Mode |
| --- | --- | --- |
| `fs2-monitoring` | `kube-prometheus-stack` 88.5.4 | Prometheus, Grafana, kube-state-metrics, node exporter |
| `fs2-loki` | `loki` 7.3.0 | one single-binary replica, filesystem TSDB |
| `fs2-otel-gateway` | `opentelemetry-collector` 0.171.0 | two Collector Contrib 0.159.0 replicas |
| `fs2-otel-node` | `opentelemetry-collector` 0.171.0 | one Collector K8s 0.159.0 Pod per node |

The gateway accepts OTLP/gRPC and OTLP/HTTP, exposes received metrics and
span-derived metrics for Prometheus, and exports logs to Loki. The node mode
tails only `fs2-*` namespace container logs, enriches them with bounded
Kubernetes identity, removes sensitive identity/header attributes, and sends
them to the gateway. Node checkpoint persistence is disabled so the Collector
can remain non-root without a writable host path; a Collector restart begins
at the end of each current log file.

## Install and verify

Run from this repository worktree:

```bash
bash k8s-inference/observability/scripts/test.sh
bash k8s-inference/observability/scripts/install.sh
bash k8s-inference/observability/scripts/verify.sh
bash k8s-inference/observability/scripts/check-dcgm-collision.sh
```

The installer refuses to adopt a namespace not labeled for this task. It
generates the Grafana admin credential directly into a Kubernetes Secret and
never prints it. All Helm operations use bounded waits and rollback on failure.

## Access and useful queries

All Services are cluster-local. Select a kubeconfig and context explicitly:

```bash
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export FS2_OBSERVABILITY_CONTEXT="$(kubectl --kubeconfig "$KUBECONFIG" config current-context)"
kubectl --context "$FS2_OBSERVABILITY_CONTEXT" -n fs2-observability \
  port-forward service/fs2-monitoring-grafana 3000:80
kubectl --context "$FS2_OBSERVABILITY_CONTEXT" -n fs2-observability \
  port-forward service/fs2-monitoring-prometheus 9090:9090
kubectl --context "$FS2_OBSERVABILITY_CONTEXT" -n fs2-observability \
  port-forward service/fs2-loki 3100:3100
```

Retrieve the Grafana username/password from Secret `fs2-grafana-admin` only in
the consuming operator's shell. Do not copy either value into logs, tickets,
or Git.

Three task dashboards are provisioned with UIDs `fs2-platform`,
`fs2-inference`, and `fs2-gpu`. Representative PromQL/LogQL queries are:

```promql
sum(kube_node_status_condition{condition="Ready",status="true"})
sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})
sum by (namespace,pod,node) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
)
sum by (model,outcome) (rate(fs2_serve_requests_total[5m]))
histogram_quantile(0.95,
  sum by (le,model) (rate(fs2_serve_request_duration_seconds_bucket[15m])))
absent(fs2_serve_operations) or vector(0)
absent(DCGM_FI_DEV_GPU_UTIL) or vector(0)
```

```logql
{k8s_namespace_name="fs2-models",k8s_pod_name=~"qwen3-8b-b300.*"}
```

The explicit `absent(...)` panels and alerts distinguish missing telemetry from
a measured zero. Metric relabeling and Collector processors remove tenant,
user, token, API-key, authorization, subject, and request identifiers.

## Validation contract

`test.sh` verifies chart checksums, lints and renders every chart with the
checked-in values, rejects unpinned `latest` images, validates dashboards, and
checks Prometheus rules. It contacts chart registries but no Kubernetes
cluster.

`verify.sh` is the live, read-only acceptance check. It requires every
observability Pod to be running, queries Prometheus and Loki through temporary
local port forwards, confirms that node/GPU allocation metrics and dashboards
exist, and rejects sensitive identity or credential label names. Counts are
derived from the selected cluster; no project, cluster, node, Pod, PVC, or
resource UID is embedded in this repository.

## DCGM boundary and deferred work

Nebius Managed Kubernetes may already provide DCGM components. Run
`check-dcgm-collision.sh` before enabling the optional standalone exporter; it
fails when another DCGM workload is scheduled on the selected GPU nodes. This
package does not install a GPU driver, toolkit, device plugin, GPU Operator, or
hostengine.

Without an explicitly enabled and verified DCGM exporter, GPU accounting is
an allocation estimate from Kubernetes requests. It must not be presented as
device utilization or precise billing.

## Retention and rollback

The four Helm releases, namespace, generated Grafana Secret, dashboards,
rules, and three PVCs are intentionally retained. No temporary Kubernetes
resource remains. Before a rollback, inspect `helm history` and capture the
current revision. Helm's normal rollback path is, for example:

```bash
helm --kubeconfig "$KUBECONFIG" \
  --kube-context "$FS2_OBSERVABILITY_CONTEXT" rollback fs2-otel-gateway 3 \
  --namespace fs2-observability --wait --timeout 10m
```

Do not uninstall the namespace or releases as a rollback: the PVC reclaim
policy can make that destructive. Re-run `verify.sh` and
`check-dcgm-collision.sh` after any change.
