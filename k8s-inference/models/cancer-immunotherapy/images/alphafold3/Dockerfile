# AlphaFold 3 v3.0.4 academic-PoC runtime for the fs2 cancer-immunotherapy platform.
#
# Upstream AlphaFold 3 source is Apache-2.0 (google-deepmind/alphafold3).
# The AlphaFold 3 *model parameters* are NOT Apache-2.0 and are NOT part of this
# image. They are obtained by the operator directly from Google under the
# AlphaFold 3 Weights Terms of Use and are mounted at run time from the
# task-owned academic asset volume. This image also contains no AlphaFold 3
# public reference-database bytes; those are mounted from the shared
# reference-data filesystem. See contracts/ for the exact binding contracts.
#
# syntax=docker/dockerfile:1

##############################################################################
# Stage 1 - pinned upstream source
##############################################################################
FROM nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e AS source

ARG AF3_REPOSITORY=https://github.com/google-deepmind/alphafold3.git
ARG AF3_COMMIT=85c4d20505fd5cef05eac22b534d4e793971ae69
ARG AF3_TREE=efa1a376c9cf94d517d70e68425bc1ed3b17a570
ARG AF3_VERSION=3.0.4

# hadolint ignore=DL3008,DL3009
RUN DEBIAN_FRONTEND=noninteractive apt-get update --quiet \
    && apt-get install --yes --quiet --no-install-recommends git ca-certificates

# Fetch exactly one immutable commit and prove its identity before any of it is
# used. A git commit id is a content address, so an equal id means an equal
# tree; the tree id is additionally asserted so a rewritten history cannot pass.
WORKDIR /src
RUN git init --quiet . \
    && git remote add origin "${AF3_REPOSITORY}" \
    && git fetch --quiet --depth 1 origin "${AF3_COMMIT}" \
    && git checkout --quiet FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${AF3_COMMIT}" \
    && test "$(git rev-parse 'HEAD^{tree}')" = "${AF3_TREE}" \
    && git ls-tree -r --name-only HEAD > /src.filelist \
    && rm -rf /src/.git

# Fail the build if upstream ever starts shipping parameter or database payloads
# inside its source tree, so the "no weights in image" property is enforced at
# the source boundary and not only asserted afterwards.
RUN set -eu; \
    found="$(find /src -type f \( -name '*.bin' -o -name '*.bin.zst' -o -name '*.zst' \) -print)"; \
    if [ -n "${found}" ]; then echo "unexpected payload in upstream source: ${found}" >&2; exit 1; fi

##############################################################################
# Stage 2 - HMMER 3.4 with the upstream jackhmmer sequence-limit patch
##############################################################################
FROM nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e AS hmmer

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG HMMER_URL=http://eddylab.org/software/hmmer/hmmer-3.4.tar.gz
ARG HMMER_SHA256=ca70d94fd0cf271bd7063423aabb116d42de533117343a9b27a65c17ff06fbf3

# hadolint ignore=DL3008,DL3009
RUN DEBIAN_FRONTEND=noninteractive apt-get update --quiet \
    && apt-get install --yes --quiet --no-install-recommends \
       wget ca-certificates gcc g++ make patch

# eddylab.org serves plain HTTP only; integrity comes from the pinned SHA-256
# rather than from the transport, exactly as upstream does.
WORKDIR /hmmer_build
RUN wget --quiet --output-document hmmer-3.4.tar.gz "${HMMER_URL}" \
    && echo "${HMMER_SHA256}  hmmer-3.4.tar.gz" | sha256sum --check --strict \
    && tar zxf hmmer-3.4.tar.gz \
    && rm hmmer-3.4.tar.gz

COPY --from=source /src/docker/jackhmmer_seq_limit.patch /hmmer_build/jackhmmer_seq_limit.patch
RUN patch -p0 < jackhmmer_seq_limit.patch

WORKDIR /hmmer_build/hmmer-3.4
RUN ./configure --prefix /hmmer \
    && make -j"$(nproc)" \
    && make install

WORKDIR /hmmer_build/hmmer-3.4/easel
RUN make install

##############################################################################
# Stage 3 - AlphaFold 3 python environment
##############################################################################
FROM nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e AS venv

# bash rather than dash: the dependency sync raises its own file-descriptor
# soft limit, and ulimit is not defined by POSIX sh.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG AF3_VERSION=3.0.4

