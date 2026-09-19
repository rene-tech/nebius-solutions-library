FROM cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/visual-science/cellpose-cpsam-v2@sha256:b34056411192ee463539bfa105905e340fcbe16142fb8bab897d6c7a6a94a47f

COPY requirements.txt /app/requirements.fs2.txt
RUN python -m pip install --no-cache-dir --upgrade -r /app/requirements.fs2.txt \
    && python -m pip check \
    && python -c "from importlib.metadata import version; import fastapi,PIL,starlette; print(version('cellpose'), fastapi.__version__, PIL.__version__, starlette.__version__)"

LABEL org.opencontainers.image.title="Nebius Scientific AI Cellpose CPSAM v2" \
      org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      ai.nebius.fs2-serve.model-id="cellpose-cpsam-v2" \
      ai.nebius.fs2-serve.model-revision="7c61431b5fbb078f3296754bd15d9f51b320f837" \
      ai.nebius.fs2-serve.weight-sha256="0f1cc3f7ecdd8a037a57c6c48d9d8921391be4cbce3fa9f13c3e3a2e1253c667" \
      ai.nebius.fs2-serve.parent-image-digest="sha256:b34056411192ee463539bfa105905e340fcbe16142fb8bab897d6c7a6a94a47f" \
      ai.nebius.fs2-serve.runtime-source-sha256="2744f4678cfb1e20d2993bb4f8c1c5e20dcbc6d9061684dc5155558808b8dadf"

COPY app.py /app/app.py
