# Runtime qualification image; not yet a publicly exposed speech service.
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime@sha256:417bd75df6365104c283ea4c1651fb3530d9eb5a4c2fafa51943cff2a94e6385
ENV PYTHONUNBUFFERED=1 HF_HOME=/cache/huggingface NUMBA_CACHE_DIR=/cache/numba
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg libsndfile1 espeak-ng build-essential \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:bef3462da5a6f44a12309bec70359de427f9f31648177bff6ad21cb3afdd64bf \
    https://codeload.github.com/NVIDIA/NeMo/tar.gz/3b08b2acacc13ec1268e53653346266202b2335f /tmp/nemo.tgz
RUN mkdir /opt/nemo && tar -xzf /tmp/nemo.tgz --strip-components=1 -C /opt/nemo && rm /tmp/nemo.tgz
WORKDIR /opt/fs2-speech
COPY requirements-nemo.lock /opt/fs2-speech/requirements-nemo.lock
RUN python -m pip install --no-cache-dir -r requirements-nemo.lock \
    && python -m pip install --no-cache-dir --no-deps /opt/nemo \
    && python -c 'from nemo.collections.asr.inference.factory.pipeline_builder import PipelineBuilder'
COPY pyproject.toml /opt/fs2-speech/pyproject.toml
COPY src /opt/fs2-speech/src
RUN python -m pip install --no-cache-dir --no-deps . \
    && python -m pip check \
    && python -m pip freeze > /opt/fs2-speech/installed-requirements.txt \
    && useradd --uid 10001 --create-home speech \
    && mkdir -p /cache/huggingface /cache/numba && chown -R speech:speech /cache
USER 10001
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      org.opencontainers.image.revision=$SOURCE_REVISION
ENTRYPOINT ["fs2-speech-probe"]
