#!/usr/bin/env bash
set -euo pipefail

kube_context=${KUBE_CONTEXT:?set KUBE_CONTEXT to the target Kubernetes context}
kubeconfig=${KUBECONFIG:-$HOME/.kube/config}
export KUBECONFIG=$kubeconfig

for target in \
  kserve:kserve \
  kserve-crd:kserve \
  kueue:kueue-system \
  keda:keda \
  envoy-gateway:envoy-gateway-system \
  cert-manager:cert-manager
do
  release=${target%:*}
  namespace=${target#*:}
  helm --kube-context "$kube_context" history "$release" -n "$namespace"
  printf 'rollback: helm --kube-context %q rollback %q <REVISION> -n %q --wait --timeout 10m\n' "$kube_context" "$release" "$namespace"
done

printf '%s\n' 'First-install rollback is helm uninstall in reverse order. Retain CRDs until all custom resources are removed.'
