#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

PYTHONDONTWRITEBYTECODE=1 python3 "$root/models/cancer-immunotherapy/primary-fleet-activation/validate_fragments.py"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s "$root/models/cancer-immunotherapy/primary-fleet-activation/tests" \
  -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 uv run --frozen \
  --project "$root/components/control-plane" \
  python "$root/models/cancer-immunotherapy/primary-fleet-activation/adapter_checks.py"
