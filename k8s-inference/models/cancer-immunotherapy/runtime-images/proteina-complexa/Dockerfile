# syntax=docker/dockerfile:1.7

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04@sha256:50efab398f76258daa91ceebb33b6467e40217c67ea44fb5a2cebc6be7d9cce3
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e

FROM ${UV_IMAGE} AS uv
FROM ${CUDA_DEVEL_IMAGE} AS build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG SOURCE_REVISION
ARG SOURCE_ARCHIVE_URL
ARG SOURCE_ARCHIVE_SHA256
ARG SOURCE_DATE_EPOCH

ENV DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300 \
    TORCH_CUDA_ARCH_LIST="7.5;8.0;8.6;8.9;9.0+PTX" \
    FORCE_CUDA=1

COPY --from=uv /uv /uvx /usr/local/bin/
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake curl git libffi-dev libgl1 libhdf5-dev \
      libopenbabel-dev libssl-dev libxext-dev libxml2-dev libxrender-dev libxslt1-dev \
      ninja-build pkg-config python3-dev python3-venv && \
    rm -rf /var/lib/apt/lists/*

COPY proteina-complexa/requirements.lock /tmp/requirements.lock
RUN uv venv --python /usr/bin/python3 /opt/venv && \
    uv pip sync --python /opt/venv/bin/python /tmp/requirements.lock \
      --find-links https://storage.googleapis.com/jax-releases/jax_cuda_releases.html && \
    uv pip install --python /opt/venv/bin/python \
      hatchling==1.27.0 pathspec==0.12.1 pluggy==1.6.0 trove-classifiers==2026.6.1.19 && \
    uv pip install --python /opt/venv/bin/python --no-build-isolation \
      'https://files.pythonhosted.org/packages/f5/ab/2a44ecac0f891dd0d765fc59ac8d277c6283a31907626560e72685df2ed6/torch_scatter-2.1.2.tar.gz#sha256=69b3aa435f2424ac6a1bfb6ff702da6eb73b33ca0db38fb26989c74159258e47'

# ColabDesign's JAX 0.4.29 path uses the pre-take_current_trace Haiku API.
RUN uv pip install --python /opt/venv/bin/python --no-deps \
      'https://files.pythonhosted.org/packages/1c/c2/4a32e22bad1c5c675ac53701b099ce39c286970326512d3e9b06f8866f7d/dm_haiku-0.0.12-py3-none-any.whl#sha256=7448a43a6486bff95253f84e18eacc607d9c1256592573117a9d1d23e2780706'

RUN curl -fL --retry 5 --retry-delay 2 -o /tmp/source.tar.gz "${SOURCE_ARCHIVE_URL}" && \
    echo "${SOURCE_ARCHIVE_SHA256}  /tmp/source.tar.gz" | sha256sum -c - && \
    mkdir -p /opt/fs2/source && \
    tar -xzf /tmp/source.tar.gz --strip-components=1 -C /opt/fs2/source && \
    rm /tmp/source.tar.gz && \
    uv pip install --python /opt/venv/bin/python --no-deps --no-build-isolation /opt/fs2/source

COPY runtime_probe.py /opt/fs2/runtime_probe.py
RUN mkdir -p /opt/fs2/artifacts && \
    PATH=/opt/venv/bin:${PATH} FS2_ARTIFACT_ROOT=/opt/fs2/artifacts \
      python /opt/fs2/runtime_probe.py --model proteina-complexa

FROM ${CUDA_RUNTIME_IMAGE} AS runtime

ARG SOURCE_REVISION
ARG SOURCE_DATE_EPOCH
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/fs2/source /opt/fs2/source
COPY --from=build /opt/fs2/runtime_probe.py /opt/fs2/runtime_probe.py
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      libffi8 libgl1 libgomp1 libhdf5-103-1t64 libopenbabel7 libssl3t64 libxext6 \
      libxml2 libxrender1 libxslt1.1 python3 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 fs2 && useradd --uid 10001 --gid 10001 --create-home fs2 && \
    mkdir -p /opt/fs2/artifacts /work /tmp/fs2-cache && \
    chown -R 10001:10001 /opt/fs2/artifacts /work /tmp/fs2-cache

LABEL org.opencontainers.image.source="https://github.com/NVIDIA-BioNeMo/Proteina-Complexa" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.created="2026-08-27T22:37:20Z" \
      org.opencontainers.image.licenses="Apache-2.0" \
      ai.nebius.fs2.runtime.kind="scientific-batch" \
      ai.nebius.fs2.runtime.weights="external" \
      ai.nebius.fs2.runtime.cuda="12.6" \
      ai.nebius.fs2.runtime.pytorch="2.7.0+cu126" \
      ai.nebius.fs2.runtime.base-substitution="nvcr.io/nvidia/pytorch:24.08-py3->nvidia/cuda:12.6.3-base-ubuntu24.04"

ENV PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/fs2/source/src \
    FS2_ARTIFACT_ROOT=/opt/fs2/artifacts \
    HF_HOME=/opt/fs2/artifacts/huggingface \
    XDG_CACHE_HOME=/opt/fs2/artifacts/xdg \
    RF3_CKPT_PATH=/opt/fs2/artifacts/rosettafold3/rf3_foundry_01_24_latest_remapped.ckpt \
    AF2_DIR=/opt/fs2/artifacts/alphafold2 \
    SC_EXEC=/opt/fs2/artifacts/tools/sc \
    DSSP_EXEC=/opt/fs2/artifacts/tools/dssp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/fs2/runtime_probe.py", "--model", "proteina-complexa"]
