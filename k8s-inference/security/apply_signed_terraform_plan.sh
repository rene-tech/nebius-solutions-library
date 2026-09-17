#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'usage: %s FOUNDATION_OR_WORKLOADS_ROOT SAVED_PLAN RELEASE_CLOSURE\n' "$0" >&2
}

if [[ $# -ne 3 ]]; then
  usage
  exit 2
fi

root=$1
saved_plan=$2
release_closure=$3
case "$root" in
  stages/foundation|stages/workloads) ;;
  *) printf 'unsupported production Terraform root: %s\n' "$root" >&2; exit 1 ;;
esac

source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
repository_root=$(cd "$source_root/.." && pwd)
case "$saved_plan" in /*) ;; *) saved_plan="$PWD/$saved_plan" ;; esac
case "$release_closure" in /*) ;; *) release_closure="$PWD/$release_closure" ;; esac

test -f "$saved_plan"
test -f "$release_closure"
test -f "$release_closure.sig"
command -v terraform >/dev/null 2>&1

# The JSON is streamed from the exact saved binary plan and is never replaced
# by a caller-selected sidecar. The same saved plan is applied only after its
# complete Helm resource closure passes the protected signature and hash gate.
terraform -chdir="$source_root/$root" show -json "$saved_plan" | \
  python3 "$source_root/security/release_image_closure.py" \
    --root "$source_root" \
    --surfaces "$source_root/security/release-image-surfaces.json" \
    --trust "$source_root/security/image-attestation-trust.json" \
    --verify-terraform-closure "$release_closure" \
    --plan-root "$root" \
    --terraform-plan-json-stdin

export FS2_IMAGE_GATE_AUTHORIZATION="$release_closure"
exec terraform -chdir="$source_root/$root" apply "$saved_plan"
