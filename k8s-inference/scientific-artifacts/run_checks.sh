#!/usr/bin/env bash
# Every check that guards the dedicated scientific result artifact store.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
cleanup() { rm -rf "${work}"; }
trap cleanup EXIT
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${work}/pycache"

cd "${root}"

echo "== python =="
python3 -m py_compile scientific-artifacts/artifact_store.py
python3 -m json.tool scientific-artifacts/artifact-store-contract.json >/dev/null
python3 -m unittest discover -s scientific-artifacts/tests -v
python3 -m unittest tests.test_scientific_artifact_store_wiring -v

echo "== canonical key =="
python3 scientific-artifacts/artifact_store.py key \
  --tenant fs2-acceptance \
  --operation 0f8b2c1e-4d5a-4a0e-9c3b-77c1b5a9d2e4 \
  --stage semantic-validation \
  --digest "$(printf 'a%.0s' {1..64})"

echo "== terraform =="
terraform fmt -check -recursive \
  stages/infrastructure/scientific_artifacts.tf \
  stages/workloads/scientific_artifacts.tf \
  stages/infrastructure/tests/scientific_artifacts.tftest.hcl \
  stages/workloads/tests/scientific_artifacts.tftest.hcl

for stage in infrastructure workloads; do
  echo "-- ${stage} --"
  TF_DATA_DIR="${work}/tf-${stage}" terraform -chdir="stages/${stage}" \
    init -backend=false -input=false >/dev/null
  TF_DATA_DIR="${work}/tf-${stage}" terraform -chdir="stages/${stage}" validate
  TF_DATA_DIR="${work}/tf-${stage}" terraform -chdir="stages/${stage}" \
    test -test-directory=tests -filter=tests/scientific_artifacts.tftest.hcl
done

echo "-- root facade --"
TF_DATA_DIR="${work}/tf-root" terraform init -backend=false -input=false >/dev/null
TF_DATA_DIR="${work}/tf-root" terraform validate

echo "all scientific artifact store checks passed"
