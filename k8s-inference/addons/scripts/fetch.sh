#!/usr/bin/env bash
set -euo pipefail

addons_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lock.env
source "$addons_dir/lock.env"
state_home=${XDG_STATE_HOME:-$HOME/.local/state}
cache_dir=${ADDONS_CACHE_DIR:-$state_home/nebius-k8s-inference/addons-cache}
mkdir -p "$cache_dir"
chmod 700 "$(dirname "$cache_dir")" "$cache_dir"

verify_file() {
  local path=$1 expected=$2
  local actual
  actual=$(sha256sum "$path" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || {
    printf 'checksum mismatch for %s: expected %s, got %s\n' "$path" "$expected" "$actual" >&2
    return 1
  }
}

fetch_http() {
  local url=$1 file=$2 sha=$3
  if [[ ! -f "$cache_dir/$file" ]]; then
    curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$cache_dir/$file" "$url"
  fi
  verify_file "$cache_dir/$file" "$sha"
}

pull_oci() {
  local ref=$1 version=$2 file=$3 sha=$4
  if [[ ! -f "$cache_dir/$file" ]]; then
    helm pull "$ref" --version "$version" --destination "$cache_dir"
  fi
  verify_file "$cache_dir/$file" "$sha"
}

fetch_http "$GATEWAY_API_URL" "$GATEWAY_API_FILE" "$GATEWAY_API_SHA256"
pull_oci "$ENVOY_GATEWAY_CHART" "$ENVOY_GATEWAY_VERSION" "$ENVOY_GATEWAY_CHART_FILE" "$ENVOY_GATEWAY_CHART_SHA256"
fetch_http "$CERT_MANAGER_CHART_URL" "$CERT_MANAGER_CHART_FILE" "$CERT_MANAGER_CHART_SHA256"
pull_oci "$KUEUE_CHART" "$KUEUE_VERSION" "$KUEUE_CHART_FILE" "$KUEUE_CHART_SHA256"
fetch_http "$KEDA_CHART_URL" "$KEDA_CHART_FILE" "$KEDA_CHART_SHA256"
pull_oci "$KSERVE_CRD_CHART" "$KSERVE_VERSION" "$KSERVE_CRD_CHART_FILE" "$KSERVE_CRD_CHART_SHA256"
pull_oci "$KSERVE_RESOURCES_CHART" "$KSERVE_VERSION" "$KSERVE_RESOURCES_CHART_FILE" "$KSERVE_RESOURCES_CHART_SHA256"

printf 'verified add-on cache: %s\n' "$cache_dir"
