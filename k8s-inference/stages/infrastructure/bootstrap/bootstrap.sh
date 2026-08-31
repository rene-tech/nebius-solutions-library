#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly LOCK_FILE="$SCRIPT_DIR/components.lock.json"
readonly KUEUE_CLUSTER_POLICY="$SCRIPT_DIR/../../infra/kubernetes/kueue-cluster-queues.json"
readonly KUEUE_LOCAL_QUEUES="$SCRIPT_DIR/../../catalog/kubernetes/localqueues.json"
readonly CLUSTER_ID_PREFIX="mk8scluster-"
readonly RETAINED_CLUSTER_ID="${CLUSTER_ID_PREFIX}u02y9yaj886ymys770"
readonly PROHIBITED_CLUSTER_ID="${CLUSTER_ID_PREFIX}e00rj6hs72aa1sq0te"

usage() {
  printf 'usage: %s fetch|install|verify|remove\n' "$0" >&2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    exit 1
  }
}

validate_run() {
  : "${FS2_RUN_ID:?set FS2_RUN_ID}"
  : "${FS2_RUN_ROOT:?set FS2_RUN_ROOT}"
  : "${FS2_DISPOSABLE_CLUSTER_ID:?set FS2_DISPOSABLE_CLUSTER_ID}"
  : "${KUBECONFIG:?set KUBECONFIG to the run-scoped file}"

  [[ "$FS2_RUN_ID" =~ ^[a-z][a-z0-9]{5,11}$ ]] || {
    printf 'invalid FS2_RUN_ID\n' >&2
    exit 1
  }
  case "$FS2_DISPOSABLE_CLUSTER_ID" in
    "$RETAINED_CLUSTER_ID" | "$PROHIBITED_CLUSTER_ID")
      printf 'refusing denylisted cluster ID\n' >&2
      exit 1
      ;;
  esac
  [[ "$(kubectl config current-context)" == "fs2-disposable-$FS2_RUN_ID" ]] || {
    printf 'current context is not the run-scoped disposable context\n' >&2
    exit 1
  }
}

artifact_dir() {
  : "${FS2_RUN_ROOT:?set FS2_RUN_ROOT}"
  printf '%s\n' "$FS2_RUN_ROOT/bootstrap-artifacts"
}

verify_artifact() {
  local path="$1"
  local expected="$2"
  local actual
  actual="$(sha256sum "$path" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || {
    printf 'checksum mismatch for %s: got %s expected %s\n' "$path" "$actual" "$expected" >&2
    exit 1
  }
}

fetch_components() {
  local destination name kind version source chart artifact expected path
  destination="$(artifact_dir)"
  install -d -m 0700 "$destination"

  while IFS=$'\x1f' read -r name kind version source chart artifact expected; do
    path="$destination/$artifact"
    if [[ ! -f "$path" ]]; then
      case "$kind" in
        manifest)
          curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
            "$source" --output "$path"
          ;;
        helm-repo)
          helm pull "$chart" --repo "$source" --version "$version" --destination "$destination"
          ;;
        helm-oci)
          helm pull "$source" --version "$version" --destination "$destination"
          ;;
        *)
          printf 'unsupported component kind %s for %s\n' "$kind" "$name" >&2
          exit 1
          ;;
      esac
    fi
    verify_artifact "$path" "$expected"
    chmod 0600 "$path"
    printf 'verified %s %s\n' "$name" "$expected"
  done < <(
    jq -r '.components[] | [.name, .kind, .version, .source, (.chart // ""), .artifact, .sha256] | join("\u001f")' "$LOCK_FILE"
  )
}

ensure_namespace() {
  local namespace="$1"
  kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
  kubectl label namespace "$namespace" --overwrite \
    owner=k8s-elastic-inference-platform \
    task=fs2-terraform-recipe \
    environment=fs2-disposable \
    retention=ephemeral \
    "run-id=$FS2_RUN_ID"
}

chart_path() {
  local name="$1"
  local artifact
  artifact="$(jq -er --arg name "$name" '.components[] | select(.name == $name) | .artifact' "$LOCK_FILE")"
  printf '%s/%s\n' "$(artifact_dir)" "$artifact"
}

