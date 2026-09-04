#!/usr/bin/env bash
set -euo pipefail

# Kubernetes StageInvocation.command replaces the container ENTRYPOINT. Source
# the immutable environment here as well so every advertised executable is a
# complete runtime boundary when invoked directly.
if [[ -r /opt/fs2/activate.sh ]]; then
  # The activation files are generated from pinned Pixi environments at build
  # time and are part of the read-only image. Conda's generated CUDA hook
  # legitimately probes unset variables, so suspend nounset only while the
  # immutable activation script runs.
  # shellcheck disable=SC1091
  set +u
  source /opt/fs2/activate.sh
  set -u
fi

case "${0##*/}" in
  fs2-image-smoke)
    script=/opt/fs2/image_smoke.py
    ;;
  fs2-run-esmfold2)
    script=/opt/fs2/run_esmfold2.py
    ;;
  fs2-run-openfold3)
    script=/opt/fs2/run_openfold3.py
    ;;
  fs2-run-protenix)
    script=/opt/fs2/run_protenix.py
    ;;
  *)
    printf 'unsupported FS2 Python launcher identity: %s\n' "${0##*/}" >&2
    exit 64
    ;;
esac

exec python "$script" "$@"
