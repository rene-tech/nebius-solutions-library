#!/usr/bin/env bash
set -euo pipefail

if [[ -r /opt/fs2/activate.sh ]]; then
  # Pixi emits the exact environment activation required by the frozen lock.
  # shellcheck disable=SC1091
  source /opt/fs2/activate.sh
fi

exec "$@"
