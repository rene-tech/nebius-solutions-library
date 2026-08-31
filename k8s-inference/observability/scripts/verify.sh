#!/usr/bin/env bash
set -euo pipefail

task_kubeconfig=${FS2_OBSERVABILITY_KUBECONFIG:-${KUBECONFIG:-$HOME/.kube/config}}
task_context=${FS2_OBSERVABILITY_CONTEXT:?set FS2_OBSERVABILITY_CONTEXT to the target Kubernetes context}
task_namespace=fs2-observability
task_tmp=$(mktemp -d /tmp/fs2-observability-verify.XXXXXX)
task_pids=()

cleanup() {
  for task_pid in "${task_pids[@]:-}"; do kill "$task_pid" 2>/dev/null || true; done
  case "$task_tmp" in
    /tmp/fs2-observability-verify.*) find "$task_tmp" -depth -delete ;;
    *) return 1 ;;
  esac
}
trap cleanup EXIT

kctl() {
  kubectl --kubeconfig "$task_kubeconfig" --context "$task_context" "$@"
}

start_forward() {
  local task_resource=$1 task_ports=$2 task_log=$3
  kctl -n "$task_namespace" port-forward "$task_resource" "$task_ports" >"$task_log" 2>&1 &
  task_pids+=("$!")
}

wait_http() {
  local task_url=$1
  for _ in $(seq 1 60); do
    curl --fail --silent --show-error "$task_url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

kctl -n "$task_namespace" wait --for=condition=Ready pod --all --timeout=5m
test "$(kctl -n "$task_namespace" get pods -o json | jq '[.items[] | select(.status.phase != "Running")] | length')" -eq 0

start_forward service/fs2-monitoring-prometheus 19090:9090 "$task_tmp/prometheus.log"
start_forward service/fs2-loki 13100:3100 "$task_tmp/loki.log"
start_forward service/fs2-monitoring-grafana 13000:80 "$task_tmp/grafana.log"
wait_http http://127.0.0.1:19090/-/ready
wait_http http://127.0.0.1:13100/ready
wait_http http://127.0.0.1:13000/api/health

prom_query() {
  curl --fail --silent --get http://127.0.0.1:19090/api/v1/query --data-urlencode "query=$1"
}

task_node_count=$(prom_query 'count(kube_node_info)' | jq -r '.data.result[0].value[1]')
task_up_count=$(prom_query 'sum(up)' | jq -r '.data.result[0].value[1]')
task_gpu_capacity=$(prom_query 'sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})' | jq -r '.data.result[0].value[1]')
task_qwen_gpu=$(prom_query 'sum(kube_pod_container_resource_requests{namespace="fs2-models",resource="nvidia_com_gpu"}) or vector(0)' | jq -r '.data.result[0].value[1]')
task_qwen_metrics_up=$(prom_query 'max(up{job="qwen3-8b-b300"}) or vector(0)' | jq -r '.data.result[0].value[1]')
task_dcgm_series=$(prom_query 'count(DCGM_FI_DEV_GPU_UTIL) or vector(0)' | jq -r '.data.result[0].value[1]')
test "${task_node_count%.*}" -ge 4
test "${task_up_count%.*}" -gt 0
test "${task_gpu_capacity%.*}" -ge 8

task_forbidden=$(curl --fail --silent --get http://127.0.0.1:19090/api/v1/series \
  --data-urlencode 'match[]={__name__=~"fs2_serve_.*|otelcol_.*"}' | \
  jq -r '.data[]? | keys[]' | sort -u | rg -i 'authorization|api_?key|token|tenant(_?id)?|user(_?id)?|subject|request(_?id)?' || true)
test -z "$task_forbidden"

task_grafana_user=$(kctl -n "$task_namespace" get secret fs2-grafana-admin -o jsonpath='{.data.admin-user}' | base64 -d)
task_grafana_password=$(kctl -n "$task_namespace" get secret fs2-grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)
umask 077
printf 'machine 127.0.0.1 login %s password %s\n' "$task_grafana_user" "$task_grafana_password" >"$task_tmp/grafana.netrc"
task_dashboards=$(curl --fail --silent --netrc-file "$task_tmp/grafana.netrc" 'http://127.0.0.1:13000/api/search?tag=fs2-serve' | jq 'length')
test "$task_dashboards" -ge 3

task_loki_ready=$(curl --fail --silent http://127.0.0.1:13100/ready)
test "$task_loki_ready" = ready

printf 'PASS nodes=%s up_targets=%s allocatable_gpus=%s qwen_requested_gpus=%s qwen_metrics_up=%s dcgm_series=%s dashboards=%s loki=%s\n' \
  "$task_node_count" "$task_up_count" "$task_gpu_capacity" "$task_qwen_gpu" \
  "$task_qwen_metrics_up" "$task_dcgm_series" "$task_dashboards" "$task_loki_ready"
