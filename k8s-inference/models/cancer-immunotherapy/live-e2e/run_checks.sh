#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

python3 "$SCRIPT_DIR/acceptance_harness.py" validate
python3 -m unittest discover -s "$SCRIPT_DIR/tests" -v
python3 -m py_compile "$SCRIPT_DIR/acceptance_harness.py"
