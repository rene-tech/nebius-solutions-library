FROM pytorch/pytorch@sha256:60f22fb80755fd0b470fb47928dbd55816aa9f847edd95cf43c93253507a9ddf

ARG GENMOL_COMMIT=add09fc83b7255bd09c797e527c0f4b51f5fb7c1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
       libxext6 \
       libxrender1 \
    && git clone --filter=blob:none https://github.com/NVIDIA-BioNeMo/genmol.git /opt/genmol \
    && git -C /opt/genmol checkout "${GENMOL_COMMIT}" \
    && test "$(git -C /opt/genmol rev-parse HEAD)" = "${GENMOL_COMMIT}" \
    && python -m pip install --no-cache-dir \
       bionemo-moco==0.0.2.1 \
       datasets==2.18.0 \
       easydict==1.13 \
       einops==0.7.0 \
       fastapi==0.141.1 \
       hydra-core==1.3.2 \
       jaxtyping==0.3.2 \
       lightning==2.5.1 \
       numpy==1.26.4 \
       omegaconf==2.3.0 \
       pandas==2.1.0 \
       pot==0.9.6.post1 \
       rdkit==2025.9.6 \
       safe-mol==0.1.14 \
       scikit-learn==1.6.1 \
       transformers==4.52.4 \
       uvicorn==0.52.4 \
    && python -m pip install --no-cache-dir --no-deps /opt/genmol \
    && apt-get purge -y git \
    && apt-get autoremove -y \
    && apt-get clean

ENV GENMOL_SOURCE_REVISION=add09fc83b7255bd09c797e527c0f4b51f5fb7c1 \
    GENMOL_MODEL_REPOSITORY=nvidia/NV-GenMol-89M-v2 \
    GENMOL_MODEL_REVISION=2acccbd6eee62f2a90334ccf14dc7d2b17ef9e80 \
    GENMOL_MODEL_SHA256=7ef31ef5652574b6c108231a0d67d6d871b4df3f780e43c5a4dcc2491bc8d44a \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PYTHONPATH=/opt/genmol/src \
    TOKENIZERS_PARALLELISM=false

WORKDIR /opt/fs2
COPY k8s-inference/models/bionemo/genmol/server.py /opt/fs2/server.py

EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
