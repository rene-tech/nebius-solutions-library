#!/usr/bin/env bash
set -euo pipefail

reference_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
repository_root=$(cd "${reference_root}/.." && pwd)
check_directory=$(mktemp -d)
cleanup() {
  find "${check_directory}" -depth -delete
}
trap cleanup EXIT
export PYTHONPYCACHEPREFIX="${check_directory}/pycache"

cd "${repository_root}"
python3 -m py_compile \
  reference-data/reference_data.py \
  reference-data/render_job.py \
  reference-data/tests/test_reference_data.py
python3 -m unittest discover -s reference-data/tests -v
python3 reference-data/reference_data.py validate-catalog \
  --catalog reference-data/source-catalog.json
python3 reference-data/reference_data.py validate-request \
  --request reference-data/examples/private-msa-request.json
python3 reference-data/render_job.py preprocess \
  --request reference-data/examples/private-msa-request.json \
  >/dev/null

terraform fmt -check -recursive reference-data/terraform
mkdir -p "${check_directory}/reference-data/terraform"
cp reference-data/terraform/*.tf "${check_directory}/reference-data/terraform/"
cp reference-data/reference_data.py reference-data/source-catalog.json \
  reference-data/model-requirements.json "${check_directory}/reference-data/"
TF_DATA_DIR="${check_directory}/terraform-data" terraform \
  -chdir="${check_directory}/reference-data/terraform" init \
  -backend=false -input=false >/dev/null
TF_DATA_DIR="${check_directory}/terraform-data" terraform \
  -chdir="${check_directory}/reference-data/terraform" validate
