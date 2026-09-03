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

ENV DEBIAN_FRONTEND=noninteractive UV_LINK_MODE=copy UV_HTTP_TIMEOUT=300
COPY --from=uv /uv /uvx /usr/local/bin/
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake curl git libboost-all-dev libffi-dev \
      libgl1 libhdf5-dev libssl-dev libxml2-dev libxslt1-dev pkg-config \
      python3-dev python3-venv && \
    rm -rf /var/lib/apt/lists/*

COPY boltzgen/requirements.lock /tmp/requirements.lock
RUN uv venv --python /usr/bin/python3 /opt/venv && \
    uv pip sync --python /opt/venv/bin/python /tmp/requirements.lock

# BoltzGen only constrains cuEquivariance to >=0.5.0.  The 0.11 packages were
# published after this source revision and their Triton compatibility shim is
# incompatible with the pinned PyTorch 2.7 API.  Keep the source-era 0.10 CUDA
# implementation, with every wheel bound to its PyPI digest.
RUN uv pip install --python /opt/venv/bin/python --no-deps --reinstall \
      'https://files.pythonhosted.org/packages/1b/52/3131b6dc9577eaa556b2b66584a44df32a9d4356aa641ff5d33c8b797225/cuequivariance-0.10.0-py3-none-any.whl#sha256=340f5160b99efe57f8a220db75747e59b8a0f9f3bbced7ec527c46f8cc615e87' \
      'https://files.pythonhosted.org/packages/2b/a3/5c2b6cac9bf5d210536c26bfb1ba191c3c36883ec8188076576332a4a49f/cuequivariance_torch-0.10.0-py3-none-any.whl#sha256=3a13afba71c5e2c2dc154032879c640e9d8653a177efeca0bc9fb99e607cf540' \
      'https://files.pythonhosted.org/packages/9b/aa/ff2ac0defafd3862cc35498a1b6fcf4efdf51c43b043befbb5af64e11b24/cuequivariance_ops_cu12-0.10.0-py3-none-manylinux_2_24_x86_64.manylinux_2_28_x86_64.whl#sha256=ca09836949cd86dd64fe7ef224b6212531049b94b9b5b4c1d928985eff0cf0b3' \
      'https://files.pythonhosted.org/packages/27/6d/9661acc68515e02063bf582d477faa07acc10948391c1784fd2541358855/cuequivariance_ops_torch_cu12-0.10.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl#sha256=36c24d187f456f36e25cdbba475fb08297652f43415e6c183fea1af4074de652'

RUN curl -fL --retry 5 --retry-delay 2 -o /tmp/source.tar.gz "${SOURCE_ARCHIVE_URL}" && \
    echo "${SOURCE_ARCHIVE_SHA256}  /tmp/source.tar.gz" | sha256sum -c - && \
    mkdir -p /opt/fs2/source && \
    tar -xzf /tmp/source.tar.gz --strip-components=1 -C /opt/fs2/source && \
    rm /tmp/source.tar.gz && \
    uv pip install --python /opt/venv/bin/python --no-deps --no-build-isolation /opt/fs2/source

COPY runtime_probe.py /opt/fs2/runtime_probe.py
RUN mkdir -p /opt/fs2/artifacts && \
    PATH=/opt/venv/bin:${PATH} FS2_ARTIFACT_ROOT=/opt/fs2/artifacts \
      python /opt/fs2/runtime_probe.py --model boltzgen

FROM ${CUDA_RUNTIME_IMAGE} AS runtime

ARG SOURCE_REVISION
ARG SOURCE_DATE_EPOCH
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/fs2/source /opt/fs2/source
COPY --from=build /opt/fs2/runtime_probe.py /opt/fs2/runtime_probe.py
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libboost-filesystem1.83.0 libboost-system1.83.0 libffi8 libgl1 libgomp1 \
      libhdf5-103-1t64 libssl3t64 libxml2 libxslt1.1 python3 python3-dev && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 fs2 && useradd --uid 10001 --gid 10001 --create-home fs2 && \
    mkdir -p /opt/fs2/artifacts /work /tmp/fs2-cache && \
    chown -R 10001:10001 /opt/fs2/artifacts /work /tmp/fs2-cache

LABEL org.opencontainers.image.source="https://github.com/HannesStark/boltzgen" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.created="2026-05-01T14:21:49Z" \
      org.opencontainers.image.licenses="MIT" \
      ai.nebius.fs2.runtime.kind="scientific-batch" \
      ai.nebius.fs2.runtime.weights="external" \
      ai.nebius.fs2.runtime.cuda="12.6" \
      ai.nebius.fs2.runtime.pytorch="2.7.1+cu126" \
      ai.nebius.fs2.runtime.cuequivariance="0.10.0" \
      ai.nebius.fs2.runtime.base-substitution="nvcr.io/nvidia/pytorch:24.08-py3->nvidia/cuda:12.6.3-base-ubuntu24.04"

ENV PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/fs2/source/src \
    FS2_ARTIFACT_ROOT=/opt/fs2/artifacts \
    HF_HOME=/opt/fs2/artifacts/huggingface \
    XDG_CACHE_HOME=/opt/fs2/artifacts/xdg \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/fs2/runtime_probe.py", "--model", "boltzgen"]
