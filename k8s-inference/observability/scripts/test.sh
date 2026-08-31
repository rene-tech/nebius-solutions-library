#!/usr/bin/env bash
set -euo pipefail

task_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
task_lock="$task_root/versions.lock.yaml"
task_tmp=$(mktemp -d /tmp/fs2-observability-test.XXXXXX)

cleanup() {
  case "$task_tmp" in
    /tmp/fs2-observability-test.*) find "$task_tmp" -depth -delete ;;
    *) return 1 ;;
  esac
}
trap cleanup EXIT

for task_bin in helm yq jq sha256sum promtool; do
  command -v "$task_bin" >/dev/null || { printf 'missing required command: %s\n' "$task_bin" >&2; exit 1; }
done

pull_chart() {
  local task_key=$1
  local task_repo task_name task_version task_sha task_archive
  task_repo=$(yq -r ".charts.${task_key}.repository" "$task_lock")
  task_name=$(yq -r ".charts.${task_key}.name" "$task_lock")
  task_version=$(yq -r ".charts.${task_key}.version" "$task_lock")
  task_sha=$(yq -r ".charts.${task_key}.sha256" "$task_lock")
  helm pull --repo "$task_repo" "$task_name" --version "$task_version" --destination "$task_tmp"
  task_archive="$task_tmp/${task_name}-${task_version}.tgz"
  test "$(sha256sum "$task_archive" | awk '{print $1}')" = "$task_sha"
  printf '%s\n' "$task_archive"
}

task_kps=$(pull_chart kubePrometheusStack)
task_loki=$(pull_chart loki)
task_otel=$(pull_chart openTelemetryCollector)
task_dcgm=$(pull_chart dcgmExporter)

helm lint "$task_kps" -f "$task_root/values/kube-prometheus-stack.yaml"
helm lint "$task_loki" -f "$task_root/values/loki.yaml"
helm lint "$task_otel" -f "$task_root/values/otel-gateway.yaml"
helm lint "$task_otel" -f "$task_root/values/otel-node.yaml"
helm lint "$task_dcgm" -f "$task_root/values/dcgm-exporter.yaml"

helm template fs2-monitoring "$task_kps" -n fs2-observability -f "$task_root/values/kube-prometheus-stack.yaml" >"$task_tmp/kps.yaml"
helm template fs2-loki "$task_loki" -n fs2-observability -f "$task_root/values/loki.yaml" >"$task_tmp/loki.yaml"
helm template fs2-otel-gateway "$task_otel" -n fs2-observability -f "$task_root/values/otel-gateway.yaml" >"$task_tmp/otel-gateway.yaml"
helm template fs2-otel-node "$task_otel" -n fs2-observability -f "$task_root/values/otel-node.yaml" >"$task_tmp/otel-node.yaml"
helm template fs2-dcgm "$task_dcgm" -n fs2-observability -f "$task_root/values/dcgm-exporter.yaml" >"$task_tmp/dcgm.yaml"

for task_render in "$task_tmp"/*.yaml; do yq eval-all '.' "$task_render" >/dev/null; done
yq eval-all '.' "$task_root/manifests/observability.yaml" >/dev/null
test -z "$(rg -n 'kind:\s*Secret|Authorization:\s|Bearer\s|api[_-]?key:\s*[^\"{]' "$task_root" || true)"
test -z "$(find "$task_root" -type f -iname '*post*render*' -print)"
test -z "$(yq '.. | select(tag == "!!map" and has("image")) | .image | select(tag == "!!str")' "$task_tmp/kps.yaml" "$task_tmp/loki.yaml" "$task_tmp/otel-gateway.yaml" "$task_tmp/otel-node.yaml" | rg ':latest($|@)' || true)"

python3 - "$task_lock" "$task_tmp/kps.yaml" "$task_tmp/loki.yaml" "$task_tmp/otel-gateway.yaml" "$task_tmp/otel-node.yaml" <<'PY'
import pathlib
import sys
import yaml

lock = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
locked_images = set(lock["images"])
rendered_images = set()

def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                rendered_images.add(child)
            walk(child)
    elif isinstance(value, list):
        for child in value:
            walk(child)

for source in sys.argv[2:]:
    for document in yaml.safe_load_all(pathlib.Path(source).read_text()):
        walk(document)

missing = sorted(rendered_images - locked_images)
assert not missing, f"rendered images absent from versions.lock.yaml: {missing}"
PY

python3 - "$task_root/manifests/observability.yaml" <<'PY'
import json
import pathlib
import sys
import yaml

docs = list(yaml.safe_load_all(pathlib.Path(sys.argv[1]).read_text()))
dashboards = []
for doc in docs:
    if doc and doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("labels", {}).get("grafana_dashboard") == "1":
        dashboards.extend(json.loads(value) for value in doc.get("data", {}).values())
assert len(dashboards) == 3
assert {item["uid"] for item in dashboards} == {"fs2-platform", "fs2-inference", "fs2-gpu"}
assert all(item.get("panels") for item in dashboards)
PY

yq 'select(.kind == "PrometheusRule") | {"groups": .spec.groups}' "$task_root/manifests/observability.yaml" >"$task_tmp/rules.yaml"
promtool check rules "$task_tmp/rules.yaml"
bash -n "$task_root/scripts/"*.sh
if command -v shellcheck >/dev/null; then shellcheck "$task_root/scripts/"*.sh; fi

printf 'PASS charts=4 renders=5 dashboards=3 prometheus_rules=1 image_policy=official-chart-tags\n'
