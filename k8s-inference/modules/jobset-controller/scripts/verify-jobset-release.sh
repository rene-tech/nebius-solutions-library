#!/usr/bin/env bash
set -euo pipefail

verification_error() {
  local exit_code="$?"
  echo "JobSet release verification failed at line ${BASH_LINENO[0]} (exit ${exit_code})" >&2
  if [[ -n "${rendered_crd:-}" && -f "${rendered_crd}" ]]; then
    echo "Rendered CRD bytes: $(wc -c <"${rendered_crd}")" >&2
    grep -n -m 1 -E 'CustomResourceDefinition|jobsets\.jobset\.x-k8s\.io' \
      "${rendered_crd}" >&2 || true
  fi
  exit "${exit_code}"
}
trap verification_error ERR

: "${FS2_JOBSET_CHART_REF:?required}"
: "${FS2_JOBSET_CHART_VERSION:?required}"
: "${FS2_JOBSET_CHART_DIGEST:?required}"
: "${FS2_JOBSET_CHART_ARCHIVE_SHA256:?required}"
: "${FS2_JOBSET_IMAGE:?required}"
: "${FS2_JOBSET_IMAGE_DIGEST:?required}"
: "${FS2_JOBSET_RENDERED_IMAGE:?required}"
: "${FS2_JOBSET_VERIFY_RUN_ROOT:?required}"
: "${FS2_JOBSET_CHART_ARCHIVE:?required}"

case "${FS2_JOBSET_CHART_ARCHIVE}" in
  "${FS2_JOBSET_VERIFY_RUN_ROOT}/charts/"*.tgz) ;;
  *) echo "JobSet chart archive must live under the private run root" >&2; exit 2 ;;
esac

case "${FS2_JOBSET_VERIFY_RUN_ROOT}" in
  /*) ;;
  *) echo "JobSet verification run root must be absolute" >&2; exit 2 ;;
esac

digest_ref="${FS2_JOBSET_CHART_REF}"
actual_chart_digest="$(crane digest "${digest_ref#oci://}")"
image_repository="${FS2_JOBSET_IMAGE%:*}"
actual_image_digest="$(crane digest "${image_repository}@${FS2_JOBSET_IMAGE_DIGEST}")"
if [[ "${actual_chart_digest}" != "${FS2_JOBSET_CHART_DIGEST}" ]]; then
  echo "JobSet chart digest drifted: ${actual_chart_digest}" >&2
  exit 1
fi
if [[ "${actual_image_digest}" != "${FS2_JOBSET_IMAGE_DIGEST}" ]]; then
  echo "JobSet controller image digest drifted: ${actual_image_digest}" >&2
  exit 1
fi

verify_dir="$(mktemp -d "${FS2_JOBSET_VERIFY_RUN_ROOT}/jobset-render.XXXXXX")"
cleanup() {
  find "${verify_dir}" -type f -delete
  find "${verify_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT
rendered_crd="${verify_dir}/jobset-crd.yaml"
chart_metadata="${verify_dir}/Chart.yaml"
chart_values="${verify_dir}/values.yaml"
controller_template="${verify_dir}/controller-deployment.yaml"
# Verify the exact archive that will be installed, not a fresh copy of it.
downloaded_chart="${FS2_JOBSET_CHART_ARCHIVE}"
[[ -f "${downloaded_chart}" && ! -L "${downloaded_chart}" ]] || {
  echo "verified JobSet chart archive is absent" >&2
  exit 1
}
actual_archive_sha="$(sha256sum "${downloaded_chart}" | awk '{print $1}')"
if [[ "${actual_archive_sha}" != "${FS2_JOBSET_CHART_ARCHIVE_SHA256}" ]]; then
  echo "JobSet chart archive drifted: ${actual_archive_sha}" >&2
  exit 1
fi
crd_member="$(tar -tzf "${downloaded_chart}" | grep -E '^[^/]+/crds/jobset\.x-k8s\.io_jobsets\.yaml$')"
[[ -n "${crd_member}" && "$(wc -l <<<"${crd_member}")" -eq 1 ]]
chart_root="${crd_member%%/*}"
tar -xOzf "${downloaded_chart}" "${crd_member}" >"${rendered_crd}"
tar -xOzf "${downloaded_chart}" "${chart_root}/Chart.yaml" >"${chart_metadata}"
tar -xOzf "${downloaded_chart}" "${chart_root}/values.yaml" >"${chart_values}"
tar -xOzf "${downloaded_chart}" \
  "${chart_root}/templates/controller/deployment.yaml" >"${controller_template}"

grep -Fq 'name: jobsets.jobset.x-k8s.io' "${rendered_crd}"
grep -Fq 'name: v1alpha2' "${rendered_crd}"
grep -Fq 'served: true' "${rendered_crd}"
grep -Fq 'storage: true' "${rendered_crd}"
grep -Fqx "appVersion: ${FS2_JOBSET_CHART_VERSION/#/v}" "${chart_metadata}"
grep -Fqx "version: ${FS2_JOBSET_CHART_VERSION}" "${chart_metadata}"
grep -Fq "repository: ${image_repository}" "${chart_values}"
grep -Fq "tag: ${FS2_JOBSET_CHART_VERSION/#/v}" "${chart_values}"
grep -Fq 'image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"' \
  "${controller_template}"
grep -Fq '{{- with .Values.controller.nodeSelector }}' "${controller_template}"

# JobSet packages its CRD under chart crds/, which Helm never upgrades, so the
# module applies it explicitly from this same digest-pinned chart.
[[ "$(grep -c 'kind: CustomResourceDefinition' "${rendered_crd}")" -eq 1 ]]

printf 'JobSet release verified chart=%s@%s image=%s api=jobset.x-k8s.io/v1alpha2 crd_owner=server-side-apply\n' \
  "${FS2_JOBSET_CHART_VERSION}" "${actual_chart_digest}" "${FS2_JOBSET_RENDERED_IMAGE}"
