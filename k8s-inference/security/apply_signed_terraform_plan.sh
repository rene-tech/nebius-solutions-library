#!/usr/bin/env bash
set -euo pipefail

if [[ "${FS2_EXTERNAL_CAPSULE_ACTIVE:-}" != "1" ]]; then
  printf 'saved-plan compatibility dispatcher must start through external capsule shell-entry\n' >&2
  exit 1
fi
case "${FS2_CAPSULE_SOURCE_ROOT:-}" in /*) ;; *) printf 'capsule read-only source root is absent\n' >&2; exit 1 ;; esac
case "${FS2_CAPSULE_TOOL_DIR:-}" in /*) ;; *) printf 'capsule read-only tool directory is absent\n' >&2; exit 1 ;; esac
export PATH="$FS2_CAPSULE_TOOL_DIR"
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP

usage() {
  printf 'usage: %s TERRAFORM_ROOT SAVED_PLAN PLAN_RECEIPT RELEASE_CLOSURE REGISTRY_RECEIPT REFRESH_REGISTRATION\n' "$0" >&2
  printf 'requires FS2_IMAGE_GATE_BOOTSTRAP, FS2_EXTERNAL_CAPSULE_TRUST, and FS2_IMAGE_GATE_TOOLCHAIN\n' >&2
}

if [[ $# -ne 6 ]]; then
  usage
  exit 2
fi

root=$1
saved_plan=$2
plan_receipt=$3
release_closure=$4
registry_receipt=$5
refresh_registration=$6
case "$root" in
  .|stages/infrastructure|stages/foundation|stages/workloads) ;;
  *) printf 'unsupported production Terraform root: %s\n' "$root" >&2; exit 1 ;;
esac

source_root=$FS2_CAPSULE_SOURCE_ROOT
case "$saved_plan" in /*) ;; *) saved_plan="$PWD/$saved_plan" ;; esac
case "$release_closure" in /*) ;; *) release_closure="$PWD/$release_closure" ;; esac
case "$plan_receipt" in /*) ;; *) plan_receipt="$PWD/$plan_receipt" ;; esac
case "$registry_receipt" in /*) ;; *) registry_receipt="$PWD/$registry_receipt" ;; esac
case "$refresh_registration" in /*) ;; *) refresh_registration="$PWD/$refresh_registration" ;; esac

case "${FS2_IMAGE_GATE_BOOTSTRAP:-}" in /*) ;; *) printf 'FS2_IMAGE_GATE_BOOTSTRAP must be absolute\n' >&2; exit 1 ;; esac
case "${FS2_EXTERNAL_CAPSULE_TRUST:-}" in /*) ;; *) printf 'FS2_EXTERNAL_CAPSULE_TRUST must be absolute\n' >&2; exit 1 ;; esac
case "${FS2_IMAGE_GATE_TOOLCHAIN:-}" in /*) ;; *) printf 'FS2_IMAGE_GATE_TOOLCHAIN must be absolute\n' >&2; exit 1 ;; esac

# The independently installed bootstrap validates itself from external
# root-owned trust before any repository code runs. It seals source, tools,
# providers, plan, closure and signature, and binds init/plan/show/apply to the
# same private read-only capsule.
exec "$FS2_IMAGE_GATE_BOOTSTRAP" signed-terraform-apply \
  --external-trust "$FS2_EXTERNAL_CAPSULE_TRUST" \
  --toolchain "$FS2_IMAGE_GATE_TOOLCHAIN" \
  --source-root "$source_root" \
  --surfaces "$source_root/security/release-image-surfaces.json" \
  --trust "$source_root/security/image-attestation-trust.json" \
  --root "$root" \
  --plan "$saved_plan" \
  --plan-receipt "$plan_receipt" \
  --closure "$release_closure" \
  --registry-auth-receipt "$registry_receipt" \
  --registry-refresh-registration "$refresh_registration"
