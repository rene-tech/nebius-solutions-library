#!/usr/bin/env bash
# Optional adoption of pre-existing live academic asset infrastructure.
#
# A fresh deployment does NOT need this. `terraform apply` creates the namespace,
# the tenant-private claim and the offline-validation policy directly from tfvars.
#
# Adoption exists only for the case where a claim already holds verified licensed
# bytes, because recreating it would provision an empty volume and discard them.
#
# Backend correctness matters here: inspecting or importing into the wrong state
# is worse than doing nothing. The script therefore binds to an explicit
# TF_DATA_DIR and initialises the backend BEFORE reading state, and it passes
# backend configuration only to `init`, never to `state`/`import`, where it is not
# a valid argument.
#
# It never creates, mutates or deletes a Kubernetes object, and never handles
# licensed bytes.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: adopt-live-resources.sh [options]

  --apply                 perform the imports (default: print the plan)
  --chdir DIR             Terraform working directory (default: the workloads stage)
  --data-dir DIR          exact TF_DATA_DIR for this run (recommended; isolates state)
  --var-file FILE         Terraform var file, repeatable (passed to policy discovery
                          and import, never to state inspection)
  --backend-config VALUE  backend configuration, repeatable (passed to init only)
  --state FILE            explicit state file (passed to state and import)
  --module-prefix PREFIX  module address holding the resources (default module.academic_assets;
                          pass "" when targeting the module directly)
  --runtime-lifecycle L   retained (default) or disposable; selects which claim resource
                          the import addresses, matching the tfvars contract
  --legacy-lifecycle L    retained (default) or disposable for the quarantine claim
  --network-policy MODE   auto (default), enabled, or disabled; auto reads the
                          configured offline-validation policy from Terraform
  --no-init               skip terraform init (only when the caller already ran it)
  --kubeconfig FILE       kubeconfig used for the liveness probe
  --context NAME          kube context used for the liveness probe

Environment:
  ACADEMIC_NAMESPACE          default fs2-academic-poc
  ACADEMIC_RUNTIME_PVC        default academic-assets-runtime-rwx
  ACADEMIC_LEGACY_NAMESPACE   default fs2-models
  ACADEMIC_LEGACY_PVC         default cancer-immunotherapy-academic-assets-rwx-v1
USAGE
}

apply=false
run_init=true
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
chdir="${here}/stages/workloads"
data_dir="${TF_DATA_DIR:-}"
kubeconfig="${FS2_ACADEMIC_KUBECONFIG:-}"
context="${FS2_ACADEMIC_KUBE_CONTEXT:-}"
module_prefix="module.academic_assets"
runtime_lifecycle="retained"
legacy_lifecycle="retained"
network_policy="auto"
module_prefix_set=false
# Each Terraform subcommand accepts a different set of flags. Keeping them apart
# is what stops an invalid argument from aborting adoption midway:
#   init   -backend-config
#   state  -state
#   import -state and -var-file
backend_args=()
state_args=()
import_args=()
variable_args=()

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=true; shift ;;
    --chdir) chdir="$2"; shift 2 ;;
    --data-dir) data_dir="$2"; shift 2 ;;
    --var-file)
      import_args+=("-var-file=$2")
      variable_args+=("-var-file=$2")
      shift 2
      ;;
    --backend-config) backend_args+=("-backend-config=$2"); shift 2 ;;
    --state) state_args+=("-state=$2"); import_args+=("-state=$2"); shift 2 ;;
    --module-prefix) module_prefix="$2"; module_prefix_set=true; shift 2 ;;
    --runtime-lifecycle) runtime_lifecycle="$2"; shift 2 ;;
    --legacy-lifecycle) legacy_lifecycle="$2"; shift 2 ;;
    --network-policy) network_policy="$2"; shift 2 ;;
    --no-init) run_init=false; shift ;;
    --kubeconfig) kubeconfig="$2"; shift 2 ;;
    --context) context="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -n "${data_dir}" ]; then
  export TF_DATA_DIR="${data_dir}"
  echo "bound to TF_DATA_DIR=${TF_DATA_DIR}"
elif [ ${#backend_args[@]} -gt 0 ]; then
  echo "refusing to use backend configuration without an explicit --data-dir" >&2
  echo "an ambiguous data directory can select the wrong state" >&2
  exit 2
fi

# Initialise the backend before any state read. Reading state first can silently
# inspect a stale or empty local state and conclude the wrong thing.
if [ "${run_init}" = true ]; then
  terraform -chdir="${chdir}" init -input=false "${backend_args[@]+"${backend_args[@]}"}" >/dev/null
  echo "backend initialised"
else
  echo "skipping init at caller's request"
fi

for selected in "${runtime_lifecycle}" "${legacy_lifecycle}"; do
  case "${selected}" in
    retained | disposable) ;;
    *)
      echo "lifecycle must be retained or disposable, got: ${selected}" >&2
      exit 2
      ;;
  esac
done

