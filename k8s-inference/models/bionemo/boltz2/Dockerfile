FROM pytorch/pytorch@sha256:60f22fb80755fd0b470fb47928dbd55816aa9f847edd95cf43c93253507a9ddf

ARG BOLTZ_COMMIT=b1ebfc46ecf57f5414e0d1a6f9027bbb122c53bc

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       git \
       libxext6 \
       libxrender1 \
    && git clone --filter=blob:none https://github.com/jwohlwend/boltz.git /opt/boltz \
    && git -C /opt/boltz checkout "${BOLTZ_COMMIT}" \
    && test "$(git -C /opt/boltz rev-parse HEAD)" = "${BOLTZ_COMMIT}" \
    && python -m pip install --no-cache-dir \
       /opt/boltz \
       fastapi==0.141.1 \
       huggingface-hub==0.36.2 \
       uvicorn==0.52.4 \
    && apt-get purge -y git \
    && apt-get autoremove -y \
    && apt-get clean

ENV BOLTZ_SOURCE_REVISION=b1ebfc46ecf57f5414e0d1a6f9027bbb122c53bc \
    BOLTZ_MODEL_REPOSITORY=boltz-community/boltz-2 \
    BOLTZ_MODEL_REVISION=6fdef46d763fee7fbb83ca5501ccceff43b85607 \
    BOLTZ_CONF_SHA256=090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1 \
    BOLTZ_AFF_SHA256=dcc5cd3722b1c9eaa34267e4ae32f55cbbf1963f4c19319381ccfa30fdd2ca9e \
    BOLTZ_MOLS_SHA256=39e076d96dbec6b4e86982bbda16f3a53a2a60c9bdc17828d88f6f9a0c7d1fd7 \
    BOLTZ_CACHE=/models \
    CUEQ_DEFAULT_CONFIG=1 \
    CUEQ_DISABLE_AOT_TUNING=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PYTHONPATH=/opt/boltz/src

WORKDIR /opt/fs2
COPY k8s-inference/models/bionemo/boltz2/server.py /opt/fs2/server.py

EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
