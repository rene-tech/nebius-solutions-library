#!/usr/bin/env bash
set -euo pipefail

readonly registry_lookup_attempts=4
readonly registry_lookup_timeout_seconds=20
readonly registry_lookup_kill_seconds=5

fail() {
  printf 'Kueue release verification failed: %s\n' "$*" >&2
  exit 1
}

unexpected_failure() {
  local line="$1"
  local status="$2"
  printf 'Kueue release verification stopped unexpectedly at line %s (exit %s)\n' \
    "${line}" "${status}" >&2
  exit "${status}"
}

trap 'unexpected_failure "${LINENO}" "$?"' ERR

: "${FS2_KUEUE_CHART_REF:?required}"
: "${FS2_KUEUE_CHART_DIGEST:?required}"
: "${FS2_KUEUE_CHART_ARCHIVE_SHA256:?required}"
: "${FS2_KUEUE_IMAGE:?required}"
: "${FS2_KUEUE_RUN_ROOT:?required}"
: "${FS2_KUEUE_CHART_ARCHIVE:?required}"

case "${FS2_KUEUE_CHART_ARCHIVE}" in
  "${FS2_KUEUE_RUN_ROOT}/charts/"*.tgz) ;;
  *) echo "Kueue chart archive must live under the private run root" >&2; exit 2 ;;
esac

case "${FS2_KUEUE_RUN_ROOT}" in
  /*) ;;
  *) echo "Kueue run root must be absolute" >&2; exit 2 ;;
esac

for tool in crane helm sha256sum timeout; do
  command -v "${tool}" >/dev/null 2>&1 || fail "required tool is unavailable: ${tool}"
done

verify_dir="$(mktemp -d "${FS2_KUEUE_RUN_ROOT}/kueue-chart.XXXXXX")"
cleanup() {
  find "${verify_dir}" -type f -delete
  find "${verify_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT

lookup_digest() {
  local subject="$1"
  local reference="$2"
  local expected="$3"
  local stderr_file="${verify_dir}/${subject}-digest.stderr"
  local actual=""
  local diagnostic=""
  local status=0
  local delay=0
  local attempt=0

  for ((attempt = 1; attempt <= registry_lookup_attempts; attempt++)); do
    : >"${stderr_file}"
    set +e
    actual="$(
      timeout --kill-after="${registry_lookup_kill_seconds}s" \
        "${registry_lookup_timeout_seconds}s" \
        crane digest "${reference}" 2>"${stderr_file}"
    )"
    status=$?
    set -e

    if ((status == 0)) && [[ -n "${actual}" ]]; then
      if [[ "${actual}" != "${expected}" ]]; then
        printf 'Kueue %s digest drifted: expected %s, got %s\n' \
          "${subject}" "${expected}" "${actual}" >&2
        return 1
      fi
      printf '%s\n' "${actual}"
      return 0
    fi

    if ((status == 0)); then
      status=1
      printf 'crane returned an empty digest\n' >>"${stderr_file}"
    fi
    diagnostic="$(<"${stderr_file}")"
    diagnostic="${diagnostic//$'\n'/ }"
    diagnostic="${diagnostic:0:1000}"
    if ((status == 124 || status == 137)); then
      diagnostic="lookup timed out after ${registry_lookup_timeout_seconds}s${diagnostic:+: ${diagnostic}}"
    elif [[ -z "${diagnostic}" ]]; then
      diagnostic="no diagnostic output"
    fi

    if ((attempt == registry_lookup_attempts)); then
      printf 'Kueue %s digest lookup failed after %d attempts for %s (last exit %d): %s\n' \
        "${subject}" "${registry_lookup_attempts}" "${reference}" "${status}" "${diagnostic}" >&2
      return 1
    fi

    delay=$((1 << (attempt - 1)))
    printf 'Kueue %s digest lookup attempt %d/%d failed for %s (exit %d): %s; retrying in %ss\n' \
      "${subject}" "${attempt}" "${registry_lookup_attempts}" "${reference}" \
      "${status}" "${diagnostic}" "${delay}" >&2
    sleep "${delay}"
  done
}

digest_ref="${FS2_KUEUE_CHART_REF}@${FS2_KUEUE_CHART_DIGEST}"
if ! actual_chart_digest="$(
  lookup_digest chart "${digest_ref#oci://}" "${FS2_KUEUE_CHART_DIGEST}"
)"; then
  exit 1
fi

image_without_digest="${FS2_KUEUE_IMAGE%@*}"
image_repository="${image_without_digest%:*}"
expected_image_digest="${FS2_KUEUE_IMAGE##*@}"
if ! lookup_digest controller-image \
  "${image_repository}@${expected_image_digest}" \
  "${expected_image_digest}" >/dev/null; then
  exit 1
fi

# Verify the exact archive that will be installed, not a fresh copy of it.
downloaded_chart="${FS2_KUEUE_CHART_ARCHIVE}"
[[ -f "${downloaded_chart}" && ! -L "${downloaded_chart}" ]] || {
  fail "verified chart archive is absent or is a symbolic link: ${downloaded_chart}"
}
actual_archive_sha="$(sha256sum "${downloaded_chart}" | awk '{print $1}')"
if [[ "${actual_archive_sha}" != "${FS2_KUEUE_CHART_ARCHIVE_SHA256}" ]]; then
  fail "chart archive drifted: expected ${FS2_KUEUE_CHART_ARCHIVE_SHA256}, got ${actual_archive_sha}"
fi

rendered="${verify_dir}/kueue.yaml"
if ! helm template fs2-kueue "${downloaded_chart}" \
  --namespace kueue-system \
  --set-string 'controllerManager.nodeSelector.workload\.fs2\.nebius/system=true' \
  --set controllerManager.manager.image.repository="${image_repository}" \
  --set-string controllerManager.manager.image.tag="${image_without_digest##*:}@${expected_image_digest}" >"${rendered}"; then
  fail "Helm could not render the verified chart archive"
fi
grep -Fq "${FS2_KUEUE_IMAGE}" "${rendered}" \
  || fail "rendered chart does not contain the exact digest-qualified controller image"
grep -Fq 'name: clusterqueues.kueue.x-k8s.io' "${rendered}" \
  || fail "rendered chart does not contain the ClusterQueue CRD"
grep -Fq 'workload.fs2.nebius/system: "true"' "${rendered}" \
  || fail "rendered controller is not pinned to system nodes"

# Kueue 0.17.8 renders its CRDs from templates/crd, so the Helm release owns
# and upgrades them. A second server-side-apply owner is deliberately absent.
if ! packaged_crds="$(helm show crds "${downloaded_chart}")"; then
  fail "Helm could not inspect the verified chart archive's crds directory"
fi
if grep -Fq 'kind: CustomResourceDefinition' <<<"${packaged_crds}"; then
  fail "verified chart unexpectedly packages CRDs outside Helm templates"
fi

printf 'Kueue release verified chart=%s archive_sha256=%s image=%s crd_owner=helm-templates\n' \
  "${actual_chart_digest}" "${actual_archive_sha}" "${FS2_KUEUE_IMAGE}"
