# syntax=docker/dockerfile:1
# Extend the exact existing runtime; keep upstream source and model weights pinned.
ARG BASE_IMAGE=cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/h100-fleet/diffdock@sha256:471db264f4c544e1798c090f13b6cf94b3fbd304fb2c91e9f63a3dd33c9d81d7
FROM ${BASE_IMAGE}
USER 0
COPY k8s-inference/models/structure/runtime/diffdock-seeded/patch_upstream.py /opt/fs2/runtime/patches/seed_conformers.py
RUN python3 /opt/fs2/runtime/patches/seed_conformers.py --upstream /opt/fs2/model/upstream
COPY --chown=10001:10001 k8s-inference/models/structure/runtime/adapters/diffdock.py /opt/fs2/runtime/adapters/diffdock.py
USER 10001:10001
