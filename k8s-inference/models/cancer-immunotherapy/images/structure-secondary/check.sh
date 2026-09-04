#!/usr/bin/env bash
set -euo pipefail

runtime_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
openfold_revision=c4771653c5d0a3ebb0b3af71b05efd64bc44ee86
protenix_revision=2475421477ab414b571149ad4a875c390ff8a35d
artifact_worker_repository=https://github.com/rene-tech/nebius-solutions-library.git
artifact_worker_revision=a1ecc219f5e319be87cfa20d5a79af1e3674c6f0
artifact_generator_sha256=e7ec850a96daaf7d9463d953490d263069406ff4f1b125d400d75390372994b8
superseded_unreachable_revision=80d3b940f05597dde2beaaf55a9fa2a9c55f1e02
temporary_root=""

cleanup() {
  if [[ -n "$temporary_root" && "$temporary_root" == /tmp/fs2-structure-source-check.* ]]; then
    rm -rf -- "$temporary_root"
  fi
}
trap cleanup EXIT

for tool in git uv; do
  command -v "$tool" >/dev/null || {
    printf 'required source-check tool is missing: %s\n' "$tool" >&2
    exit 1
  }
done

checkout_exact_tag() {
  local repository="$1"
  local tag="$2"
  local revision="$3"
  local destination="$4"
  git init --quiet "$destination"
  git -C "$destination" remote add origin "$repository"
  git -C "$destination" fetch --quiet --depth=1 origin "refs/tags/${tag}:refs/tags/${tag}"
  git -C "$destination" checkout --quiet --detach "refs/tags/${tag}"
  test "$(git -C "$destination" rev-parse HEAD)" = "$revision"
  test "$(git -C "$destination" describe --tags --exact-match HEAD)" = "$tag"
}

checkout_exact_revision() {
  local repository="$1"
  local revision="$2"
  local destination="$3"
  git init --quiet "$destination"
  git -C "$destination" remote add origin "$repository"
  git -C "$destination" fetch --quiet --no-tags --depth=1 origin "$revision"
  git -C "$destination" checkout --quiet --detach FETCH_HEAD
  test "$(git -C "$destination" rev-parse HEAD)" = "$revision"
}

openfold_source="${FS2_OPENFOLD3_SOURCE:-}"
protenix_source="${FS2_PROTENIX_SOURCE:-}"
temporary_root="$(mktemp -d /tmp/fs2-structure-source-check.XXXXXX)"
if [[ -z "$openfold_source" ]]; then
  openfold_source="${temporary_root}/openfold3"
  checkout_exact_tag \
    https://github.com/aqlaboratory/openfold-3.git \
    v0.5.0 \
    "$openfold_revision" \
    "$openfold_source"
fi
if [[ -z "$protenix_source" ]]; then
  protenix_source="${temporary_root}/protenix"
  checkout_exact_tag \
    https://github.com/bytedance/Protenix.git \
    v2.0.0 \
    "$protenix_revision" \
    "$protenix_source"
fi
artifact_worker_source="${temporary_root}/artifact-worker"
checkout_exact_revision \
  "$artifact_worker_repository" \
  "$artifact_worker_revision" \
  "$artifact_worker_source"
if git -C "$artifact_worker_source" cat-file -e \
  "${superseded_unreachable_revision}^{commit}" 2>/dev/null; then
  printf 'fresh artifact-worker source unexpectedly contains superseded %s\n' \
    "$superseded_unreachable_revision" >&2
  exit 1
fi
artifact_generator="${artifact_worker_source}/k8s-inference/model-artifacts/generate_catalog.py"
printf 'ARTIFACT_GENERATOR_SOURCE revision=%s fetch=immutable-commit sha256=%s superseded_present=false\n' \
  "$artifact_worker_revision" "$artifact_generator_sha256"

test "$(git -C "$openfold_source" rev-parse HEAD)" = "$openfold_revision"
test "$(git -C "$protenix_source" rev-parse HEAD)" = "$protenix_revision"
printf '%s  %s\n' "$artifact_generator_sha256" "$artifact_generator" | sha256sum --check --status

FS2_OPENFOLD3_SOURCE="$openfold_source" \
FS2_PROTENIX_SOURCE="$protenix_source" \
FS2_ARTIFACT_GENERATOR="$artifact_generator" \
PYTHONDONTWRITEBYTECODE=1 \
uv run \
  --with 'pydantic>=2,<3' \
  --with numpy \
  --with gemmi \
  --with packaging \
  --with pyyaml \
  --with jsonschema \
  --python 3.11 \
  python -m unittest discover -s "${runtime_dir}/tests" -p 'test_*.py' -v

python3 -m py_compile "${runtime_dir}"/*.py "${runtime_dir}"/tests/*.py
git -C "$runtime_dir" diff --check
