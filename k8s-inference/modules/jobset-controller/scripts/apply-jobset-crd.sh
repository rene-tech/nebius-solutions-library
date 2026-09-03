#!/usr/bin/env bash
set -euo pipefail

: "${FS2_JOBSET_CHART_ARCHIVE:?required}"
: "${FS2_JOBSET_CHART_ARCHIVE_SHA256:?required}"
: "${FS2_JOBSET_KUBECONFIG:?required}"
: "${FS2_JOBSET_CONTEXT:?required}"
: "${FS2_JOBSET_RUN_ROOT:?required}"
: "${FS2_JOBSET_KUBERNETES_MINOR:?required}"

case "${FS2_JOBSET_KUBECONFIG}" in
  "${FS2_JOBSET_RUN_ROOT}/kubeconfig") ;;
  *) echo "JobSet CRD upgrade rejected a kubeconfig outside the run root" >&2; exit 2 ;;
esac
case "${FS2_JOBSET_KUBERNETES_MINOR}" in
  v1.*) ;;
  *) echo "JobSet CRD upgrade requires a normalized Kubernetes minor" >&2; exit 2 ;;
esac

case "${FS2_JOBSET_CHART_ARCHIVE}" in
  "${FS2_JOBSET_RUN_ROOT}/charts/"*.tgz) ;;
  *) echo "JobSet CRD upgrade rejected a chart archive outside the run root" >&2; exit 2 ;;
esac

upgrade_dir="$(mktemp -d "${FS2_JOBSET_RUN_ROOT}/jobset-crd.XXXXXX")"
cleanup() {
  find "${upgrade_dir}" -type f -delete
  find "${upgrade_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT
# The exact archive materialized during plan. Pulling again here could install
# a different chart than the one whose bytes were verified.
chart_archive="${FS2_JOBSET_CHART_ARCHIVE}"
[[ -f "${chart_archive}" && ! -L "${chart_archive}" ]] || {
  echo "verified JobSet chart archive is absent" >&2
  exit 1
}
actual_archive_sha="$(sha256sum "${chart_archive}" | awk '{print $1}')"
if [[ "${actual_archive_sha}" != "${FS2_JOBSET_CHART_ARCHIVE_SHA256}" ]]; then
  echo "JobSet chart archive drifted before CRD upgrade: ${actual_archive_sha}" >&2
  exit 1
fi

kubectl=(kubectl --kubeconfig "${FS2_JOBSET_KUBECONFIG}" --context "${FS2_JOBSET_CONTEXT}")
server_version="$("${kubectl[@]}" version -o json | jq -r '.serverVersion.gitVersion')"
server_minor="$(sed -E 's/^v?([0-9]+\.[0-9]+).*/v\1/' <<<"${server_version}")"
if [[ "${server_minor}" != "${FS2_JOBSET_KUBERNETES_MINOR}" ]]; then
  echo "JobSet CRD upgrade expected Kubernetes minor ${FS2_JOBSET_KUBERNETES_MINOR}, got ${server_version}" >&2
  exit 1
fi

crd_file="${upgrade_dir}/jobset-crds.yaml"
crd_member="$(tar -tzf "${chart_archive}" | grep -E '^[^/]+/crds/jobset\.x-k8s\.io_jobsets\.yaml$')"
[[ -n "${crd_member}" && "$(wc -l <<<"${crd_member}")" -eq 1 ]] || {
  echo "verified JobSet chart must contain exactly one canonical JobSet CRD" >&2
  exit 1
}
tar -xOzf "${chart_archive}" "${crd_member}" >"${crd_file}"

grep -Fq 'name: jobsets.jobset.x-k8s.io' "${crd_file}"
grep -Fq 'name: v1alpha2' "${crd_file}"
grep -Fq 'served: true' "${crd_file}"
grep -Fq 'storage: true' "${crd_file}"
# The chart may already own this CRD from a prior Helm install, and a
# CustomResourceDefinition has managed fields no other manager may take
# silently. Adopt them explicitly rather than failing the upgrade.
"${kubectl[@]}" apply --server-side --force-conflicts \
  --field-manager=fs2-jobset-crd -f "${crd_file}"
"${kubectl[@]}" wait --for=condition=Established --timeout=180s crd/jobsets.jobset.x-k8s.io

printf 'JobSet CRD upgraded archive_sha256=%s server=%s api=jobset.x-k8s.io/v1alpha2\n' \
  "${actual_archive_sha}" "${server_version}"
