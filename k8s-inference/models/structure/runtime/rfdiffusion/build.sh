#!/usr/bin/env bash
set -euo pipefail

runtime_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="${1:-fs2-structure/rfdiffusion-upstream:86507b65-cu130}"

docker build \
  --file "${runtime_dir}/Dockerfile" \
  --label org.opencontainers.image.created="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --tag "${image}" \
  "${runtime_dir}"

docker image inspect "${image}" --format '{{.Id}} {{json .Config.Labels}}'
