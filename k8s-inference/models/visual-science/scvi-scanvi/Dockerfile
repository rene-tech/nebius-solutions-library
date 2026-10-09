FROM cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/visual-science/scvi-scanvi@sha256:bf25f8253407fee4c8b0e0c425cbf6efd0e9ad8b185145c3548fce93636760c9

COPY requirements.txt /app/requirements.fs2.txt
RUN python -m pip install --no-cache-dir --upgrade -r /app/requirements.fs2.txt \
    && python -m pip check \
    && python -c "import anndata,fastapi,scvi,starlette,umap; print(anndata.__version__, fastapi.__version__, scvi.__version__, starlette.__version__, umap.__version__)"

LABEL org.opencontainers.image.title="Nebius Scientific AI scVI/scANVI" \
      org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      ai.nebius.fs2-serve.model-id="scvi-scanvi" \
      ai.nebius.fs2-serve.model-revision="56520c713eb1d2b245c72a0f60bc393b74198c91" \
      ai.nebius.fs2-serve.parent-image-digest="sha256:bf25f8253407fee4c8b0e0c425cbf6efd0e9ad8b185145c3548fce93636760c9" \
      ai.nebius.fs2-serve.runtime-source-sha256="dd35a95403aa92a29beb8894fbcbdac69907ad5d6ba9b72a5e1e1113480c931d"

COPY app.py /app/app.py
