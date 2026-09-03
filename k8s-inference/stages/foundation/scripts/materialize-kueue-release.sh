#!/usr/bin/env bash
set -euo pipefail

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
digest_ref="${FS2_KUEUE_CHART_REF}@${FS2_KUEUE_CHART_DIGEST}"
actual_chart_digest="$(crane digest "${digest_ref#oci://}")"
if [[ "${actual_chart_digest}" != "${FS2_KUEUE_CHART_DIGEST}" ]]; then
  echo "Kueue chart digest drifted: ${actual_chart_digest}" >&2
  exit 1
fi

image_without_digest="${FS2_KUEUE_IMAGE%@*}"
image_repository="${image_without_digest%:*}"
expected_image_digest="${FS2_KUEUE_IMAGE##*@}"
actual_image_digest="$(crane digest "${image_repository}@${expected_image_digest}")"
if [[ "${actual_image_digest}" != "${expected_image_digest}" ]]; then
  echo "Kueue controller image digest drifted: ${actual_image_digest}" >&2
  exit 1
fi

verify_dir="$(mktemp -d "${FS2_KUEUE_RUN_ROOT}/kueue-chart.XXXXXX")"
cleanup() {
  find "${verify_dir}" -type f -delete
  find "${verify_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT

# Verify the exact archive that will be installed, not a fresh copy of it.
downloaded_chart="${FS2_KUEUE_CHART_ARCHIVE}"
[[ -f "${downloaded_chart}" && ! -L "${downloaded_chart}" ]] || {
  echo "verified Kueue chart archive is absent" >&2
  exit 1
}
actual_archive_sha="$(sha256sum "${downloaded_chart}" | awk '{print $1}')"
if [[ "${actual_archive_sha}" != "${FS2_KUEUE_CHART_ARCHIVE_SHA256}" ]]; then
  echo "Kueue chart archive drifted: ${actual_archive_sha}" >&2
  exit 1
fi

rendered="${verify_dir}/kueue.yaml"
helm template fs2-kueue "${downloaded_chart}" \
  --namespace kueue-system \
  --set-string 'controllerManager.nodeSelector.workload\.fs2\.nebius/system=true' \
  --set controllerManager.manager.image.repository="${image_repository}" \
  --set-string controllerManager.manager.image.tag="${image_without_digest##*:}@${expected_image_digest}" >"${rendered}"
grep -Fq "${FS2_KUEUE_IMAGE}" "${rendered}"
grep -Fq 'name: clusterqueues.kueue.x-k8s.io' "${rendered}"
grep -Fq 'workload.fs2.nebius/system: "true"' "${rendered}"

# Kueue 0.17.8 renders its CRDs from templates/crd, so the Helm release owns
# and upgrades them. A second server-side-apply owner is deliberately absent.
grep -Fq 'name: clusterqueues.kueue.x-k8s.io' "${rendered}"
[[ "$(helm show crds "${downloaded_chart}" | grep -c 'kind: CustomResourceDefinition')" -eq 0 ]]

printf 'Kueue release verified chart=%s archive_sha256=%s image=%s crd_owner=helm-templates\n' \
  "${actual_chart_digest}" "${actual_archive_sha}" "${FS2_KUEUE_IMAGE}"
