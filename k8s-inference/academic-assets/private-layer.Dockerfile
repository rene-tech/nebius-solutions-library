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
      pyrosetta-bindcraft) destination=/opt/fs2/academic-assets/pyrosetta/pyrosetta-2025.24+release.8e1e5e54f0-py310_0.conda ;; \
      *) exit 64 ;; \
    esac; \
    actual="$(sha256sum /run/secrets/licensed_asset)"; \
    actual="${actual%% *}"; \
    test "${actual}" = "${FS2_ASSET_SHA256}"; \
    install -d -m 0700 "$(dirname "${destination}")"; \
    install -o "${FS2_RUNTIME_UID}" -g "${FS2_RUNTIME_GID}" -m 0400 \
      /run/secrets/licensed_asset "${destination}"

LABEL ai.nebius.fs2.contains-licensed-academic-asset="true" \
      ai.nebius.fs2.redistributable="false"

USER ${FS2_RUNTIME_UID}:${FS2_RUNTIME_GID}
