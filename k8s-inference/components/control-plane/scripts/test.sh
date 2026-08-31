#!/usr/bin/env bash
set -euo pipefail

control_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd "${control_root}/../../.." && pwd)"
chart="${repo_root}/k8s-inference/charts/control-plane/fs2-serve-control-plane"
test_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111"
test_repository="registry.nebius.cloud/unit/fs2-serve-control-plane"
test_public_url="https://203.0.113.17"
test_authorization_url="https://identity.unit.test"
test_catalog_rollout_digest="sha256:3333333333333333333333333333333333333333333333333333333333333333"
helm_test_values=(
  --set "image.repository=${test_repository}"
  --set "image.digest=${test_digest}"
  --set "catalog.rolloutDigest=${test_catalog_rollout_digest}"
  --set "config.publicBaseUrl=${test_public_url}"
  --set "config.authorizationServerUrl=${test_authorization_url}"
  --set "config.publicAuthorityMode=ip"
  --set "httpRoute.authorityMode=ip"
)

cd "${control_root}"
uv sync --frozen --all-groups
uv run ruff check src tests
uv run ruff format --check src tests
PYTHONPATH="../../catalog/runtime" uv run mypy src
PYTHONPATH="src:../../catalog/runtime" uv run pytest -q

cd "${repo_root}"
bash k8s-inference/catalog/runtime/run_checks.sh
helm lint "${chart}" --namespace fs2-system "${helm_test_values[@]}"
helm template fs2-serve "${chart}" --namespace fs2-system \
  "${helm_test_values[@]}" >/dev/null
promtool check rules <(
  helm template fs2-serve "${chart}" --namespace fs2-system \
    "${helm_test_values[@]}" \
    --set serviceMonitor.enabled=true \
    --set prometheusRule.enabled=true \
    | yq 'select(.kind == "PrometheusRule") | {"groups": .spec.groups}' -
)
