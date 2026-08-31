#!/bin/sh
set -eu

# Kueue 0.17.8's aggregate target ClusterRoles can be recreated after Helm
# deletes them. Kubernetes' clusterrole-aggregation-controller uses
# server-side Apply and can win the uninstall race after Helm has removed its
# ownership metadata. This destroy-only cleanup recognizes only those two
# exact, ownerless controller artifacts.

readonly timeout_seconds="${FS2_CLEANUP_TIMEOUT_SECONDS:-180}"
readonly retry_seconds="${FS2_CLEANUP_RETRY_SECONDS:-2}"

fail() {
  printf 'ERROR: Kueue teardown cleanup: %s\n' "$1" >&2
  exit 1
}

require_environment() {
  [ -n "${FS2_CLEANUP_KUBECONFIG:-}" ] || fail "missing FS2_CLEANUP_KUBECONFIG"
  [ -n "${FS2_CLEANUP_RUN_ROOT:-}" ] || fail "missing FS2_CLEANUP_RUN_ROOT"
  [ -n "${FS2_CLEANUP_KUBE_CONTEXT:-}" ] || fail "missing FS2_CLEANUP_KUBE_CONTEXT"
  [ -n "${FS2_CLEANUP_CLUSTER_ID:-}" ] || fail "missing FS2_CLEANUP_CLUSTER_ID"
  [ -n "${FS2_CLEANUP_CLUSTER_NAME:-}" ] || fail "missing FS2_CLEANUP_CLUSTER_NAME"
  [ -n "${FS2_CLEANUP_KUBE_SYSTEM_UID:-}" ] || fail "missing FS2_CLEANUP_KUBE_SYSTEM_UID"
  [ -n "${FS2_CLEANUP_RUN_ID:-}" ] || fail "missing FS2_CLEANUP_RUN_ID"
  [ -n "${FS2_CLEANUP_KUEUE_RELEASE:-}" ] || fail "missing FS2_CLEANUP_KUEUE_RELEASE"
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

  printf '%s' "$FS2_CLEANUP_CLUSTER_ID" \
    | grep -Eq '^mk8scluster-[a-z0-9]+$' \
    || fail "cluster ID is not a Nebius Managed Kubernetes ID"
  printf '%s' "$FS2_CLEANUP_RUN_ID" \
    | grep -Eq '^[a-z][a-z0-9]{5,11}$' \
    || fail "run ID is invalid"

  [ "$FS2_CLEANUP_KUBE_CONTEXT" = "$FS2_CLEANUP_CLUSTER_NAME" ] \
    || fail "context is not the exact cluster name emitted by infrastructure"
  [ "$FS2_CLEANUP_KUEUE_RELEASE" = "fs2-${FS2_CLEANUP_RUN_ID}-kueue" ] \
    || fail "Kueue release is not the exact disposable run release"

  [ -d "$FS2_CLEANUP_RUN_ROOT" ] || fail "run root is not a directory"
  [ ! -L "$FS2_CLEANUP_RUN_ROOT" ] || fail "run root must not be a symlink"
  [ "$(stat -c '%a' "$FS2_CLEANUP_RUN_ROOT")" = "700" ] \
    || fail "run root must be mode 0700"
  [ "$(stat -c '%u' "$FS2_CLEANUP_RUN_ROOT")" = "$(id -u)" ] \
    || fail "run root must be owned by the invoking user"

  [ -f "$FS2_CLEANUP_KUBECONFIG" ] || fail "kubeconfig is not a regular file"
  [ ! -L "$FS2_CLEANUP_KUBECONFIG" ] || fail "kubeconfig must not be a symlink"
  [ "$(stat -c '%a' "$FS2_CLEANUP_KUBECONFIG")" = "600" ] \
    || fail "kubeconfig must be mode 0600"
  [ "$(stat -c '%u' "$FS2_CLEANUP_KUBECONFIG")" = "$(id -u)" ] \
    || fail "kubeconfig must be owned by the invoking user"

  run_root_real="$(realpath "$FS2_CLEANUP_RUN_ROOT")"
  kubeconfig_real="$(realpath "$FS2_CLEANUP_KUBECONFIG")"
  [ "$kubeconfig_real" = "$run_root_real/kubeconfig" ] \
    || fail "kubeconfig is not the exact run-owned file"

  # `kubectl config view` is local-only. Keep the redacted document in memory
  # and expose only the selected API server to the checks below.
  config_json="$(
    kubectl \
      --kubeconfig "$FS2_CLEANUP_KUBECONFIG" \
      --context "$FS2_CLEANUP_KUBE_CONTEXT" \
      config view --minify -o json 2>/dev/null
  )" || fail "cannot read the exact kubeconfig context"
  selected_context="$(printf '%s' "$config_json" | jq -er '."current-context"')" \
    || fail "kubeconfig has no selected context"
  selected_server="$(printf '%s' "$config_json" | jq -er '.clusters[0].cluster.server')" \
    || fail "kubeconfig has no selected API server"
  [ "$selected_context" = "$FS2_CLEANUP_KUBE_CONTEXT" ] \
    || fail "kubeconfig selected a different context"
  case "$selected_server" in
    https://*"$FS2_CLEANUP_CLUSTER_ID"*) ;;
    *) fail "selected API server is not bound to the disposable cluster ID" ;;
  esac
}

