#!/usr/bin/env bash
set -euo pipefail

admin_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${admin_root}"

python3 -m json.tool contracts/admin-console-plan.json >/dev/null
python3 -m json.tool acceptance/inventory.fixture.json >/dev/null
python3 -m json.tool acceptance/status-cases.json >/dev/null
python3 -m py_compile acceptance/validate_plan.py tests/test_plan.py
python3 acceptance/validate_plan.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n run_checks.sh