case "${network_policy}" in
  auto)
    # The policy is optional in the module. Resolve its configured count before
    # importing anything so adoption cannot succeed for three resources and then
    # fail on an address that does not exist in configuration.
    network_policy=$(printf '%s\n' \
      'try(var.academic_assets.delivery.deny_egress_on_validate, false)' | \
      terraform -chdir="${chdir}" console \
        "${variable_args[@]+"${variable_args[@]}"}")
    network_policy=${network_policy//[[:space:]]/}
    ;;
  enabled | disabled) ;;
  *)
    echo "network-policy must be auto, enabled, or disabled, got: ${network_policy}" >&2
    exit 2
    ;;
esac

case "${network_policy}" in
  true) network_policy="enabled" ;;
  false) network_policy="disabled" ;;
  enabled | disabled) ;;
  *)
    echo "could not resolve the configured offline-validation policy: ${network_policy}" >&2
    exit 2
    ;;
esac

# Resources are mutually exclusive by lifecycle, so the import has to name the one
# that actually exists in configuration.
runtime_resource="kubernetes_persistent_volume_claim_v1.academic_assets_runtime_${runtime_lifecycle}[0]"
legacy_resource="kubernetes_persistent_volume_claim_v1.academic_assets_legacy_${legacy_lifecycle}[0]"
namespace_resource="kubernetes_namespace_v1.academic_assets[0]"
policy_resource="kubernetes_network_policy_v1.academic_offline_validation[0]"

if [ "${module_prefix_set}" = true ] && [ -z "${module_prefix}" ]; then
  prefix=""
else
  prefix="${module_prefix}."
fi

namespace="${ACADEMIC_NAMESPACE:-fs2-academic-poc}"
runtime_pvc="${ACADEMIC_RUNTIME_PVC:-academic-assets-runtime-rwx}"
legacy_namespace="${ACADEMIC_LEGACY_NAMESPACE:-fs2-models}"
legacy_pvc="${ACADEMIC_LEGACY_PVC:-cancer-immunotherapy-academic-assets-rwx-v1}"

kubectl_args=()
if [ -n "${kubeconfig}" ]; then kubectl_args+=(--kubeconfig "${kubeconfig}"); fi
if [ -n "${context}" ]; then kubectl_args+=(--context "${context}"); fi

# address | live kind | live namespace | live name | terraform import id
targets=(
  "${prefix}${namespace_resource}|namespace||${namespace}|${namespace}"
  "${prefix}${runtime_resource}|pvc|${namespace}|${runtime_pvc}|${namespace}/${runtime_pvc}"
  "${prefix}${legacy_resource}|pvc|${legacy_namespace}|${legacy_pvc}|${legacy_namespace}/${legacy_pvc}"
)
if [ "${network_policy}" = "enabled" ]; then
  targets+=("${prefix}${policy_resource}|networkpolicy|${namespace}|academic-offline-validation-deny-egress|${namespace}/academic-offline-validation-deny-egress")
else
  echo "skip (disabled in configuration): ${prefix}${policy_resource}"
fi

already=0
missing=0
importable=0

for entry in "${targets[@]}"; do
  IFS='|' read -r address kind live_namespace live_name identifier <<<"${entry}"

  if terraform -chdir="${chdir}" state show \
    "${state_args[@]+"${state_args[@]}"}" "${address}" >/dev/null 2>&1; then
    echo "skip (already in state): ${address}"
    already=$((already + 1))
    continue
  fi

  probe=(kubectl "${kubectl_args[@]+"${kubectl_args[@]}"}")
  if [ -n "${live_namespace}" ]; then probe+=(-n "${live_namespace}"); fi
  probe+=(get "${kind}" "${live_name}")
  if ! "${probe[@]}" >/dev/null 2>&1; then
    echo "skip (not live; terraform apply will create it): ${address}"
    missing=$((missing + 1))
    continue
  fi

  importable=$((importable + 1))
  if [ "${apply}" = true ]; then
    echo "importing ${address} <- ${identifier}"
    terraform -chdir="${chdir}" import -input=false \
      "${import_args[@]+"${import_args[@]}"}" "${address}" "${identifier}"
  else
    printf 'terraform -chdir=%s import -input=false %s %q %q\n' \
      "${chdir}" "${import_args[*]+"${import_args[*]}"}" "${address}" "${identifier}"
  fi
done

echo "adoption summary: ${already} already managed, ${missing} to be created by apply, ${importable} adoptable"

if [ "${apply}" = true ] && [ "${importable}" -gt 0 ]; then
  cat <<'NEXT'

Now confirm adoption did not schedule a destroy of licensed storage:

  terraform -chdir=<dir> plan

The plan must show no destroy or replace for either persistent volume claim.
Claims selected with the retained lifecycle carry prevent_destroy, so their
replacement fails closed instead of silently discarding verified licensed bytes.
Disposable acceptance claims intentionally have no destroy guard.
NEXT
fi
