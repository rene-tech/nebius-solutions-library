FROM python:3.12.14-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.title="Scientific AI Cosmos Transfer 2.5 adapter"
RUN groupadd --gid 10001 fs2 && useradd --uid 10001 --gid 10001 --no-create-home fs2
WORKDIR /app
COPY models/general-media/cosmos-transfer25/adapter/requirements.lock .
RUN pip install --no-cache-dir --require-hashes --requirement requirements.lock
COPY models/general-media/cosmos-transfer25/adapter/app.py .
COPY models/general-media/video-augmentation/runtime/fs2_video/__init__.py fs2_video/__init__.py
COPY models/general-media/video-augmentation/runtime/fs2_video/media.py fs2_video/media.py
USER 10001:10001
ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
