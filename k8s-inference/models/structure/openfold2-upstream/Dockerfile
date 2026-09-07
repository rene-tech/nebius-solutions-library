# syntax=docker/dockerfile:1.7
# The model checkpoint is a gated Hugging Face artifact and is deliberately
# supplied as a named BuildKit context rather than copied into git:
#   --build-context openfold2_artifacts=/private/path/containing/checkpoint
FROM pytorch/pytorch@sha256:60f22fb80755fd0b470fb47928dbd55816aa9f847edd95cf43c93253507a9ddf

ARG OPENFOLD_SOURCE_REVISION=be2ec1841f16c966c65ae0e7599ebbadc725757d

USER root
WORKDIR /opt/openfold

ADD --checksum=sha256:238b1d6ba21066e594bc6116226b00472fbab138ea85d672b85cbc43f4ce1820 \
    https://github.com/aqlaboratory/openfold/archive/be2ec1841f16c966c65ae0e7599ebbadc725757d.tar.gz \
    /tmp/openfold.tar.gz
RUN tar -xzf /tmp/openfold.tar.gz --strip-components=1 -C /opt/openfold \
    && rm /tmp/openfold.tar.gz \
    && test "$(git config --global --get safe.directory 2>/dev/null || true)" = "" \
    && test -f /opt/openfold/openfold/model/model.py \
    && python -m pip install --no-cache-dir \
       biopython==1.85 \
       dm-tree==0.1.9 \
       ml-collections==1.1.0 \
       modelcif==1.5 \
       pandas==2.3.3 \
       scipy==1.15.3

ADD --checksum=sha256:24510899eeb49167cffedec8fa45363a4d08279c0c637a403b452f7d0ac09451 \
    https://git.scicore.unibas.ch/schwede/openstructure/-/raw/7102c63615b64735c4941278d92b554ec94415f8/modules/mol/alg/src/stereo_chemical_props.txt \
    /opt/openfold/openfold/resources/stereo_chemical_props.txt

COPY --from=openfold2_artifacts /finetuning_no_templ_ptm_1.pt \
    /opt/fs2/openfold2-artifacts/finetuning_no_templ_ptm_1.pt
RUN echo "1a179937a6f61143c41f8b6a0b763ac1f4762c9dc63d2fb151240930c110bee1  /opt/fs2/openfold2-artifacts/finetuning_no_templ_ptm_1.pt" \
      | sha256sum --check --strict \
    && chmod -R a+rX,a-w /opt/openfold /opt/fs2/openfold2-artifacts

COPY k8s-inference/models/structure/openfold2-upstream/attn_core_inplace_cuda.py \
    /opt/fs2/openfold2/attn_core_inplace_cuda.py
COPY k8s-inference/models/structure/openfold2-upstream/server.py \
    /opt/fs2/openfold2/server.py

ENV FS2_OPENFOLD2_CHECKPOINT=/opt/fs2/openfold2-artifacts/finetuning_no_templ_ptm_1.pt \
    FS2_OPENFOLD2_SOURCE_REVISION=be2ec1841f16c966c65ae0e7599ebbadc725757d \
    FS2_OPENFOLD2_MODEL_REVISION=6344b2a680cbbae20b8afdb7f154228cc5b0cb27 \
    HOME=/tmp/fs2-home \
    PYTHONPATH=/opt/fs2/openfold2:/opt/openfold \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.source="https://github.com/aqlaboratory/openfold" \
      org.opencontainers.image.revision="be2ec1841f16c966c65ae0e7599ebbadc725757d" \
      org.opencontainers.image.licenses="Apache-2.0 AND CC-BY-4.0" \
      ai.nebius.fs2.runtime.id="openfold2-upstream-portable" \
      ai.nebius.fs2.runtime.origin="upstream-source-not-nim" \
      ai.nebius.fs2.model.repository="nz/OpenFold" \
      ai.nebius.fs2.model.revision="6344b2a680cbbae20b8afdb7f154228cc5b0cb27" \
      ai.nebius.fs2.model.checkpoint.sha256="1a179937a6f61143c41f8b6a0b763ac1f4762c9dc63d2fb151240930c110bee1"

RUN mkdir -p /tmp/fs2-home /tmp/fs2-cache \
    && chown -R 10001:10001 /tmp/fs2-home /tmp/fs2-cache /opt/fs2/openfold2

USER 10001:10001
WORKDIR /opt/fs2/openfold2
EXPOSE 8000
ENTRYPOINT ["python", "/opt/fs2/openfold2/server.py"]
