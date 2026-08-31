#!/usr/bin/env bash
set -euo pipefail

kube_context=${KUBE_CONTEXT:?set KUBE_CONTEXT to the target Kubernetes context}
kubeconfig=${KUBECONFIG:-$HOME/.kube/config}
export KUBECONFIG=$kubeconfig
k=(kubectl --context "$kube_context")
h=(helm --kube-context "$kube_context")

"${k[@]}" version
"${h[@]}" list -A
"${k[@]}" get ns envoy-gateway-system cert-manager keda kueue-system kserve --show-labels
"${k[@]}" get deployment -n envoy-gateway-system
"${k[@]}" get deployment -n cert-manager
"${k[@]}" get deployment -n keda
"${k[@]}" get deployment -n kueue-system
"${k[@]}" get deployment -n kserve
"${k[@]}" get gatewayclass envoy
"${k[@]}" get validatingwebhookconfigurations,mutatingwebhookconfigurations | grep -E 'NAME|envoy|cert-manager|keda|kueue|kserve'
"${k[@]}" get apiservice v1beta1.external.metrics.k8s.io
"${k[@]}" get pods -A -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,IMAGE:.spec.containers[*].image,NODE:.spec.nodeName | grep -E 'NAMESPACE|envoy-gateway|cert-manager|keda|kueue|kserve'
"${k[@]}" get storageclass -o custom-columns=NAME:.metadata.name,PROVISIONER:.provisioner,RECLAIM:.reclaimPolicy,DEFAULT:.metadata.annotations.storageclass\\.kubernetes\\.io/is-default-class,UID:.metadata.uid
