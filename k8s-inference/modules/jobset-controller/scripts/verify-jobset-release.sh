#!/usr/bin/env bash
set -euo pipefail

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
rendered="${verify_dir}/jobset.yaml"
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
helm template fs2-jobset "${downloaded_chart}" \
  --namespace jobset-system \
  --include-crds \
  --set-string 'controller.nodeSelector.workload\.fs2\.nebius/system=true' \
  --set image.repository="${image_repository}" \
  --set-string image.tag="${FS2_JOBSET_RENDERED_IMAGE#*:}" >"${rendered}"

grep -Fq 'name: jobsets.jobset.x-k8s.io' "${rendered}"
grep -Fq 'name: v1alpha2' "${rendered}"
grep -Fq 'served: true' "${rendered}"
grep -Fq 'storage: true' "${rendered}"
grep -Fq "image: \"${FS2_JOBSET_RENDERED_IMAGE}\"" "${rendered}"
grep -Fq 'workload.fs2.nebius/system: "true"' "${rendered}"

# JobSet packages its CRD under chart crds/, which Helm never upgrades, so the
# module applies it explicitly from this same digest-pinned chart.
[[ "$(helm show crds "${downloaded_chart}" | grep -c 'kind: CustomResourceDefinition')" -eq 1 ]]

printf 'JobSet release verified chart=%s@%s image=%s api=jobset.x-k8s.io/v1alpha2 crd_owner=server-side-apply\n' \
  "${FS2_JOBSET_CHART_VERSION}" "${actual_chart_digest}" "${FS2_JOBSET_RENDERED_IMAGE}"
