# syntax=docker/dockerfile:1
FROM pytorch/pytorch@sha256:60f22fb80755fd0b470fb47928dbd55816aa9f847edd95cf43c93253507a9ddf

ARG BIONEMO_COMMIT=9ba9b2c337917fc815ffbd5d99f78d9db7904493

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
       libxext6 \
       libxrender1 \
    && git clone --filter=blob:none https://github.com/NVIDIA/bionemo-framework.git /opt/bionemo-source \
    && git -C /opt/bionemo-source checkout "${BIONEMO_COMMIT}" \
    && test "$(git -C /opt/bionemo-source rev-parse HEAD)" = "${BIONEMO_COMMIT}" \
    && python -m pip install --no-cache-dir \
       fastapi==0.141.1 \
       pydantic==2.13.4 \
       rdkit==2025.9.6 \
       uvicorn==0.52.4 \
    && apt-get purge -y git \
    && apt-get autoremove -y \
    && apt-get clean

COPY --from=molmim_artifact /molmim_70m_24_3.nemo /opt/artifacts/molmim_70m_24_3.nemo

ENV BIONEMO_SOURCE_REVISION=9ba9b2c337917fc815ffbd5d99f78d9db7904493 \
    MOLMIM_NEMO=/opt/artifacts/molmim_70m_24_3.nemo \
    MOLMIM_NEMO_SHA256=10522c9db6018c355313f9f01a0edea2b021ddc0a5a22ae4540cbf5bdafbd1f5 \
    MOLMIM_WEIGHTS_SHA256=bc246330d019b18a4f0acaa88b65d64eaf6e5e940f1ec82e1ba097aff1fdddee \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/fs2
COPY k8s-inference/models/bionemo/molmim/server.py /opt/fs2/server.py

EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
