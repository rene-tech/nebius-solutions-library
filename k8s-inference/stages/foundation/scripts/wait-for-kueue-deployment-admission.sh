#!/bin/sh
set -eu

# Kueue's Helm release can be Ready a few seconds before the API server can
# reach its admission Service. This probe submits a Deployment to server-side
# dry-run, which executes admission without persisting or scheduling anything.

readonly probe_namespace="kserve"
readonly timeout_seconds="${FS2_GATE_TIMEOUT_SECONDS:-180}"
readonly retry_seconds="${FS2_GATE_RETRY_SECONDS:-2}"

fail() {
  printf 'ERROR: Kueue admission gate: %s\n' "$1" >&2
  exit 1
}

require_environment() {
  [ -n "${FS2_GATE_KUBECONFIG:-}" ] || fail "missing FS2_GATE_KUBECONFIG"
  [ -n "${FS2_GATE_RUN_ROOT:-}" ] || fail "missing FS2_GATE_RUN_ROOT"
  [ -n "${FS2_GATE_KUBE_CONTEXT:-}" ] || fail "missing FS2_GATE_KUBE_CONTEXT"
  [ -n "${FS2_GATE_CLUSTER_ID:-}" ] || fail "missing FS2_GATE_CLUSTER_ID"
  [ -n "${FS2_GATE_CLUSTER_NAME:-}" ] || fail "missing FS2_GATE_CLUSTER_NAME"
  [ -n "${FS2_GATE_KUBE_SYSTEM_UID:-}" ] || fail "missing FS2_GATE_KUBE_SYSTEM_UID"
  [ -n "${FS2_GATE_RUN_ID:-}" ] || fail "missing FS2_GATE_RUN_ID"
  [ -n "${FS2_GATE_KUEUE_RELEASE:-}" ] || fail "missing FS2_GATE_KUEUE_RELEASE"
}

require_local_target_safety() {
  command -v kubectl >/dev/null 2>&1 || fail "kubectl is required"
  command -v jq >/dev/null 2>&1 || fail "jq is required"
  command -v realpath >/dev/null 2>&1 || fail "realpath is required"

  printf '%s' "$timeout_seconds" | grep -Eq '^[1-9][0-9]*$' \
    || fail "timeout must be a positive integer"
  printf '%s' "$retry_seconds" | grep -Eq '^[1-9][0-9]*$' \
    || fail "retry interval must be a positive integer"
  [ "$timeout_seconds" -le 900 ] || fail "timeout exceeds 900 seconds"
  [ "$retry_seconds" -le 30 ] || fail "retry interval exceeds 30 seconds"

  printf '%s' "$FS2_GATE_CLUSTER_ID" \
    | grep -Eq '^mk8scluster-[a-z0-9]+$' \
    || fail "cluster ID is not a Nebius Managed Kubernetes ID"
  printf '%s' "$FS2_GATE_RUN_ID" \
    | grep -Eq '^[a-z][a-z0-9]{5,11}$' \
    || fail "run ID is invalid"

  [ "$FS2_GATE_KUBE_CONTEXT" = "$FS2_GATE_CLUSTER_NAME" ] \
    || fail "context is not the exact cluster name emitted by infrastructure"
  [ "$FS2_GATE_KUEUE_RELEASE" = "fs2-${FS2_GATE_RUN_ID}-kueue" ] \
    || fail "Kueue release is not the exact disposable run release"

  [ -d "$FS2_GATE_RUN_ROOT" ] || fail "run root is not a directory"
  [ ! -L "$FS2_GATE_RUN_ROOT" ] || fail "run root must not be a symlink"
  [ "$(stat -c '%a' "$FS2_GATE_RUN_ROOT")" = "700" ] \
    || fail "run root must be mode 0700"
  [ "$(stat -c '%u' "$FS2_GATE_RUN_ROOT")" = "$(id -u)" ] \
    || fail "run root must be owned by the invoking user"

  [ -f "$FS2_GATE_KUBECONFIG" ] || fail "kubeconfig is not a regular file"
  [ ! -L "$FS2_GATE_KUBECONFIG" ] || fail "kubeconfig must not be a symlink"
  [ "$(stat -c '%a' "$FS2_GATE_KUBECONFIG")" = "600" ] \
    || fail "kubeconfig must be mode 0600"
  [ "$(stat -c '%u' "$FS2_GATE_KUBECONFIG")" = "$(id -u)" ] \
    || fail "kubeconfig must be owned by the invoking user"

  run_root_real="$(realpath "$FS2_GATE_RUN_ROOT")"
  kubeconfig_real="$(realpath "$FS2_GATE_KUBECONFIG")"
  [ "$kubeconfig_real" = "$run_root_real/kubeconfig" ] \
    || fail "kubeconfig is not the exact run-owned file"

  # `kubectl config view` is local-only. Keep the redacted document in memory
  # and expose only the selected API server to the checks below.
  config_json="$(
    kubectl \
      --kubeconfig "$FS2_GATE_KUBECONFIG" \
      --context "$FS2_GATE_KUBE_CONTEXT" \
      config view --minify -o json 2>/dev/null
  )" || fail "cannot read the exact kubeconfig context"
  selected_context="$(printf '%s' "$config_json" | jq -er '."current-context"')" \
    || fail "kubeconfig has no selected context"
  selected_server="$(printf '%s' "$config_json" | jq -er '.clusters[0].cluster.server')" \
    || fail "kubeconfig has no selected API server"
  [ "$selected_context" = "$FS2_GATE_KUBE_CONTEXT" ] \
    || fail "kubeconfig selected a different context"
  case "$selected_server" in
    https://*"$FS2_GATE_CLUSTER_ID"*) ;;
    *) fail "selected API server is not bound to the disposable cluster ID" ;;
  esac
}

