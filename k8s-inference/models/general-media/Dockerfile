FROM registry.example.invalid/k8s-inference/models/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635

ARG DIFFUSERS_VERSION=0.35.1
ARG HUGGINGFACE_HUB_VERSION=0.36.0
ARG MONAI_VERSION=1.5.1
ARG NIBABEL_VERSION=5.3.2
ARG PILLOW_VERSION=11.3.0
ARG TRANSFORMERS_VERSION=4.57.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1

RUN python3 -m venv --system-site-packages /opt/fs2-media-venv \
    && /opt/fs2-media-venv/bin/python -m pip install --no-cache-dir --no-deps \
       "diffusers==${DIFFUSERS_VERSION}" \
       "huggingface-hub==${HUGGINGFACE_HUB_VERSION}" \
       "monai==${MONAI_VERSION}" \
       "nibabel==${NIBABEL_VERSION}" \
       "Pillow==${PILLOW_VERSION}" \
       "transformers==${TRANSFORMERS_VERSION}" \
    && /opt/fs2-media-venv/bin/python -c \
       'import diffusers, huggingface_hub, monai, nibabel, PIL, torch, transformers; from diffusers import StableDiffusionXLPipeline; assert diffusers.__version__ == "0.35.1"; assert huggingface_hub.__version__ == "0.36.0"; assert monai.__version__ == "1.5.1"; assert nibabel.__version__ == "5.3.2"; assert PIL.__version__ == "11.3.0"; assert transformers.__version__ == "4.57.1"; print(torch.__version__, torch.version.cuda, StableDiffusionXLPipeline.__name__)'

COPY sdxl_server.py nv_segment_ct_server.py /opt/fs2-media/

LABEL org.opencontainers.image.title="FS2 general and medical media runtime" \
      org.opencontainers.image.source="https://github.com/nebius/nebius-solutions-library" \
      ai.nebius.fs2.sdxl.revision="462165984030d82259a11f4367a4eed129e94a7b" \
      ai.nebius.fs2.nv-segment-ct.revision="afb51518689f71e6abb367ee6301b2cd0225c66a" \
      ai.nebius.fs2.cuda="13.0.2"

EXPOSE 8000
ENTRYPOINT ["/opt/fs2-media-venv/bin/python"]
CMD ["/opt/fs2-media/sdxl_server.py"]
