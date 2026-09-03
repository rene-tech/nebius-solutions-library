#!/usr/bin/env bash
set -euo pipefail

artifact_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd "${artifact_root}/.." && pwd)
check_root=$(mktemp -d)
cleanup() {
  find "${check_root}" -depth -delete
}
trap cleanup EXIT
export PYTHONPYCACHEPREFIX="${check_root}/pycache"

cd "${repository_root}"
python3 -m py_compile \
  model-artifacts/public_artifacts.py \
  model-artifacts/inspect_protenix_v2.py \
  model-artifacts/render_jobs.py \
  model-artifacts/generate_catalog.py \
  model-artifacts/tests/test_public_artifacts.py
python3 model-artifacts/generate_catalog.py --check
python3 model-artifacts/public_artifacts.py validate \
  --catalog model-artifacts/artifact-catalog.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s model-artifacts/tests -v
python3 model-artifacts/render_jobs.py \
  --catalog model-artifacts/artifact-catalog.json \
  --artifact mosaic-components \
  --project-id project-example \
  --region region-example \
  --cluster cluster-example \
  --filesystem-id computefilesystem-example \
  --filesystem-size-gib 2048 \
  --namespace fs2-reference-data \
  --local-queue reference-data \
  --service-account fs2-reference-data \
  --shared-filesystem-host-path /mnt/fs2-reference-data/data \
  --cpu-pool-id computenodegroup-example \
  --cpu-pool-name reference-data-cpu \
  --node-selector '{"workload.fs2.nebius/reference-data":"true","capacity.fs2.nebius/type":"regular","capacity.fs2.nebius/pool":"reference-data","storage.fs2.nebius/reference-data":"true"}' \
  --node-toleration '{"key":"workload.fs2.nebius/reference-data","operator":"Equal","value":"true","effect":"NoSchedule"}' \
  --reference-plane-source-commit fedcba9876543210 \
  --source-commit 0123456789abcdef \
  >"${check_root}/jobs.json"
python3 -m json.tool "${check_root}/jobs.json" >/dev/null

terraform fmt -check -recursive model-artifacts/terraform
mkdir -p "${check_root}/terraform"
cp model-artifacts/terraform/*.tf "${check_root}/terraform/"
TF_DATA_DIR="${check_root}/terraform-data" terraform \
  -chdir="${check_root}/terraform" init -backend=false -input=false >/dev/null
TF_DATA_DIR="${check_root}/terraform-data" terraform \
  -chdir="${check_root}/terraform" validate
terraform_args=(
  -var=project_id=project-example
  -var=cluster_region=region-example
  -var=cluster_name=cluster-example
  -var=source_commit=0123456789abcdef
  -var=reference_plane_source_commit=fedcba9876543210
  -var=reference_plane_integrated=true
  -var=public_source_staging_enabled=true
  -var=filesystem_id=computefilesystem-example
  -var=filesystem_size_gib=2048
  -var=namespace=fs2-reference-data
  -var=local_queue=reference-data
  -var=service_account=fs2-reference-data
  -var=shared_filesystem_host_path=/mnt/fs2-reference-data/data
  -var=cpu_pool_id=computenodegroup-example
  -var=cpu_pool_name=reference-data-cpu
  '-var=node_selector={"workload.fs2.nebius/reference-data":"true","capacity.fs2.nebius/type":"regular","capacity.fs2.nebius/pool":"reference-data","storage.fs2.nebius/reference-data":"true"}'
  '-var=node_toleration={"key":"workload.fs2.nebius/reference-data","operator":"Equal","value":"true","effect":"NoSchedule"}'
)
TF_DATA_DIR="${check_root}/terraform-data" terraform \
  -chdir="${check_root}/terraform" plan -input=false -lock=false -refresh=false \
  -out="${check_root}/terraform.plan" "${terraform_args[@]}" >/dev/null
test "$(TF_DATA_DIR="${check_root}/terraform-data" terraform \
  -chdir="${check_root}/terraform" show -json "${check_root}/terraform.plan" | jq '.resource_changes // [] | length')" -eq 0

python3 - <<'PY'
import json
from pathlib import Path
for path in Path("model-artifacts").glob("*.json"):
    json.loads(path.read_text(encoding="utf-8"))
print("all model-artifact JSON documents parse")
PY
