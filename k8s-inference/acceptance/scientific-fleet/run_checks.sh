#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s "$root/acceptance/scientific-fleet/tests" \
  -p 'test_*.py' -v
