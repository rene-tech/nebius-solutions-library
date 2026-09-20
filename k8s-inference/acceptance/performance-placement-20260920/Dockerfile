# Build from a committed git archive rooted at the repository. Reuse the exact
# serving release's locked Python dependencies; no CUDA runtime is required.
ARG CONTROL_PLANE_IMAGE
FROM ${CONTROL_PLANE_IMAGE}
USER 0:0
# Exact decoder version for whole-video semantic validation (not only MP4 magic).
RUN apk add --no-cache 'ffmpeg=8.0.1-r1'
COPY --chown=65532:65532 k8s-inference/acceptance /workspace/k8s-inference/acceptance
COPY --chown=65532:65532 k8s-inference/models /workspace/k8s-inference/models
COPY --chown=65532:65532 k8s-inference/catalog/runtime /workspace/k8s-inference/catalog/runtime
COPY --chown=65532:65532 k8s-inference/model-artifacts /workspace/k8s-inference/model-artifacts
RUN chmod -R a+rX /workspace/k8s-inference/acceptance /workspace/k8s-inference/models \
    /workspace/k8s-inference/catalog /workspace/k8s-inference/model-artifacts
ARG FS2_SOURCE_COMMIT
LABEL org.opencontainers.image.revision=${FS2_SOURCE_COMMIT}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /workspace/k8s-inference
ENTRYPOINT ["python", "acceptance/performance-placement-20260920/runner.py"]
