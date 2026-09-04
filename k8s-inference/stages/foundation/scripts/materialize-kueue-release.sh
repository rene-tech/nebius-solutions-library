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

for tool in crane helm sha256sum tar timeout; do
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

bounded_shell_quote() {
  local value="${1:0:1000}"
  printf '%q' "${value}"
}

bounded_file_quote() {
  local file="$1"
  local value=""
  if [[ -f "${file}" ]]; then
    value="$(<"${file}")"
  fi
  bounded_shell_quote "${value}"
}

manager_manifest=""
manager_images_file="${verify_dir}/manager-images"
helm_stdout="${verify_dir}/helm-template.stdout"
helm_stderr="${verify_dir}/helm-template.stderr"

render_diagnostic() {
  local reason="$1"
  local manager_bytes="missing"
  local manager_sha256="missing"
  local manager_images=""
  local manager_image_count=0
  local helm_version=""

  if [[ -n "${manager_manifest}" && -f "${manager_manifest}" ]]; then
    manager_bytes="$(wc -c <"${manager_manifest}")"
    manager_sha256="$(sha256sum "${manager_manifest}" | awk '{print $1}')"
  fi
  if [[ -f "${manager_images_file}" ]]; then
    manager_images="$(<"${manager_images_file}")"
    manager_image_count="$(wc -l <"${manager_images_file}")"
  fi
  helm_version="$(helm version --short 2>&1 || true)"

  printf 'Kueue render diagnostic: reason=%s manager_bytes=%s manager_sha256=%s manager_image_count=%s\n' \
    "${reason}" "${manager_bytes}" "${manager_sha256}" "${manager_image_count}" >&2
  printf 'Kueue render diagnostic: expected_image=%s parsed_manager_images=%s helm_version=%s\n' \
    "$(bounded_shell_quote "${FS2_KUEUE_IMAGE}")" \
    "$(bounded_shell_quote "${manager_images}")" \
    "$(bounded_shell_quote "${helm_version}")" >&2
  printf 'Kueue render diagnostic: helm_stdout=%s helm_stderr=%s\n' \
    "$(bounded_file_quote "${helm_stdout}")" \
    "$(bounded_file_quote "${helm_stderr}")" >&2
}

extract_manager_images() {
  local manifest="$1"
  awk '
    function scalar(line, value) {
      value = line
      sub(/^[[:space:]]*[[:alnum:]_-]+:[[:space:]]*/, "", value)
      sub(/[[:space:]]+#[[:print:]]*$/, "", value)
      if (substr(value, 1, 1) == "\"" && substr(value, length(value), 1) == "\"") {
        value = substr(value, 2, length(value) - 2)
      }
      return value
    }
    function emit_container() {
      if (container_name == "manager") {
        print container_image
      }
    }
    $0 == "kind: Deployment" {
      deployment = 1
    }
    deployment && /^      containers:[[:space:]]*$/ {
      in_containers = 1
      container_name = ""
      container_image = ""
      next
    }
    in_containers && /^      -[[:space:]]/ {
      emit_container()
      container_name = ""
      container_image = ""
      next
    }
    in_containers && /^        name:[[:space:]]*/ {
      container_name = scalar($0)
      next
    }
    in_containers && /^        image:[[:space:]]*/ {
      container_image = scalar($0)
      next
    }
    in_containers && /^      [[:alnum:]_-]+:[[:space:]]*/ {
      emit_container()
      in_containers = 0
    }
    END {
      if (in_containers) {
        emit_container()
      }
    }
  ' "${manifest}"
}

# Ask Helm to write each rendered template itself. When a snap-confined
# Terraform launches snap-confined Helm, Helm 4.2.4 can exit successfully while
# emitting zero bytes on its inherited stdout path. --output-dir avoids that
# path and also lets us inspect the manager workload rather than searching an
# aggregate multi-document stream.
render_root="${verify_dir}/rendered"
if ! helm template fs2-kueue "${downloaded_chart}" \
  --namespace kueue-system \
  --set-string 'controllerManager.nodeSelector.workload\.fs2\.nebius/system=true' \
  --set controllerManager.manager.image.repository="${image_repository}" \
  --set-string controllerManager.manager.image.tag="${image_without_digest##*:}@${expected_image_digest}" \
  --output-dir "${render_root}" >"${helm_stdout}" 2>"${helm_stderr}"; then
  render_diagnostic "helm-template-failed"
  fail "Helm could not render the verified chart archive"
fi

mapfile -t manager_manifests < <(
  find "${render_root}" -type f -path '*/templates/manager/manager.yaml' -print
)
if ((${#manager_manifests[@]} != 1)); then
  render_diagnostic "manager-manifest-count-${#manager_manifests[@]}"
  fail "rendered chart must contain exactly one manager workload manifest"
fi
manager_manifest="${manager_manifests[0]}"
if [[ ! -s "${manager_manifest}" ]]; then
  render_diagnostic "manager-manifest-empty"
  fail "rendered manager workload manifest is empty"
fi

extract_manager_images "${manager_manifest}" >"${manager_images_file}"
mapfile -t manager_images <"${manager_images_file}"
if ((${#manager_images[@]} != 1)); then
  render_diagnostic "manager-image-count-${#manager_images[@]}"
  fail "rendered manager workload must contain exactly one manager container image"
fi
if [[ "${manager_images[0]}" != "${FS2_KUEUE_IMAGE}" ]]; then
  render_diagnostic "manager-image-mismatch"
  fail "rendered manager workload image does not match the exact digest-qualified controller image"
fi

mapfile -t clusterqueue_crds < <(
  find "${render_root}" -type f \
    -path '*/templates/crd/kueue.x-k8s.io_clusterqueues.yaml' -print
)
if ((${#clusterqueue_crds[@]} != 1)) || [[ ! -s "${clusterqueue_crds[0]:-}" ]]; then
  render_diagnostic "clusterqueue-crd-missing-or-empty"
  fail "rendered chart must contain one non-empty ClusterQueue CRD manifest"
fi
grep -Fq 'name: clusterqueues.kueue.x-k8s.io' "${clusterqueue_crds[0]}" \
  || fail "rendered chart does not contain the ClusterQueue CRD"
grep -Fq 'workload.fs2.nebius/system: "true"' "${manager_manifest}" \
  || fail "rendered controller is not pinned to system nodes"

# Kueue 0.17.8 renders its CRDs from templates/crd, so the Helm release owns
# and upgrades them. A second server-side-apply owner is deliberately absent.
archive_members="${verify_dir}/archive-members"
archive_members_stderr="${verify_dir}/archive-members.stderr"
if ! tar -tzf "${downloaded_chart}" >"${archive_members}" 2>"${archive_members_stderr}"; then
  fail "could not inspect verified chart members: $(bounded_file_quote "${archive_members_stderr}")"
fi
packaged_crd_member="$(grep -Em1 '^[^/]+/crds/.+' "${archive_members}" || true)"
if [[ -n "${packaged_crd_member}" ]]; then
  fail "verified chart unexpectedly packages CRDs outside Helm templates: $(bounded_shell_quote "${packaged_crd_member}")"
fi

printf 'Kueue release verified chart=%s archive_sha256=%s image=%s crd_owner=helm-templates\n' \
  "${actual_chart_digest}" "${actual_archive_sha}" "${FS2_KUEUE_IMAGE}"
