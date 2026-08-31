#!/usr/bin/env bash
set -euo pipefail

addons_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lock.env
source "$addons_dir/lock.env"
state_home=${XDG_STATE_HOME:-$HOME/.local/state}
cache_dir=${ADDONS_CACHE_DIR:-$state_home/nebius-k8s-inference/addons-cache}
kube_context=${KUBE_CONTEXT:?set KUBE_CONTEXT to the target Kubernetes context}
kubeconfig=${KUBECONFIG:-$HOME/.kube/config}
export KUBECONFIG=$kubeconfig
k=(kubectl --context "$kube_context")
h=(helm --kube-context "$kube_context")

[[ "$("${k[@]}" version -o json | jq -r .serverVersion.gitVersion)" == v1.35.* ]] || {
  printf 'refusing non-Kubernetes-1.35 target context %s\n' "$kube_context" >&2
  exit 1
}
[[ "$("${k[@]}" config current-context)" == "$kube_context" ]] || {
  printf 'refusing unexpected current context\n' >&2
  exit 1
}

"$addons_dir/scripts/fetch.sh"

for pair in \
  "$ENVOY_GATEWAY_CHART_FILE:$addons_dir/values/envoy-gateway.yaml" \
  "$CERT_MANAGER_CHART_FILE:$addons_dir/values/cert-manager.yaml" \
  "$KUEUE_CHART_FILE:$addons_dir/values/kueue.yaml" \
  "$KEDA_CHART_FILE:$addons_dir/values/keda.yaml" \
  "$KSERVE_CRD_CHART_FILE:" \
  "$KSERVE_RESOURCES_CHART_FILE:$addons_dir/values/kserve.yaml"
do
  IFS=: read -r chart values <<<"$pair"
  if [[ -n "$values" ]]; then
    helm template verify "$cache_dir/$chart" --values "$values" >/dev/null
  else
    helm template verify "$cache_dir/$chart" >/dev/null
  fi
done

"${k[@]}" apply --server-side --field-manager=fs2-upstream-addons -f "$addons_dir/manifests/namespaces.yaml"
"${k[@]}" apply --server-side --field-manager=fs2-upstream-addons -f "$cache_dir/$GATEWAY_API_FILE"

common=(--reset-values --rollback-on-failure --wait=watcher --wait-for-jobs --timeout 10m --history-max 10)
envoy_crd_dir=$(mktemp -d "${TMPDIR:-/tmp}/fs2-envoy-crds.XXXXXX")
trap 'rm -rf "$envoy_crd_dir"' EXIT
tar -xzf "$cache_dir/$ENVOY_GATEWAY_CHART_FILE" -C "$envoy_crd_dir"
"${k[@]}" apply --server-side --field-manager=fs2-upstream-addons \
  -f "$envoy_crd_dir/gateway-helm/charts/crds/crds/generated"
"${h[@]}" upgrade --install envoy-gateway "$cache_dir/$ENVOY_GATEWAY_CHART_FILE" -n envoy-gateway-system -f "$addons_dir/values/envoy-gateway.yaml" "${common[@]}"
"${k[@]}" apply --server-side --field-manager=fs2-upstream-addons -f "$addons_dir/manifests/gatewayclass.yaml"
"${h[@]}" upgrade --install cert-manager "$cache_dir/$CERT_MANAGER_CHART_FILE" -n cert-manager -f "$addons_dir/values/cert-manager.yaml" "${common[@]}"
"${h[@]}" upgrade --install keda "$cache_dir/$KEDA_CHART_FILE" -n keda -f "$addons_dir/values/keda.yaml" "${common[@]}"
"${h[@]}" upgrade --install kueue "$cache_dir/$KUEUE_CHART_FILE" -n kueue-system -f "$addons_dir/values/kueue.yaml" "${common[@]}"
"${h[@]}" upgrade --install kserve-crd "$cache_dir/$KSERVE_CRD_CHART_FILE" -n kserve --server-side true "${common[@]}"
"${h[@]}" upgrade --install kserve "$cache_dir/$KSERVE_RESOURCES_CHART_FILE" -n kserve -f "$addons_dir/values/kserve.yaml" --server-side true "${common[@]}"

for target in \
  envoy-gateway-system/envoy-gateway \
  cert-manager/cert-manager \
  cert-manager/cert-manager-webhook \
  cert-manager/cert-manager-cainjector \
  keda/keda-operator \
  keda/keda-operator-metrics-apiserver \
  keda/keda-admission-webhooks \
  kueue-system/kueue-controller-manager \
  kserve/kserve-controller-manager
do
  "${k[@]}" -n "${target%/*}" rollout status deployment/"${target#*/}" --timeout=10m
done

"$addons_dir/scripts/status.sh"
