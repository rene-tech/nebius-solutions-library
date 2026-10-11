#!/bin/sh
# Local CPU acceptance only. Mount one synthetic test directory, no credentials.
set -eu
: "${SAI_GMX_DOCKER_WORKSPACE:?synthetic test workspace is required}"
: "${SAI_GMX_DOCKER_IMAGE:?digest-pinned local image is required}"
case "$SAI_GMX_DOCKER_IMAGE" in *@sha256:*) ;; *) exit 2 ;; esac
exec docker run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --cpus 2 --memory 2g \
  --mount "type=bind,src=$SAI_GMX_DOCKER_WORKSPACE,dst=$SAI_GMX_DOCKER_WORKSPACE" \
  --workdir "$PWD" --env OMP_NUM_THREADS --env GMX_MAXBACKUP \
  --entrypoint /opt/gromacs/bin/gmx "$SAI_GMX_DOCKER_IMAGE" "$@"
