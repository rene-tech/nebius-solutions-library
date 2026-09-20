# Build from a committed git archive rooted at the repository. A CPU-only glibc
# image supports RDKit's binary wheels; keep chemistry out of the API image.
FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*
COPY k8s-inference/acceptance/performance-placement-20260920/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY --chown=65532:65532 k8s-inference/acceptance /workspace/k8s-inference/acceptance
COPY --chown=65532:65532 k8s-inference/models /workspace/k8s-inference/models
COPY --chown=65532:65532 k8s-inference/catalog/runtime /workspace/k8s-inference/catalog/runtime
COPY --chown=65532:65532 k8s-inference/model-artifacts /workspace/k8s-inference/model-artifacts
RUN chmod -R a+rX /workspace/k8s-inference/acceptance /workspace/k8s-inference/models \
    /workspace/k8s-inference/catalog /workspace/k8s-inference/model-artifacts
RUN python -c "from rdkit import Chem; from rdkit.Chem import QED; assert QED.qed(Chem.MolFromSmiles('CCO')) > 0"
RUN UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev \
    --directory /workspace/k8s-inference/models/general-media/lerobot-augmentation/runtime \
    && chmod -R a+rX /workspace/k8s-inference/models/general-media/lerobot-augmentation/runtime/.venv
ARG FS2_SOURCE_COMMIT
LABEL org.opencontainers.image.revision=${FS2_SOURCE_COMMIT}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/tmp/huggingface TORCH_HOME=/tmp/torch XDG_CACHE_HOME=/tmp/cache
USER 65532:65532
WORKDIR /workspace/k8s-inference
ENTRYPOINT ["python", "acceptance/performance-placement-20260920/runner.py"]
