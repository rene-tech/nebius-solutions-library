# syntax=docker/dockerfile:1.7
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG FS2_ASSET_ID
ARG FS2_ASSET_SHA256
ARG FS2_RUNTIME_UID=65532
ARG FS2_RUNTIME_GID=65532

USER 0
RUN --mount=type=secret,id=licensed_asset,required=true \
    set -eu; \
    case "${FS2_ASSET_ID}" in \
      pyrosetta-bindcraft) destination=/tmp/pyrosetta.whl ;; \
      *) exit 64 ;; \
    esac; \
    actual="$(sha256sum /run/secrets/licensed_asset)"; \
    actual="${actual%% *}"; \
    test "${actual}" = "${FS2_ASSET_SHA256}"; \
    install -d -m 0700 "$(dirname "${destination}")"; \
    install -m 0400 /run/secrets/licensed_asset "${destination}"; \
    python3.10 -m pip install --no-deps --no-index --disable-pip-version-check "${destination}"; \
    PYTHONPATH= python3.10 -c 'import pyrosetta, importlib.metadata as metadata; assert metadata.version("pyrosetta").startswith("2026.29")'; \
    rm -f "${destination}"

LABEL ai.nebius.fs2.contains-licensed-academic-asset="true" \
      ai.nebius.fs2.redistributable="false"

USER ${FS2_RUNTIME_UID}:${FS2_RUNTIME_GID}
