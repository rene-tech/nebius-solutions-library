#!/usr/bin/env bash
# Local gate for the AlphaFold 3 academic runtime image.
#
# Everything here runs offline, needs no GPU, and touches no licensed byte.
# The parameter load and inference checks require a real GPU and the mounted
# academic artifact, so they are recorded as live H100 evidence instead.
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

image_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${image_root}"

echo "== JSON well-formedness =="
while IFS= read -r -d '' document; do
  python3 -m json.tool "${document}" > /dev/null
done < <(find contracts schemas evidence fixtures -name '*.json' -print0 | sort -z)

echo "== Python compiles =="
python3 -m py_compile build.py runtime/af3_runtime.py fixtures/generate.py tests/*.py

echo "== Contract and Dockerfile invariants =="
python3 build.py check > /dev/null

echo "== Generated command and IO contract is current =="
python3 build.py contract --check-only > /dev/null

echo "== Dockerfile lint =="
if command -v hadolint > /dev/null 2>&1; then
  hadolint Dockerfile
else
  echo "hadolint not installed; skipping lint"
fi

echo "== Unit and interoperability tests =="
# The producer-interoperability tests generate their fixture with the
# reference-data producer's own code, resolved from this repository by default.
# FS2_AF3_PRODUCER_MODULE overrides that path when testing against a checkout
# that is still on its own branch. If neither exposes _terminal_receipt those
# tests skip rather than pretending to have run.
echo "   producer interoperability: ${FS2_AF3_PRODUCER_MODULE:-reference-data/reference_data.py}"
python3 -m unittest discover -s tests -p 'test_*.py' -v

echo "== Shell syntax =="
bash -n run_checks.sh

echo "alphafold3 runtime image checks passed"
