# Build context: k8s-inference/models/molecular-dynamics
# Scientific wrapper correction only. Preserve every r4 native/OS/Python layer;
# no package refresh or independent hardening change enters this successor.
FROM cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/namd-worker@sha256:1af3abab5c794c38ef19d557bd5ca0e754fd078343f36b7e41a9eeee2ac7354f
ARG SOURCE_REVISION=development
LABEL org.opencontainers.image.revision=${SOURCE_REVISION} \
      scientific-ai.nebius.com.predecessor="sha256:1af3abab5c794c38ef19d557bd5ca0e754fd078343f36b7e41a9eeee2ac7354f"
COPY namd/runtime/fs2_namd /opt/fs2/namd/fs2_namd
