# syntax=docker/dockerfile:1.12@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25

# This target is used only by the repository-root context verification gate.
# It materializes BuildKit's post-.dockerignore view and is not an ancestor of
# the production image.
FROM scratch AS context-audit
COPY . /

FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv

FROM python:3.13.15-alpine3.23@sha256:7ea3f82de8ea6d4fb7e5d2bbe3fe3c9d931700b7a529f1fe5769e42abe514ca1 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /workspace/k8s-inference/components/control-plane
COPY --from=uv /uv /usr/local/bin/uv
COPY k8s-inference/components/control-plane/pyproject.toml k8s-inference/components/control-plane/uv.lock k8s-inference/components/control-plane/README.md ./
COPY k8s-inference/components/control-plane/src ./src
COPY k8s-inference/components/control-plane/migrations ./migrations
COPY k8s-inference/components/control-plane/contracts ./contracts
COPY k8s-inference/catalog/runtime /workspace/k8s-inference/catalog/runtime

# The canonical catalog is a nested immutable tree and cannot be represented by
# a single Kubernetes ConfigMap without flattening paths. Build the exact
# runtime tree into the image, including only repository files whose digests are
# referenced by semantic-validator contracts.
COPY k8s-inference/catalog/runtime/catalog.json /workspace/runtime-catalog/catalog.json
COPY k8s-inference/catalog/runtime/pyproject.toml k8s-inference/catalog/runtime/uv.lock /workspace/runtime-catalog/
COPY k8s-inference/catalog/runtime/contracts /workspace/runtime-catalog/contracts
COPY k8s-inference/catalog/runtime/kubernetes /workspace/runtime-catalog/kubernetes
COPY k8s-inference/catalog/runtime/models /workspace/runtime-catalog/models
COPY k8s-inference/catalog/runtime/schema /workspace/runtime-catalog/schema
COPY k8s-inference/catalog/runtime/sql /workspace/runtime-catalog/sql
COPY k8s-inference/catalog/runtime/validators /workspace/runtime-catalog/validators
COPY k8s-inference/catalog/runtime/packaged-repository /workspace/runtime-catalog/packaged-repository

# Runtime dependencies are installed only from uv.lock. The application is
# then built as a wheel with a hash-bearing build constraint exported from the
# same lock and installed without dependency resolution or an index lookup.
RUN uv sync --frozen --no-dev --no-install-project \
    && uv sync --check --frozen --no-dev --no-install-project \
    && uv export --frozen --only-group build --no-emit-project --no-annotate --output-file /tmp/build-constraints.txt \
    && uv build --wheel --out-dir /tmp/dist --build-constraints /tmp/build-constraints.txt --require-hashes \
    && uv pip install --python .venv/bin/python --no-deps --no-index /tmp/dist/fs2_serve_control_plane-*.whl \
    && uv pip check --python .venv/bin/python \
    && .venv/bin/python -I -c "from pathlib import Path; import fs2_serve, fs2_serve_catalog; from fs2_serve.settings import Settings; from fs2_serve_catalog.loader import load_catalog; migration_dir=Settings.model_fields['migrations_dir'].default; assert isinstance(migration_dir, Path); assert migration_dir.parent == Path(fs2_serve.__file__).resolve().parent; assert len(list(migration_dir.glob('[0-9][0-9][0-9][0-9]_*.sql'))) == 19; assert load_catalog(Path('/workspace/runtime-catalog'), repo_root=Path('/workspace/runtime-catalog/packaged-repository')).records['qwen3-8b'].model_id == 'qwen3-8b'" \
    && .venv/bin/fs2-serve --help >/dev/null

FROM python:3.13.15-alpine3.23@sha256:7ea3f82de8ea6d4fb7e5d2bbe3fe3c9d931700b7a529f1fe5769e42abe514ca1

ENV PATH="/workspace/k8s-inference/components/control-plane/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The digest-bound Python base predates these Alpine security fixes. Install
# only the three exact packages; do not run apk upgrade or select a floating
# version during an application build.
RUN apk add --no-cache \
    'libcrypto3=3.5.8-r0' \
    'libssl3=3.5.8-r0' \
    'sqlite-libs=3.53.4-r0' \
    && rm -rf \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.13 \
        /usr/local/lib/python3.13/ensurepip \
        /usr/local/lib/python3.13/site-packages/pip \
        /usr/local/lib/python3.13/site-packages/pip-26.2.1.dist-info

WORKDIR /workspace/k8s-inference/components/control-plane
COPY --from=builder --chown=65532:65532 /workspace/k8s-inference/components/control-plane/.venv ./.venv
COPY --from=builder --chown=65532:65532 /workspace/runtime-catalog /opt/fs2/catalog

ARG FS2_SOURCE_COMMIT
ARG FS2_SOURCE_TREE
ARG FS2_UV_LOCK_SHA256
ARG FS2_DOCKERFILE_SHA256
ARG FS2_CONTEXT_POLICY_SHA256
RUN test -n "$FS2_SOURCE_COMMIT" \
    && test -n "$FS2_SOURCE_TREE" \
    && test -n "$FS2_UV_LOCK_SHA256" \
    && test -n "$FS2_DOCKERFILE_SHA256" \
    && test -n "$FS2_CONTEXT_POLICY_SHA256"
LABEL org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
    org.opencontainers.image.revision="$FS2_SOURCE_COMMIT" \
    org.opencontainers.image.base.name="docker.io/library/python:3.13.15-alpine3.23" \
    org.opencontainers.image.base.digest="sha256:7ea3f82de8ea6d4fb7e5d2bbe3fe3c9d931700b7a529f1fe5769e42abe514ca1" \
    ai.nebius.fs2-serve.source-tree="$FS2_SOURCE_TREE" \
    ai.nebius.fs2-serve.uv-lock-sha256="$FS2_UV_LOCK_SHA256" \
    ai.nebius.fs2-serve.dockerfile-sha256="$FS2_DOCKERFILE_SHA256" \
    ai.nebius.fs2-serve.context-policy-sha256="$FS2_CONTEXT_POLICY_SHA256" \
    ai.nebius.fs2-serve.uv-base.digest="sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a"

USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["fs2-serve"]
CMD ["serve"]
