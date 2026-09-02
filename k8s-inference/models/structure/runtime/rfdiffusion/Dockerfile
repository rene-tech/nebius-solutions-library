FROM registry.example.invalid/k8s-inference/models/rfdiffusion@sha256:462a4962f1fdbb7c3b2a981ce1cbea19aecd3a6a54a2b46050a96d842b6b75dc

ARG SOURCE_REVISION=86507b6538f51fce57b5a72477165f03999ed7ae
ARG SOURCE_ARCHIVE_SHA256=068c5efc5097c71c2dde521af685d1c25618ee3e473c46a9b1443643860c4276
ARG BASE_CHECKPOINT_SHA256=0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca

USER 0
RUN set -eux; \
    mkdir -p /opt/fs2/rfdiffusion /opt/fs2/models /opt/fs2/runtime; \
    curl -fL --retry 5 --retry-delay 2 \
      -o /tmp/rfdiffusion.tar.gz \
      "https://github.com/RosettaCommons/RFdiffusion/archive/${SOURCE_REVISION}.tar.gz"; \
    echo "${SOURCE_ARCHIVE_SHA256}  /tmp/rfdiffusion.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/rfdiffusion.tar.gz --strip-components=1 -C /opt/fs2/rfdiffusion; \
    curl -fL --retry 5 --retry-delay 2 \
      -o /opt/fs2/models/Base_ckpt.pt \
      http://files.ipd.uw.edu/pub/RFdiffusion/6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt; \
    echo "${BASE_CHECKPOINT_SHA256}  /opt/fs2/models/Base_ckpt.pt" | sha256sum -c -; \
    python3 -m pip install --no-deps /opt/fs2/rfdiffusion; \
    rm -f /tmp/rfdiffusion.tar.gz; \
    chown -R 10001:10001 /opt/fs2; \
    chmod -R a-w /opt/fs2/rfdiffusion /opt/fs2/models

COPY --chown=10001:10001 server.py run_upstream.py /opt/fs2/runtime/
COPY --chown=10001:10001 bin/rfdiffusion-batch /opt/fs2/bin/rfdiffusion-batch
RUN chmod 0555 /opt/fs2/bin/rfdiffusion-batch

LABEL org.opencontainers.image.source="https://github.com/RosettaCommons/RFdiffusion" \
      org.opencontainers.image.revision="86507b6538f51fce57b5a72477165f03999ed7ae" \
      org.opencontainers.image.licenses="BSD-3-Clause" \
      ai.nebius.fs2.model.revision="86507b6538f51fce57b5a72477165f03999ed7ae" \
      ai.nebius.fs2.model.checkpoint.sha256="0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca" \
      ai.nebius.fs2.runtime.relationship="upstream-direct-profile-independent" \
      ai.nebius.fs2.vendor-baseline.state="incompatible-sm103"

ENV PYTHONPATH=/opt/fs2/rfdiffusion \
    DGLBACKEND=pytorch \
    FS2_RUNTIME_HOME=/tmp/fs2-home \
    XDG_CACHE_HOME=/tmp/fs2-cache \
    XDG_CONFIG_HOME=/tmp/fs2-config \
    MPLCONFIGDIR=/tmp/fs2-matplotlib \
    FS2_PORT=8000 \
    FS2_RF_TIMEOUT_SECONDS=900

USER 10001:10001
WORKDIR /opt/fs2/rfdiffusion
ENTRYPOINT []
CMD ["python3", "/opt/fs2/runtime/server.py"]
