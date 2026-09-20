FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime@sha256:c8268a92a69bd500f8be0e665b2630ee006dadaf7bfbc24249141b15ff622755

ARG SAM2_CHECKPOINT_SHA256
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.title="Nebius Scientific AI SAM 2.1"
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    SAM2_BUILD_CUDA=0 \
    SAM2_CHECKPOINT=/opt/sam2/checkpoints/sam2.1_hiera_large.pt \
    SAM2_CHECKPOINT_SHA256=${SAM2_CHECKPOINT_SHA256}
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 fs2 && useradd --uid 10001 --gid 10001 --no-create-home fs2
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt
COPY sam2.1_hiera_large.pt /opt/sam2/checkpoints/sam2.1_hiera_large.pt
RUN test -n "$SAM2_CHECKPOINT_SHA256" \
    && echo "$SAM2_CHECKPOINT_SHA256  /opt/sam2/checkpoints/sam2.1_hiera_large.pt" | sha256sum -c -
COPY app.py .
RUN chown -R 10001:10001 /app /opt/sam2
USER 10001:10001
ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
