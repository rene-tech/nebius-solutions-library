#!/usr/bin/env bash
# Optional adoption of pre-existing live academic asset infrastructure.
#
# A fresh deployment does NOT need this. `terraform apply` creates the namespace,
# the tenant-private claim and the offline-validation policy directly from tfvars.
#
# Adoption exists only for the case where a claim already holds verified licensed
# bytes: recreating it would provision an empty volume and discard them. The script
# is idempotent, so re-running it is safe: an address already in state is skipped,
# and an object that does not exist live is left for Terraform to create.
#
# It never creates, mutates or deletes a Kubernetes object, and never handles
# licensed bytes.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: adopt-live-resources.sh [options]

  --apply                 perform the imports (default: print the plan)
  --chdir DIR             Terraform working directory (default: the workloads stage)
  --var-file FILE         Terraform var file, repeatable
  --backend-config VALUE  backend configuration, repeatable
  --state FILE            explicit state file
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
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
chdir="${here}/stages/workloads"
kubeconfig="${FS2_ACADEMIC_KUBECONFIG:-}"
context="${FS2_ACADEMIC_KUBE_CONTEXT:-}"
terraform_args=()

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=true; shift ;;
    --chdir) chdir="$2"; shift 2 ;;
    --var-file) terraform_args+=("-var-file=$2"); shift 2 ;;
    --backend-config) terraform_args+=("-backend-config=$2"); shift 2 ;;
    --state) terraform_args+=("-state=$2"); shift 2 ;;
    --kubeconfig) kubeconfig="$2"; shift 2 ;;
    --context) context="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

namespace="${ACADEMIC_NAMESPACE:-fs2-academic-poc}"
runtime_pvc="${ACADEMIC_RUNTIME_PVC:-academic-assets-runtime-rwx}"
legacy_namespace="${ACADEMIC_LEGACY_NAMESPACE:-fs2-models}"
legacy_pvc="${ACADEMIC_LEGACY_PVC:-cancer-immunotherapy-academic-assets-rwx-v1}"

kubectl_args=()
if [ -n "${kubeconfig}" ]; then kubectl_args+=(--kubeconfig "${kubeconfig}"); fi
if [ -n "${context}" ]; then kubectl_args+=(--context "${context}"); fi

# address | live kind | live namespace | live name | terraform import id
targets=(
  "kubernetes_namespace_v1.academic_assets[0]|namespace||${namespace}|${namespace}"
  "kubernetes_persistent_volume_claim_v1.academic_assets_runtime[0]|pvc|${namespace}|${runtime_pvc}|${namespace}/${runtime_pvc}"
  "kubernetes_persistent_volume_claim_v1.academic_assets_legacy_quarantine[0]|pvc|${legacy_namespace}|${legacy_pvc}|${legacy_namespace}/${legacy_pvc}"
  "kubernetes_network_policy_v1.academic_offline_validation[0]|networkpolicy|${namespace}|academic-offline-validation-deny-egress|${namespace}/academic-offline-validation-deny-egress"
)

already=0
missing=0
importable=0

for entry in "${targets[@]}"; do
  IFS='|' read -r address kind live_namespace live_name identifier <<<"${entry}"

  if terraform -chdir="${chdir}" state show "${address}" >/dev/null 2>&1; then
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
    terraform -chdir="${chdir}" import "${terraform_args[@]+"${terraform_args[@]}"}" \
      "${address}" "${identifier}"
  else
    printf 'terraform -chdir=%s import %s %q %q\n' \
      "${chdir}" "${terraform_args[*]+"${terraform_args[*]}"}" "${address}" "${identifier}"
  fi
done

echo "adoption summary: ${already} already managed, ${missing} to be created by apply, ${importable} adoptable"

if [ "${apply}" = true ] && [ "${importable}" -gt 0 ]; then
  cat <<'NEXT'

Now confirm adoption did not schedule a destroy of licensed storage:

  terraform -chdir=<dir> plan

The plan must show no destroy or replace for either persistent volume claim.
Both claims carry prevent_destroy, so a replacement plan fails closed instead of
silently discarding verified licensed bytes.
NEXT
fi
