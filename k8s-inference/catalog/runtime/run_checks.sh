#!/usr/bin/env bash
set -euo pipefail

catalog_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
control_plane_src="$catalog_root/../../components/control-plane/src"

uv lock --check --project "$catalog_root"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$catalog_root:$control_plane_src" \
  python3 -m unittest discover -v "$catalog_root/tests"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$catalog_root" \
  python3 -m fs2_serve_catalog.cli validate --catalog-root "$catalog_root"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$catalog_root" \
  python3 "$catalog_root/scripts/refresh_scale_contracts.py" --check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$catalog_root" \
  python3 "$catalog_root/scripts/refresh_golden_identities.py" --check
while IFS= read -r -d '' json_file; do
  python3 -m json.tool "$json_file" >/dev/null
done < <(find "$catalog_root" -type f -name '*.json' -print0 | sort -z)
