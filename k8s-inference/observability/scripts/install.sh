#!/usr/bin/env bash
set -euo pipefail

if [[ "${FS2_EXTERNAL_CAPSULE_ACTIVE:-}" != "1" ]]; then
  printf 'observability installation must start through the external capsule shell-entry command\n' >&2
  exit 1
fi
case "${FS2_CAPSULE_SOURCE_ROOT:-}" in /*) ;; *) printf 'capsule read-only source root is absent\n' >&2; exit 1 ;; esac
case "${FS2_CAPSULE_TOOL_DIR:-}" in /*) ;; *) printf 'capsule read-only tool directory is absent\n' >&2; exit 1 ;; esac
export PATH="$FS2_CAPSULE_TOOL_DIR"
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP
task_root="$FS2_CAPSULE_SOURCE_ROOT/observability"
task_lock="$task_root/versions.lock.yaml"
task_security_dir="$FS2_CAPSULE_SOURCE_ROOT/security"
task_kubeconfig=${FS2_OBSERVABILITY_KUBECONFIG:-${KUBECONFIG:-$HOME/.kube/config}}
task_context=${FS2_OBSERVABILITY_CONTEXT:?set FS2_OBSERVABILITY_CONTEXT to the target Kubernetes context}
task_namespace=fs2-observability
task_owner=fs2-serve-lean-observability-live
task_tmp=$(mktemp -d /tmp/fs2-observability-install.XXXXXX)
gate_bootstrap=${FS2_IMAGE_GATE_BOOTSTRAP:?set externally installed capsule bootstrap}
external_trust=${FS2_EXTERNAL_CAPSULE_TRUST:?set external capsule trust}
gate_toolchain=${FS2_IMAGE_GATE_TOOLCHAIN:?set execution toolchain lock}
registry_auth_receipt=${FS2_REGISTRY_AUTH_RECEIPT:?set signed short-lived pull receipt}
reviewed_helm=("$gate_bootstrap" exec-tool --external-trust "$external_trust" --toolchain "$gate_toolchain" --source-root "$task_security_dir/.." --tool helm --)
reviewed_kubectl=("$gate_bootstrap" exec-tool --external-trust "$external_trust" --toolchain "$gate_toolchain" --source-root "$task_security_dir/.." --tool kubectl --)
task_image_gate=(
  --post-renderer "$gate_bootstrap"
  --post-renderer-args=python-entry
  --post-renderer-args=--external-trust
  --post-renderer-args "$external_trust"
  --post-renderer-args=--toolchain
  --post-renderer-args "$gate_toolchain"
  --post-renderer-args=--source-root
  --post-renderer-args "$task_security_dir/.."
  --post-renderer-args=--entry
  --post-renderer-args security/helm_image_postrenderer.py
  --post-renderer-args=--
  --post-renderer-args=--lock
  --post-renderer-args "$task_security_dir/third-party-images.lock.json"
  --post-renderer-args=--first-party-lock
  --post-renderer-args "$task_security_dir/first-party-images.lock.json"
  --post-renderer-args=--trust
  --post-renderer-args "$task_security_dir/image-attestation-trust.json"
  --post-renderer-args=--authorization
  --post-renderer-args "${FS2_IMAGE_GATE_AUTHORIZATION:-$task_security_dir/image-materials-authorization.json}"
  --post-renderer-args=--toolchain
  --post-renderer-args "$gate_toolchain"
  --post-renderer-args=--registry-auth-receipt
  --post-renderer-args "$registry_auth_receipt"
)

cleanup() {
  case "$task_tmp" in
    /tmp/fs2-observability-install.*) find "$task_tmp" -depth -delete ;;
    *) return 1 ;;
  esac
}
trap cleanup EXIT

for task_bin in yq sha256sum openssl; do
  command -v "$task_bin" >/dev/null || { printf 'missing required command: %s\n' "$task_bin" >&2; exit 1; }
done
test -r "$task_kubeconfig" || { printf 'kubeconfig is not readable: %s\n' "$task_kubeconfig" >&2; exit 1; }

kctl() {
  "${reviewed_kubectl[@]}" --kubeconfig "$task_kubeconfig" --context "$task_context" "$@"
}

hctl() {
  "${reviewed_helm[@]}" --kubeconfig "$task_kubeconfig" --kube-context "$task_context" "$@"
}

pull_chart() {
  local task_key=$1
  local task_repo task_name task_version task_sha task_archive
  task_repo=$(yq -r ".charts.${task_key}.repository" "$task_lock")
  task_name=$(yq -r ".charts.${task_key}.name" "$task_lock")
  task_version=$(yq -r ".charts.${task_key}.version" "$task_lock")
  task_sha=$(yq -r ".charts.${task_key}.sha256" "$task_lock")
  "${reviewed_helm[@]}" pull --repo "$task_repo" "$task_name" --version "$task_version" --destination "$task_tmp"
  task_archive="$task_tmp/${task_name}-${task_version}.tgz"
  test "$(sha256sum "$task_archive" | awk '{print $1}')" = "$task_sha" || {
    printf 'chart digest mismatch: %s\n' "$task_key" >&2
    exit 1
  }
  printf '%s\n' "$task_archive"
}

if kctl get namespace "$task_namespace" >/dev/null 2>&1; then
  task_existing_owner=$(kctl get namespace "$task_namespace" -o jsonpath='{.metadata.labels.observability\.fs2\.nebius/owner}')
  test "$task_existing_owner" = "$task_owner" || {
    printf 'refusing to adopt namespace %s owned by %s\n' "$task_namespace" "${task_existing_owner:-unlabeled}" >&2
    exit 1
  }
else
  kctl create namespace "$task_namespace"
fi
kctl label namespace "$task_namespace" \
  observability.fs2.nebius/owner="$task_owner" \
  app.kubernetes.io/part-of=fs2-serve --overwrite >/dev/null

if ! kctl -n "$task_namespace" get secret fs2-grafana-admin >/dev/null 2>&1; then
  umask 077
  printf 'admin' >"$task_tmp/admin-user"
  openssl rand -base64 36 | tr -d '\n' >"$task_tmp/admin-password"
  kctl -n "$task_namespace" create secret generic fs2-grafana-admin \
    --from-file=admin-user="$task_tmp/admin-user" \
    --from-file=admin-password="$task_tmp/admin-password" \
    --dry-run=client -o yaml | kctl apply -f - >/dev/null
  kctl -n "$task_namespace" label secret fs2-grafana-admin \
    observability.fs2.nebius/owner="$task_owner" --overwrite >/dev/null
fi

task_kps=$(pull_chart kubePrometheusStack)
task_loki=$(pull_chart loki)
task_otel=$(pull_chart openTelemetryCollector)

hctl upgrade --install fs2-monitoring "$task_kps" \
  --namespace "$task_namespace" \
  --values "$task_root/values/kube-prometheus-stack.yaml" \
  --rollback-on-failure --wait --timeout 15m --history-max 5 "${task_image_gate[@]}"

hctl upgrade --install fs2-loki "$task_loki" \
  --namespace "$task_namespace" \
  --values "$task_root/values/loki.yaml" \
  --rollback-on-failure --wait --timeout 10m --history-max 5 "${task_image_gate[@]}"

hctl upgrade --install fs2-otel-gateway "$task_otel" \
  --namespace "$task_namespace" \
  --values "$task_root/values/otel-gateway.yaml" \
  --rollback-on-failure --wait --timeout 10m --history-max 5 "${task_image_gate[@]}"

hctl upgrade --install fs2-otel-node "$task_otel" \
  --namespace "$task_namespace" \
  --values "$task_root/values/otel-node.yaml" \
  --rollback-on-failure --wait --timeout 10m --history-max 5 "${task_image_gate[@]}"

kctl apply --server-side --field-manager=fs2-observability \
  -f "$task_root/manifests/observability.yaml"

kctl -n "$task_namespace" wait --for=condition=Ready pod --all --timeout=10m
kctl -n "$task_namespace" get pods -o wide