kubectl_exact() {
  kubectl \
    --kubeconfig "$FS2_GATE_KUBECONFIG" \
    --context "$FS2_GATE_KUBE_CONTEXT" \
    --request-timeout=10s \
    "$@"
}

webhook_matches_deployment() {
  webhook_kind="$1"
  webhook_name="$2"
  webhook_path="$3"
  # The chart prefixes these objects with the complete Helm release name.
  # FS2_GATE_KUEUE_RELEASE already ends in "-kueue"; do not append it twice.
  configuration_name="${FS2_GATE_KUEUE_RELEASE}-${webhook_kind}-webhook-configuration"
  service_name="${FS2_GATE_KUEUE_RELEASE}-webhook-service"

  webhook_json="$(
    kubectl_exact get "${webhook_kind}webhookconfiguration.admissionregistration.k8s.io" \
      "$configuration_name" -o json 2>/dev/null
  )" || return 1

  printf '%s' "$webhook_json" | jq -e \
    --arg release "$FS2_GATE_KUEUE_RELEASE" \
    --arg webhook "$webhook_name" \
    --arg service "$service_name" \
    --arg path "$webhook_path" '
      .metadata.labels["app.kubernetes.io/instance"] == $release
      and any(.webhooks[]?;
        .name == $webhook
        and .failurePolicy == "Fail"
        and .sideEffects == "None"
        and .clientConfig.service.name == $service
        and .clientConfig.service.namespace == "kueue-system"
        and .clientConfig.service.path == $path
        and ((.matchConditions // []) | length == 0)
        and ((.objectSelector // {}) | length == 0)
        and ((.namespaceSelector.matchLabels // {}) | length == 0)
        and ((.namespaceSelector.matchExpressions // []) | length == 1)
        and any(.namespaceSelector.matchExpressions[]?;
          .key == "kubernetes.io/metadata.name"
          and .operator == "NotIn"
          and ((.values // []) | length == 2)
          and ((.values // []) | index("kube-system") != null)
          and ((.values // []) | index("kueue-system") != null)
        )
        and any(.rules[]?;
          ((.apiGroups // []) | index("apps") != null)
          and ((.apiVersions // []) | index("v1") != null)
          and ((.operations // []) | index("CREATE") != null)
          and ((.resources // []) | index("deployments") != null)
          # Admissionregistration treats an omitted scope and the literal
          # wildcard as the same all-scopes match. Kueue 0.17.8 renders "*".
          and ((has("scope") | not) or .scope == "*")
        )
      )
    ' >/dev/null 2>&1
}

deployment_probe() {
  probe_name="fs2-${FS2_GATE_RUN_ID}-kueue-webhook-probe"
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${probe_name}
  namespace: ${probe_namespace}
  labels:
    app.kubernetes.io/name: ${probe_name}
    app.kubernetes.io/part-of: fs2-serve
    fs2.nebius.ai/run-id: ${FS2_GATE_RUN_ID}
spec:
  replicas: 0
  selector:
    matchLabels:
      app.kubernetes.io/name: ${probe_name}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${probe_name}
    spec:
      containers:
        - name: admission-probe
          image: registry.k8s.io/pause:3.10.1
          resources:
            requests:
              cpu: 1m
              memory: 1Mi
            limits:
              cpu: 1m
              memory: 1Mi
EOF
}

admission_ready() {
  kube_system_uid="$(
    kubectl_exact get namespace kube-system -o jsonpath='{.metadata.uid}' 2>/dev/null
  )" || return 1
  if [ -n "$kube_system_uid" ] && [ "$kube_system_uid" != "$FS2_GATE_KUBE_SYSTEM_UID" ]; then
    fail "selected cluster has a different kube-system UID"
  fi
  [ "$kube_system_uid" = "$FS2_GATE_KUBE_SYSTEM_UID" ] || return 1

  probe_namespace_name="$(
    kubectl_exact get namespace "$probe_namespace" -o jsonpath='{.metadata.name}' 2>/dev/null
  )" || return 1
  [ "$probe_namespace_name" = "$probe_namespace" ] || return 1

  can_create="$(
    kubectl_exact auth can-i create deployments.apps --namespace "$probe_namespace" 2>/dev/null
  )" || return 1
  [ "$can_create" = "yes" ] \
    || fail "caller cannot create Deployment admission requests in kserve"

  webhook_matches_deployment \
    mutating mdeployment.kb.io /mutate-apps-v1-deployment \
    || return 1
  webhook_matches_deployment \
    validating vdeployment.kb.io /validate-apps-v1-deployment \
    || return 1

  deployment_probe \
    | kubectl_exact create --dry-run=server -f - -o name >/dev/null 2>&1
}

main() {
  require_environment
  require_local_target_safety

  started_at="$(date +%s)"
  deadline="$((started_at + timeout_seconds))"
  attempt=0
  while :; do
    attempt="$((attempt + 1))"
    if admission_ready; then
      printf 'PASS: Kueue Deployment admission is ready after %s attempt(s); server dry-run persisted no object\n' "$attempt"
      return 0
    fi
    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
      fail "timed out after ${timeout_seconds}s waiting for server-side Deployment admission"
    fi
    sleep "$retry_seconds"
  done
}

main "$@"
