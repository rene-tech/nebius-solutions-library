#!/usr/bin/env bash
set -euo pipefail

task_kubeconfig=${FS2_OBSERVABILITY_KUBECONFIG:-${KUBECONFIG:-$HOME/.kube/config}}
task_context=${FS2_OBSERVABILITY_CONTEXT:?set FS2_OBSERVABILITY_CONTEXT to the target Kubernetes context}

kctl() {
  kubectl --kubeconfig "$task_kubeconfig" --context "$task_context" "$@"
}

task_gpu_nodes=$(kctl get nodes -l 'workload.fs2.nebius/gpu=true' -o name | wc -l)
test "$task_gpu_nodes" -gt 0 || { printf 'no eligible GPU nodes found\n' >&2; exit 1; }

task_collisions=$(kctl get pods -A -o json | jq '[
  .items[] |
  select(any(.spec.containers[]?; (.image | test("(^|/)dcgm-exporter(:|@)|(^|/)dcgm(:|@)")))) |
  select(.metadata.namespace != "fs2-observability" or (.metadata.labels["app.kubernetes.io/name"] // "") != "dcgm-exporter")
] | length')
test "$task_collisions" -eq 0 || {
  printf 'a non-task DCGM/DCGM-exporter pod is already running; refusing standalone exporter\n' >&2
  exit 1
}

printf 'PASS eligible_gpu_nodes=%s dcgm_collisions=0\n' "$task_gpu_nodes"
