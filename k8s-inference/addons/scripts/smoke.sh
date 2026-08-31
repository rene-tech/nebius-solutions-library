#!/usr/bin/env bash
set -euo pipefail

addons_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
kube_context=${KUBE_CONTEXT:?set KUBE_CONTEXT to the target Kubernetes context}
kubeconfig=${KUBECONFIG:-$HOME/.kube/config}
export KUBECONFIG=$kubeconfig
k=(kubectl --context "$kube_context")
namespace=fs2-addon-smoke

cleanup() {
  [[ "${KEEP_SMOKE:-0}" == 1 ]] && return
  set +e
  "${k[@]}" delete clusterqueue/fs2-addon-smoke resourceflavor/fs2-addon-smoke --ignore-not-found --wait=true
  "${k[@]}" delete namespace "$namespace" --ignore-not-found --wait=true
}
trap cleanup EXIT
cleanup

"${k[@]}" apply -f "$addons_dir/smoke/resources.yaml"
"${k[@]}" -n "$namespace" rollout status deployment/gateway-backend --timeout=5m
"${k[@]}" -n "$namespace" wait --for=condition=Ready pod/smoke-client --timeout=5m
"${k[@]}" -n "$namespace" wait --for=condition=Ready certificate/smoke-certificate --timeout=3m
"${k[@]}" -n "$namespace" wait --for=condition=Programmed gateway/fs2-addon-smoke --timeout=5m

gateway_address=$("${k[@]}" -n "$namespace" get gateway/fs2-addon-smoke -o jsonpath='{.status.addresses[0].value}')
[[ -n "$gateway_address" ]]
"${k[@]}" -n "$namespace" exec smoke-client -- curl --fail --silent --show-error "http://$gateway_address:8080/hostname"

"${k[@]}" -n "$namespace" wait --for=condition=Ready scaledobject/keda-smoke --timeout=5m
"${k[@]}" -n "$namespace" get hpa/keda-hpa-keda-smoke
"${k[@]}" -n "$namespace" wait --for=condition=complete job/kueue-smoke --timeout=5m
workload=$("${k[@]}" -n "$namespace" get workload -o jsonpath='{.items[0].metadata.name}')
"${k[@]}" -n "$namespace" wait --for=condition=Admitted workload/"$workload" --timeout=2m
"${k[@]}" -n "$namespace" logs job/kueue-smoke
"${k[@]}" -n "$namespace" wait --for=condition=Ready inferenceservice/kserve-smoke --timeout=8m

"${k[@]}" -n "$namespace" get gateway,httproute,certificate,scaledobject,hpa,job,workload,inferenceservice
if "${k[@]}" get service -A -l fs2.nebius.ai/smoke=true -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' | grep -q .; then
  printf 'smoke created an unexpected LoadBalancer service\n' >&2
  exit 1
fi
printf 'all upstream add-on reconciliation smokes: PASS\n'
