#!/usr/bin/env bash
# Materialize one digest-pinned OCI Helm chart to a content-addressed path.
#
# This is a Terraform external data program: it reads a JSON object on stdin,
# prints a JSON object of strings on stdout, and sends every diagnostic to
# stderr. It runs during plan, before any resource is created, so the Helm
# provider can resolve the local archive it will install rather than pulling
# its own copy of the same reference.
#
# The archive SHA-256 is part of the filename, so a changed chart cannot reuse
# a previous path, and a rerun with an unchanged chart is a no-op.
set -euo pipefail

payload="$(cat)"
read_field() {
  python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "${payload}" "$1"
}

chart_ref="$(read_field chart_ref)"
chart_digest="$(read_field chart_digest)"
archive_sha256="$(read_field archive_sha256)"
chart_name="$(read_field chart_name)"
run_root="$(read_field run_root)"

case "${run_root}" in
  /*) ;;
  *) echo "chart run root must be absolute" >&2; exit 2 ;;
esac
case "${chart_name}" in
  kueue | jobset) ;;
  *) echo "unsupported chart name: ${chart_name}" >&2; exit 2 ;;
esac
if [[ ! "${chart_digest}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "chart digest must be an exact sha256 reference" >&2
  exit 2
fi
if [[ ! "${archive_sha256}" =~ ^[a-f0-9]{64}$ ]]; then
  echo "chart archive SHA-256 must be 64 lowercase hex characters" >&2
  exit 2
fi

charts_dir="${run_root}/charts"
target="${charts_dir}/${chart_name}-${archive_sha256}.tgz"
install -d -m 0700 "${charts_dir}"

emit() {
  python3 -c 'import json,sys; print(json.dumps({"path": sys.argv[1], "archive_sha256": sys.argv[2], "chart_digest": sys.argv[3]}))' \
    "${target}" "${archive_sha256}" "${chart_digest}"
}

# Idempotent: the verified bytes are already here.
if [[ -f "${target}" && ! -L "${target}" ]]; then
  if [[ "$(sha256sum "${target}" | awk '{print $1}')" == "${archive_sha256}" ]]; then
    emit
    exit 0
  fi
  echo "existing chart archive does not match its content address; removing" >&2
  rm -f "${target}"
fi

work_dir="$(mktemp -d "${charts_dir}/pull.XXXXXX")"
cleanup() {
  find "${work_dir}" -type f -delete
  find "${work_dir}" -depth -type d -empty -delete
}
trap cleanup EXIT

actual_digest="$(crane digest "${chart_ref#oci://}@${chart_digest}" 2>/dev/null || true)"
if [[ "${actual_digest}" != "${chart_digest}" ]]; then
  echo "chart digest drifted or is unreachable: ${actual_digest:-unresolved}" >&2
  exit 1
fi
helm pull "${chart_ref}@${chart_digest}" --destination "${work_dir}" >/dev/null
pulled="$(find "${work_dir}" -maxdepth 1 -type f -name '*.tgz' -print -quit)"
[[ -n "${pulled}" ]] || { echo "chart pull produced no archive" >&2; exit 1; }
actual_archive="$(sha256sum "${pulled}" | awk '{print $1}')"
if [[ "${actual_archive}" != "${archive_sha256}" ]]; then
  echo "chart archive drifted: ${actual_archive}" >&2
  exit 1
fi
install -m 0600 "${pulled}" "${target}"
emit
