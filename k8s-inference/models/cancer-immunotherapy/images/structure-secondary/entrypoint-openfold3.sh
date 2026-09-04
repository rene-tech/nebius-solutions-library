#!/usr/bin/env bash
set -eo pipefail

if [[ -r /opt/fs2/activate.sh ]]; then
  # Conda CUDA activation hooks legitimately probe optional unset variables.
  # Enable nounset only after the frozen Pixi environment is active.
  # shellcheck disable=SC1091
  source /opt/fs2/activate.sh
fi

set -u
exec "$@"