install_chart() {
  local release="$1"
  local namespace="$2"
  local component="$3"
  shift 3
  ensure_namespace "$namespace"
  helm upgrade --install "$release" "$(chart_path "$component")" \
    --namespace "$namespace" --wait --timeout 15m "$@"
}

install_components() {
  local prefix gateway_manifest
  validate_run
  fetch_components
  prefix="fs2-$FS2_RUN_ID"
  gateway_manifest="$(artifact_dir)/$(jq -er '.components[] | select(.name == "gateway-api") | .artifact' "$LOCK_FILE")"

  kubectl apply --server-side --field-manager="$prefix-bootstrap" -f "$gateway_manifest"
  install_chart "$prefix-cert-manager" cert-manager cert-manager --set crds.enabled=true
  # Gateway API is installed above from its independently pinned manifest.
  # Do not let Envoy's chart claim a second, potentially different CRD copy.
  install_chart "$prefix-envoy" envoy-gateway-system envoy-gateway --skip-crds
  install_chart "$prefix-kueue" kueue-system kueue
  ensure_namespace fs2-models
  kubectl apply -f "$KUEUE_CLUSTER_POLICY"
  kubectl apply -f "$KUEUE_LOCAL_QUEUES"
  install_chart "$prefix-kserve-crd" kserve kserve-crd
  install_chart "$prefix-kserve" kserve kserve-resources \
    --set kserve.controller.deploymentMode=Standard
  install_chart "$prefix-monitoring" fs2-observability kube-prometheus-stack

  if [[ -n "${FS2_DCGM_IMAGE_DIGEST:-}" ]]; then
    [[ "$FS2_DCGM_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || {
      printf 'FS2_DCGM_IMAGE_DIGEST must be a sha256 digest\n' >&2
      exit 1
    }
    install_chart "$prefix-dcgm" fs2-observability dcgm-exporter \
      --values "$SCRIPT_DIR/values/dcgm-exporter.yaml" \
      --set "image.digest=$FS2_DCGM_IMAGE_DIGEST" \
      --set "serviceMonitor.additionalLabels.release=$prefix-monitoring"
  else
    printf 'DCGM chart not installed: immutable NVCR image digest was not supplied\n' >&2
  fi

  verify_components
}

verify_components() {
  local receipt
  validate_run
  kubectl get customresourcedefinition gateways.gateway.networking.k8s.io >/dev/null
  kubectl get customresourcedefinition workloads.kueue.x-k8s.io >/dev/null
  kubectl get customresourcedefinition inferenceservices.serving.kserve.io >/dev/null
  kubectl get clusterqueue fs2-b300-async >/dev/null
  kubectl --namespace fs2-models get localqueue fs2-models-async >/dev/null
  for namespace in cert-manager envoy-gateway-system kueue-system kserve fs2-observability; do
    kubectl wait --for=condition=Ready pod --all --namespace "$namespace" --timeout=15m
  done
  kubectl get nodes -l capacity.fs2.nebius/preset=b300-1x -o json \
    | jq -e 'any(.items[]; ((.status.allocatable["nvidia.com/gpu"] // "0") | tonumber) >= 1)' >/dev/null

  receipt="$FS2_RUN_ROOT/bootstrap.receipt.json"
  jq -n \
    --arg run_id "$FS2_RUN_ID" \
    --arg cluster_id "$FS2_DISPOSABLE_CLUSTER_ID" \
    --arg context "$(kubectl config current-context)" \
    --arg lock_sha256 "$(sha256sum "$LOCK_FILE" | awk '{print $1}')" \
    '{schema_version:1, labels:{owner:"k8s-elastic-inference-platform",task:"fs2-terraform-recipe","managed-by":"helm",environment:"fs2-disposable",retention:"ephemeral","run-id":$run_id}, cluster_id:$cluster_id, context:$context, component_lock_sha256:$lock_sha256, gateway_api:{crd_source:"pinned-manifest",envoy_chart_skip_crds:true}}' \
    >"$receipt"
  chmod 0600 "$receipt"
  printf 'platform bootstrap verified; receipt=%s\n' "$receipt"
}

remove_components() {
  local prefix gateway_manifest
  validate_run
  prefix="fs2-$FS2_RUN_ID"
  gateway_manifest="$(artifact_dir)/$(jq -er '.components[] | select(.name == "gateway-api") | .artifact' "$LOCK_FILE")"

  helm uninstall "$prefix-dcgm" --namespace fs2-observability --ignore-not-found --wait
  helm uninstall "$prefix-monitoring" --namespace fs2-observability --ignore-not-found --wait
  # kube-prometheus-stack retains its CRDs by policy. This script can remove
  # them because validate_run proves the whole cluster is disposable.
  kubectl delete customresourcedefinition \
    alertmanagerconfigs.monitoring.coreos.com \
    alertmanagers.monitoring.coreos.com \
    podmonitors.monitoring.coreos.com \
    probes.monitoring.coreos.com \
    prometheusagents.monitoring.coreos.com \
    prometheuses.monitoring.coreos.com \
    prometheusrules.monitoring.coreos.com \
    scrapeconfigs.monitoring.coreos.com \
    servicemonitors.monitoring.coreos.com \
    thanosrulers.monitoring.coreos.com \
    --ignore-not-found --wait=true
  helm uninstall "$prefix-kserve" --namespace kserve --ignore-not-found --wait
  helm uninstall "$prefix-kserve-crd" --namespace kserve --ignore-not-found --wait
  kubectl delete -f "$KUEUE_LOCAL_QUEUES" --ignore-not-found --wait=true
  kubectl delete -f "$KUEUE_CLUSTER_POLICY" --ignore-not-found --wait=true
  # These two aggregate roles are rendered without Helm ownership metadata.
  # Delete only their run-scoped names before waiting for Helm's uninstall.
  kubectl delete clusterrole \
    "$prefix-kueue-batch-admin-role" \
    "$prefix-kueue-batch-user-role" \
    --ignore-not-found --wait=true
  helm uninstall "$prefix-kueue" --namespace kueue-system --ignore-not-found --wait
  helm uninstall "$prefix-envoy" --namespace envoy-gateway-system --ignore-not-found --wait
  # The Envoy cert-generation hook leaves run-scoped cluster RBAC behind.
  kubectl delete clusterrole \
    "$prefix-envoy-gateway-helm-certgen:envoy-gateway-system" \
    --ignore-not-found --wait=true
  kubectl delete clusterrolebinding \
    "$prefix-envoy-gateway-helm-certgen:envoy-gateway-system" \
    --ignore-not-found --wait=true
  # Envoy's extension CRDs are templates rather than the skipped Gateway API
  # CRD bundle, so Helm keeps them after uninstall.
  kubectl delete customresourcedefinition \
    tcproutes.gateway.networking.k8s.io \
    udproutes.gateway.networking.k8s.io \
    xbackendtrafficpolicies.gateway.networking.x-k8s.io \
    xmeshes.gateway.networking.x-k8s.io \
    --ignore-not-found --wait=true
  helm uninstall "$prefix-cert-manager" --namespace cert-manager --ignore-not-found --wait
  # cert-manager also marks its CRDs as retained. Exact deletion is safe only
  # after the disposable-cluster identity guard above has passed.
  kubectl delete customresourcedefinition \
    challenges.acme.cert-manager.io \
    orders.acme.cert-manager.io \
    certificaterequests.cert-manager.io \
    certificates.cert-manager.io \
    clusterissuers.cert-manager.io \
    issuers.cert-manager.io \
    --ignore-not-found --wait=true
  kubectl delete -f "$gateway_manifest" --ignore-not-found --wait=true

  for namespace in fs2-models fs2-observability kserve kueue-system envoy-gateway-system cert-manager; do
    kubectl delete namespace "$namespace" --ignore-not-found --wait=true --timeout=15m
  done
  helm list --all-namespaces -o json \
    | jq -e --arg prefix "$prefix-" '[.[] | select(.name | startswith($prefix))] | length == 0' >/dev/null
  printf 'platform bootstrap resources removed for %s\n' "$FS2_RUN_ID"
}

main() {
  require_command curl
  require_command helm
  require_command jq
  require_command kubectl
  require_command sha256sum
  [[ $# -eq 1 ]] || {
    usage
    exit 2
  }
  case "$1" in
    fetch) fetch_components ;;
    install) install_components ;;
    verify) verify_components ;;
    remove) remove_components ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