# hadolint ignore=DL3008,DL3009
RUN DEBIAN_FRONTEND=noninteractive apt-get update --quiet \
    && apt-get install --yes --quiet --no-install-recommends \
       python3.12 python3.12-dev gcc g++ make zlib1g-dev git ca-certificates

COPY --from=ghcr.io/astral-sh/uv:0.9.24@sha256:e4bc0bb4310505e7104f06430c0a2271a58f60a756a3b2c287bb409890081b2a /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1
ENV UV_PROJECT_ENVIRONMENT=/alphafold3_venv
ENV PATH="/alphafold3_venv/bin:${PATH}"
RUN uv venv "${UV_PROJECT_ENVIRONMENT}"

COPY --from=source /src /app/alphafold
WORKDIR /app/alphafold

# The upstream version is derived by setuptools-scm from git metadata, which the
# source stage deliberately removed. Upstream tag v3.0.4 points at exactly the
# pinned commit, so the version is asserted rather than guessed, and a silent
# fall back to pyproject's placeholder version is treated as a build failure.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${AF3_VERSION}
# The JAX CUDA wheels unpack tens of thousands of headers, and uv compiles
# bytecode across them in parallel. Container defaults give only 1024 open file
# descriptors, which uv exhausts. The soft limit is raised towards the already
# permitted hard limit inside the step, so the build does not depend on the
# caller remembering to pass --ulimit.
RUN --mount=type=cache,target=/root/.cache/uv \
    ulimit -n 65536 \
    && UV_LINK_MODE=copy uv sync --frozen --no-dev --no-editable

# Chemical Component Dictionary derived data. This is public chemical reference
# data shipped by the libcifpp dependency, not model parameters, and it is built
# offline from the already installed package data.
RUN /alphafold3_venv/bin/build_data

RUN set -eu; \
    installed="$(/alphafold3_venv/bin/python3 -c 'import importlib.metadata as m; print(m.version("alphafold3"))')"; \
    if [ "${installed}" != "${AF3_VERSION}" ]; then \
      echo "alphafold3 version mismatch: built ${installed}, expected ${AF3_VERSION}" >&2; exit 1; \
    fi; \
    echo "${installed}" > /alphafold3_venv/.af3-version

##############################################################################
# Stage 4 - runtime
##############################################################################
FROM nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e AS runtime

ARG AF3_COMMIT=85c4d20505fd5cef05eac22b534d4e793971ae69
ARG AF3_VERSION=3.0.4
ARG FS2_ASSET_GID=65532

# hadolint ignore=DL3008
RUN DEBIAN_FRONTEND=noninteractive apt-get update --quiet \
    && apt-get install --yes --quiet --no-install-recommends \
       python3.12 zlib1g libgomp1 zstd ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=hmmer /hmmer /hmmer
COPY --from=venv /alphafold3_venv /alphafold3_venv
COPY --from=venv /app/alphafold /app/alphafold

ENV PATH="/hmmer/bin:/alphafold3_venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Upstream XLA workaround for the custom-kernel fusion rewriter compile-time
# blow-up. Kept identical to upstream so compile behaviour is not silently
# changed, and deliberately GPU-family agnostic.
ENV XLA_FLAGS="--xla_gpu_enable_triton_gemm=false"
ENV XLA_PYTHON_CLIENT_PREALLOCATE=false
ENV XLA_CLIENT_MEM_FRACTION=0.95

# Persistent auxiliary compiler caches. These hold XLA and Triton compilation
# artefacts only. They are NOT a GPU memory snapshot and must never be reported
# as one. Every path is overridable and the runtime degrades to an uncached run,
# truthfully reported, when a path is not writable.
ENV FS2_AF3_CACHE_ROOT=/cache/alphafold3
ENV FS2_AF3_JAX_CACHE_DIR=/cache/alphafold3/jax
ENV FS2_AF3_TRITON_CACHE_DIR=/cache/alphafold3/triton
ENV FS2_AF3_XDG_CACHE_DIR=/cache/alphafold3/xdg

# Per-pod scratch, deliberately separate from the shared persistent cache.
ENV HOME=/scratch
ENV TMPDIR=/scratch