kubectl_exact() {
  kubectl \
    --kubeconfig "$FS2_CLEANUP_KUBECONFIG" \
    --context "$FS2_CLEANUP_KUBE_CONTEXT" \
    --request-timeout=10s \
    "$@"
}

require_live_target_identity() {
  kube_system_uid="$(
    kubectl_exact get namespace kube-system -o jsonpath='{.metadata.uid}' 2>/dev/null
  )" || fail "cannot read kube-system from the exact disposable cluster"
  [ -n "$kube_system_uid" ] || fail "kube-system has no UID"
  [ "$kube_system_uid" = "$FS2_CLEANUP_KUBE_SYSTEM_UID" ] \
    || fail "selected cluster has a different kube-system UID"
}

kueue_components_are_absent() {
  release_secrets="$(
    kubectl_exact get secrets --all-namespaces -o json 2>/dev/null
  )" || return 1
  controllers="$(
    kubectl_exact get deployments.apps --all-namespaces -o json 2>/dev/null
  )" || return 1
  controller_pods="$(
    kubectl_exact get pods --all-namespaces -o json 2>/dev/null
  )" || return 1
  crds="$(
    kubectl_exact get customresourcedefinitions.apiextensions.k8s.io -o json 2>/dev/null
  )" || return 1

  printf '%s' "$release_secrets" | jq -e --arg release "$FS2_CLEANUP_KUEUE_RELEASE" '
    (.items | type == "array")
    and all(.items[]?;
      (
        ((.metadata.labels.owner // "") != "helm")
        or ((.metadata.labels.name // "") != $release)
      )
      and (((.metadata.name // "") | startswith("sh.helm.release.v1." + $release + ".")) | not)
    )
  ' >/dev/null 2>&1 || return 1

  printf '%s' "$controllers" | jq -e --arg release "$FS2_CLEANUP_KUEUE_RELEASE" '
    (.items | type == "array")
    and all(.items[]?;
      .metadata.name != ($release + "-controller-manager")
      and ((.metadata.labels["app.kubernetes.io/instance"] // "") != $release)
    )
  ' >/dev/null 2>&1 || return 1

  printf '%s' "$controller_pods" | jq -e --arg release "$FS2_CLEANUP_KUEUE_RELEASE" '
    (.items | type == "array")
    and all(.items[]?;
      (((.metadata.name // "") | startswith($release + "-controller-manager")) | not)
      and
      ((.metadata.labels["app.kubernetes.io/instance"] // "") != $release)
    )
  ' >/dev/null 2>&1 || return 1

  printf '%s' "$crds" | jq -e --arg release "$FS2_CLEANUP_KUEUE_RELEASE" '
    (.items | type == "array")
    and all(.items[]?;
      (((.spec.group // "") | endswith("kueue.x-k8s.io")) | not)
      and ((.metadata.labels["app.kubernetes.io/instance"] // "") != $release)
    )
  ' >/dev/null 2>&1
}

orphan_signature_is_exact() {
  role_name="$1"
  role_json="$2"

  printf '%s' "$role_json" | jq -e --arg name "$role_name" '
    .apiVersion == "rbac.authorization.k8s.io/v1"
    and .kind == "ClusterRole"
    and .metadata.name == $name
    and ((.metadata.namespace // "") == "")
    and ((.metadata.uid // "") | test("^[0-9a-fA-F-]{20,}$"))
    and ((.metadata.labels // {}) | length == 0)
    and ((.metadata.annotations // {}) | length == 0)
    and ((.metadata.ownerReferences // []) | length == 0)
    and ((.metadata.finalizers // []) | length == 0)
    and .metadata.deletionTimestamp == null
    and .aggregationRule == null
    and (.rules | type == "array")
    and (.rules | length > 0)
    and ((.metadata.managedFields // []) | length > 0)
    and all(.metadata.managedFields[]?;
      .manager == "clusterrole-aggregation-controller"
      and .operation == "Apply"
      and .apiVersion == "rbac.authorization.k8s.io/v1"
    )
  ' >/dev/null 2>&1
}

# Return 0 when already absent and 1 after deleting an exact orphan. Any
# unreadable or differently owned object fails before a delete request.
ensure_role_absent() {
  role_name="$1"
  role_json="$(
    kubectl_exact get clusterroles.rbac.authorization.k8s.io "$role_name" \
      --ignore-not-found --show-managed-fields -o json 2>/dev/null
  )" || fail "cannot inspect exact aggregate ClusterRole $role_name"
  [ -n "$role_json" ] || return 0

  orphan_signature_is_exact "$role_name" "$role_json" \
    || fail "ClusterRole $role_name does not match the exact ownerless aggregation-controller orphan signature"
  verified_uid="$(printf '%s' "$role_json" | jq -er '.metadata.uid')" \
    || fail "ClusterRole $role_name has no readable UID"

  # kubectl does not expose an object-UID delete precondition. Narrow the
  # unavoidable read/delete race by fetching and validating the complete
  # object again immediately before the exact-name delete. A changed UID is a
  # hard failure; post-delete polling also rejects any unexpected recreation.
  role_json="$(
    kubectl_exact get clusterroles.rbac.authorization.k8s.io "$role_name" \
      --ignore-not-found --show-managed-fields -o json 2>/dev/null
  )" || fail "cannot re-read exact aggregate ClusterRole $role_name"
  # An exact object that disappears between the two reads is safe, but still
  # requires the same post-change stability polling as a deletion.
  [ -n "$role_json" ] || return 1
  orphan_signature_is_exact "$role_name" "$role_json" \
    || fail "ClusterRole $role_name changed from the exact ownerless aggregation-controller orphan signature"
  current_uid="$(printf '%s' "$role_json" | jq -er '.metadata.uid')" \
    || fail "ClusterRole $role_name has no readable UID on re-read"
  [ "$current_uid" = "$verified_uid" ] \
    || fail "ClusterRole $role_name changed UID before delete"

  kubectl_exact delete clusterroles.rbac.authorization.k8s.io "$role_name" \
    --ignore-not-found --wait=true --timeout=30s >/dev/null 2>&1 \
    || fail "failed to delete exact aggregate ClusterRole $role_name"
  deleted_count="$((deleted_count + 1))"
  return 1
}

main() {
  require_environment
  require_local_target_safety
  require_live_target_identity

  admin_role="${FS2_CLEANUP_KUEUE_RELEASE}-batch-admin-role"
  user_role="${FS2_CLEANUP_KUEUE_RELEASE}-batch-user-role"
  started_at="$(date +%s)"
  deadline="$((started_at + timeout_seconds))"
  attempt=0
  deleted_count=0
  needs_post_delete_check=0
  stable_absent_samples=0

  while :; do
    attempt="$((attempt + 1))"
    if kueue_components_are_absent; then
      deleted_this_pass=0
      if ! ensure_role_absent "$admin_role"; then
        deleted_this_pass=1
      fi
      if ! ensure_role_absent "$user_role"; then
        deleted_this_pass=1
      fi

      if [ "$deleted_this_pass" -eq 0 ]; then
        if [ "$needs_post_delete_check" -eq 0 ]; then
          printf 'PASS: Kueue teardown cleanup verified exact aggregate roles absent after %s attempt(s); deleted=%s\n' \
            "$attempt" "$deleted_count"
          return 0
        fi
        stable_absent_samples="$((stable_absent_samples + 1))"
        if [ "$stable_absent_samples" -ge 2 ]; then
          printf 'PASS: Kueue teardown cleanup verified exact aggregate roles absent after %s attempt(s); deleted=%s\n' \
            "$attempt" "$deleted_count"
          return 0
        fi
      else
        stable_absent_samples=0
      fi
      needs_post_delete_check=1
    else
      stable_absent_samples=0
    fi

    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
      if [ "$needs_post_delete_check" -eq 1 ]; then
        fail "timed out after ${timeout_seconds}s waiting for aggregate-role deletion to remain stable"
      fi
      fail "timed out after ${timeout_seconds}s waiting for the Kueue release, controller, pods, and CRDs to disappear"
    fi
    sleep "$retry_seconds"
  done
}

main "$@"
