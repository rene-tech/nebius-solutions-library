# syntax=docker/dockerfile:1.7
#
# RFdiffusion v1.1.0 scientific runtime.
#
# Every external byte is sha256-verified at build time. The image carries no model
# weights: the Base checkpoint is mounted from the artifact plane at run time, so the
# published image stays redistributable and the checkpoint stays content-addressed.
#
# GPU-family agnostic by construction. Nothing here compiles for an architecture and
# nothing selects a device by name: torch and the prebuilt DGL wheel discover whatever
# CUDA device is present. H100 is the current validation target, not a build input.

ARG BASE_IMAGE=pytorch/pytorch@sha256:0279f7aa29974bf64e61d0ff6e979b41a249b3662a46e30778dbf80b8c99c361
FROM ${BASE_IMAGE}

# Upstream RFdiffusion v1.1.0. The tag resolves to this commit; the commit is what we pin.
ARG UPSTREAM_REVISION=9273ef67335acaf91df0150473a274759229cdf6
ARG UPSTREAM_ARCHIVE_URL=https://github.com/RosettaCommons/RFdiffusion/archive/9273ef67335acaf91df0150473a274759229cdf6.tar.gz
ARG UPSTREAM_ARCHIVE_SHA256=b8a29d4d5bd7b60eba40d49da4dbb324685eb409bdbdc7d088c187514ef3f7b9

# DGL 2.3.0+cu121 matches the base image's torch 2.3.0 / CUDA 12.1 ABI.
ARG DGL_WHEEL_URL=https://data.dgl.ai/wheels/torch-2.3/cu121/dgl-2.3.0%2Bcu121-cp310-cp310-manylinux1_x86_64.whl
ARG DGL_WHEEL_SHA256=0423c4e819482077aee0c8c70dec2bfc160ada86ea473313d03bf61045d6ae17

ARG RUNTIME_UID=10001
ARG RUNTIME_GID=10001

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
USER 0

COPY fetch_verified.py /opt/fs2/build/fetch_verified.py

# 1. DGL. Verified, installed without deps so it cannot pull a second torch.
# The download keeps its PEP 427 filename: pip parses the wheel's name for its
# version and ABI tags and rejects anything else.
ARG DGL_WHEEL_FILENAME=dgl-2.3.0+cu121-cp310-cp310-manylinux1_x86_64.whl
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    set -eux; \
    python /opt/fs2/build/fetch_verified.py \
      --url "${DGL_WHEEL_URL}" \
      --output "/tmp/${DGL_WHEEL_FILENAME}" \
      --sha256 "${DGL_WHEEL_SHA256}"; \
    pip install --no-deps --no-cache-dir "/tmp/${DGL_WHEEL_FILENAME}"; \
    rm -f "/tmp/${DGL_WHEEL_FILENAME}"

# 2. Hash-pinned Python additions.
#
# DGL's CUDA kernels dlopen libnvrtc.so.12. The conda base ships no nvrtc on the
# loader path, so the wheel supplies it and the symlink puts it where the linker
# already looks. Without this, `import dgl` succeeds on CPU and only fails once a
# GPU kernel is launched, which would turn a build defect into a runtime failure.
COPY requirements.lock /opt/fs2/build/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    set -eux; \
    pip install --no-deps --no-cache-dir --require-hashes -r /opt/fs2/build/requirements.lock; \
    ln -sfn /opt/conda/lib/python3.10/site-packages/nvidia/cuda_nvrtc/lib/libnvrtc.so.12 \
            /opt/conda/lib/libnvrtc.so.12; \
    test -e /opt/conda/lib/libnvrtc.so.12

# 3. Upstream RFdiffusion v1.1.0 plus its vendored SE3Transformer.
RUN set -eux; \
    python /opt/fs2/build/fetch_verified.py \
      --url "${UPSTREAM_ARCHIVE_URL}" \
      --output /tmp/rfdiffusion.tar.gz \
      --sha256 "${UPSTREAM_ARCHIVE_SHA256}"; \
    mkdir -p /opt/rfdiffusion; \
    tar -xzf /tmp/rfdiffusion.tar.gz --strip-components=1 -C /opt/rfdiffusion; \
    rm -f /tmp/rfdiffusion.tar.gz; \
    test -f /opt/rfdiffusion/scripts/run_inference.py; \
    test -f /opt/rfdiffusion/config/inference/base.yaml; \
    pip install --no-deps --no-cache-dir /opt/rfdiffusion; \
    pip install --no-deps --no-cache-dir /opt/rfdiffusion/env/SE3Transformer

# 4. The fs2 production adapter. Copied last so runtime edits do not rebuild the stack.
COPY runtime_entrypoint.py /opt/fs2/runtime_entrypoint.py

# The upstream tree is read-only at run time; the process writes only to its output
# directory and to /tmp.
RUN set -eux; \
    groupadd --gid "${RUNTIME_GID}" fs2; \
    useradd --uid "${RUNTIME_UID}" --gid "${RUNTIME_GID}" --no-create-home --shell /usr/sbin/nologin fs2; \
    chmod -R a-w /opt/rfdiffusion /opt/fs2; \
    python -m compileall -q /opt/fs2/runtime_entrypoint.py >/dev/null

ENV LD_LIBRARY_PATH=/opt/conda/lib/python3.10/site-packages/nvidia/cuda_nvrtc/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DGLBACKEND=pytorch \
    HYDRA_FULL_ERROR=1 \
    FS2_RFDIFFUSION_HOME=/opt/rfdiffusion \
    FS2_ARTIFACT_ROOT=/opt/fs2/artifacts \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/fs2-cache \
    MPLCONFIGDIR=/tmp/fs2-matplotlib \
    DGL_HOME=/tmp/fs2-dgl

LABEL org.opencontainers.image.title="fs2 RFdiffusion scientific runtime" \
      org.opencontainers.image.source="https://github.com/RosettaCommons/RFdiffusion" \
      org.opencontainers.image.revision="${UPSTREAM_REVISION}" \
      org.opencontainers.image.version="v1.1.0" \
      org.opencontainers.image.licenses="BSD-3-Clause" \
      ai.nebius.fs2.model.id="rfdiffusion" \
      ai.nebius.fs2.upstream.revision="${UPSTREAM_REVISION}" \
      ai.nebius.fs2.upstream.archive.sha256="${UPSTREAM_ARCHIVE_SHA256}" \
      ai.nebius.fs2.runtime.weights="external-artifact-plane" \
      ai.nebius.fs2.runtime.accelerator="gpu-family-agnostic" \
      ai.nebius.fs2.runtime.cuda="12.1" \
      ai.nebius.fs2.runtime.torch="2.3.0"

USER ${RUNTIME_UID}:${RUNTIME_GID}
WORKDIR /opt/rfdiffusion
ENTRYPOINT ["python", "/opt/fs2/runtime_entrypoint.py"]