# Binding locations. Nothing is baked in: these are mount points only, and the
# defaults match the canonical academic-assets subPath binding and the
# reference-data plane's own path prefix.
ENV FS2_AF3_PARAMETER_PATH=/models/af3.bin.zst
# The read-only reference-data root, not a per-bundle path. The per-bundle
# database_root is derived from the publisher receipt as
# <mount>/datasets/<bundle>/<revision>/sha256/<content_tree_sha256>, so no
# snapshot identity is ever baked into the image.
ENV FS2_AF3_REFERENCE_MOUNT=/reference-data
ENV FS2_AF3_OUTPUT_DIR=/output
ENV FS2_AF3_SOURCE_COMMIT=${AF3_COMMIT}
ENV FS2_AF3_VERSION=${AF3_VERSION}
ENV FS2_AF3_RUN_SCRIPT=/app/alphafold/run_alphafold.py
ENV FS2_AF3_ASSET_GID=${FS2_ASSET_GID}

COPY runtime/af3_runtime.py /opt/fs2/af3_runtime.py
COPY contracts/af3-runtime-source-lock.json /opt/fs2/af3-runtime-source-lock.json
COPY contracts/af3-parameter-binding.json /opt/fs2/af3-parameter-binding.json

# The pod overrides runAsUser/runAsGroup and supplies supplementalGroups 65532
# for the read-only academic claim, so every writable directory is created with
# the sticky-bit world-writable mode that /tmp itself uses. A fresh volume and
# an arbitrary non-root uid therefore both work with no init container, and no
# access to licensed bytes is granted by it: the parameter mount stays
# read-only, mode 0440, group 65532, and is never written by this runtime.
RUN set -eu; \
    install -d -m 1777 "${FS2_AF3_CACHE_ROOT}" "${FS2_AF3_JAX_CACHE_DIR}" \
                       "${FS2_AF3_TRITON_CACHE_DIR}" "${FS2_AF3_XDG_CACHE_DIR}" \
                       /scratch "${FS2_AF3_OUTPUT_DIR}"; \
    install -d -m 0555 /models /reference-data; \
    python3.12 -c 'import compileall,sys; sys.exit(0 if compileall.compile_file("/opt/fs2/af3_runtime.py", quiet=2) else 1)'

# Last line of defence for the "no licensed payload and no reference-database
# bytes in the image" property: fail the build if any parameter-shaped or
# reference-database-shaped file exists anywhere in the image filesystem.
RUN set -eu; \
    payload="$(find / -xdev -type f \( -name '*.bin.zst' -o -name 'af3.bin*' \) -print 2>/dev/null || true)"; \
    if [ -n "${payload}" ]; then echo "licensed payload present in image: ${payload}" >&2; exit 1; fi; \
    for db in bfd-first_non_consensus_sequences.fasta mgy_clusters_2022_05.fa \
              uniprot_all_2021_04.fa uniref90_2022_05.fa \
              nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta \
              rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta \
              rnacentral_active_seq_id_90_cov_80_linclust.fasta \
              pdb_seqres_2022_09_28.fasta; do \
      hit="$(find / -xdev -type f -name "${db}" -print 2>/dev/null || true)"; \
      if [ -n "${hit}" ]; then echo "reference database present in image: ${hit}" >&2; exit 1; fi; \
    done; \
    if [ -n "$(find / -xdev -type d -name mmcif_files -print 2>/dev/null || true)" ]; then \
      echo "reference mmcif_files directory present in image" >&2; exit 1; \
    fi

USER 1001:1001
WORKDIR /app/alphafold

LABEL org.opencontainers.image.title="fs2 AlphaFold 3 academic runtime" \
      org.opencontainers.image.description="AlphaFold 3 v3.0.4 inference runtime. Contains no model parameters and no reference-database bytes." \
      org.opencontainers.image.source="https://github.com/google-deepmind/alphafold3" \
      org.opencontainers.image.revision="${AF3_COMMIT}" \
      org.opencontainers.image.version="${AF3_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="nvidia/cuda:12.6.3-base-ubuntu24.04" \
      org.opencontainers.image.base.digest="sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e" \
      ai.nebius.fs2.runtime="alphafold3" \
      ai.nebius.fs2.runtime.access-class="academic-restricted" \
      ai.nebius.fs2.runtime.parameters="external-volume" \
      ai.nebius.fs2.runtime.reference-data="external-volume" \
      ai.nebius.fs2.runtime.cache-level="L1-image-local" \
      ai.nebius.fs2.runtime.gpu-family="agnostic-nvidia-sm80-plus"

ENTRYPOINT ["/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py"]
CMD ["verify"]
