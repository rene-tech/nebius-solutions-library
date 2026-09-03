# syntax=docker/dockerfile:1.7

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04@sha256:a2e1e2360c85298ac47ec2543b406ab1e8cec42e31ee47e4d32140ebc82e1067
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:12.9.1-base-ubuntu24.04@sha256:29e5e3425e2e0f5a4e97c9fb4695ba4887cd78210a43cf94c3bcafc6ab01c5e6

FROM ${UV_IMAGE} AS uv
FROM ${CUDA_DEVEL_IMAGE} AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG SOURCE_REVISION
ARG SOURCE_ARCHIVE_URL
ARG SOURCE_ARCHIVE_SHA256
ARG SOURCE_DATE_EPOCH

ENV DEBIAN_FRONTEND=noninteractive UV_LINK_MODE=copy UV_HTTP_TIMEOUT=300
COPY --from=uv /uv /uvx /usr/local/bin/
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake curl git libffi-dev libgl1 libssl-dev \
      pkg-config python3-dev python3-venv && \
    rm -rf /var/lib/apt/lists/*

COPY mosaic/requirements.lock /tmp/requirements.lock
RUN uv venv --python /usr/bin/python3 /opt/venv && \
    uv pip sync --python /opt/venv/bin/python /tmp/requirements.lock && \
    uv pip install --python /opt/venv/bin/python \
      hatchling==1.27.0 pathspec==0.12.1 pluggy==1.6.0 trove-classifiers==2026.6.1.19

RUN curl -fL --retry 5 --retry-delay 2 -o /tmp/source.tar.gz "${SOURCE_ARCHIVE_URL}" && \
    echo "${SOURCE_ARCHIVE_SHA256}  /tmp/source.tar.gz" | sha256sum -c - && \
    mkdir -p /opt/fs2/source && \
    tar -xzf /tmp/source.tar.gz --strip-components=1 -C /opt/fs2/source && \
    rm /tmp/source.tar.gz && \
    find /opt/fs2/source/src/mosaic/proteinmpnn/weights -type f \
      \( -name '*.pt' -o -name '*.pth' -o -name '*.ckpt' -o -name '*.safetensors' \) -delete && \
    uv pip install --python /opt/venv/bin/python --no-deps --no-build-isolation /opt/fs2/source

COPY runtime_probe.py /opt/fs2/runtime_probe.py
RUN mkdir -p /opt/fs2/artifacts && \
    PATH=/opt/venv/bin:${PATH} FS2_ARTIFACT_ROOT=/opt/fs2/artifacts \
      python /opt/fs2/runtime_probe.py --model mosaic

FROM ${CUDA_RUNTIME_IMAGE} AS runtime

ARG SOURCE_REVISION
ARG SOURCE_DATE_EPOCH
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/fs2/source /opt/fs2/source
COPY --from=build /opt/fs2/runtime_probe.py /opt/fs2/runtime_probe.py
# hadolint ignore=DL3022
COPY --from=adapter bin/mosaic-batch /opt/fs2/bin/mosaic-batch
# hadolint ignore=DL3022
COPY --from=adapter recipe.json /opt/fs2/mosaic/recipe.json
COPY mosaic/runtime_entrypoint.py /opt/fs2/mosaic/runtime_entrypoint.py
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      libffi8 libgl1 libgomp1 libssl3t64 python3 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 fs2 && useradd --uid 10001 --gid 10001 --create-home fs2 && \
    mkdir -p /opt/fs2/artifacts /work /tmp/fs2-cache && \
    chmod 0555 /opt/fs2/bin/mosaic-batch /opt/fs2/mosaic/runtime_entrypoint.py && \
    chown -R 10001:10001 /opt/fs2/artifacts /work /tmp/fs2-cache

LABEL org.opencontainers.image.source="https://github.com/escalante-bio/mosaic" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.created="2026-08-01T12:25:14Z" \
      org.opencontainers.image.licenses="MIT" \
      ai.nebius.fs2.runtime.kind="scientific-batch" \
      ai.nebius.fs2.runtime.weights="external" \
      ai.nebius.fs2.runtime.cuda="12.9" \
      ai.nebius.fs2.runtime.jax="0.10.2" \
      ai.nebius.fs2.runtime.pytorch="2.7.1+cpu" \
      ai.nebius.fs2.runtime.base-substitution="nvcr.io/nvidia/pytorch:24.08-py3->nvidia/cuda:12.9.1-base-ubuntu24.04"

# Every cache default resolves under /tmp. The canonical scientific-batch Job
# runs with readOnlyRootFilesystem and mounts only the request ConfigMap,
# /workspace and a /tmp emptyDir, so any cache rooted in the image filesystem or
# in the read-only artifact plane aborts the run. Boltz compiles its MSA
# featuriser with numba @njit(cache=True), which needs a writable locator.
ENV PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/fs2/source/src \
    FS2_ARTIFACT_ROOT=/opt/fs2/artifacts \
    HOME=/tmp/fs2-home \
    HF_HOME=/tmp/fs2-home/huggingface \
    XDG_CACHE_HOME=/tmp/fs2-home/xdg \
    MPLCONFIGDIR=/tmp/fs2-home/matplotlib \
    NUMBA_CACHE_DIR=/tmp/fs2-home/numba \
    JAX_COMPILATION_CACHE_DIR=/tmp/fs2-home/jax \
    JAX_PLATFORMS=cuda,cpu \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/fs2/runtime_probe.py", "--model", "mosaic"]
