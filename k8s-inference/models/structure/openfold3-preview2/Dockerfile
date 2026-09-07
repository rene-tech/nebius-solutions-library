# This reuses dependencies only. The installed OpenBind package is replaced by
# the explicitly compatible Preview2 package, and distinct weights are baked in.
FROM cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/openfold3-upstream@sha256:6b15da4b2258c0c385adc1dbc7799493f3768cb4881f7990cb957f2c3b6759e4
USER root
WORKDIR /opt/fs2/openfold3-preview2
ADD --checksum=sha256:44040099bd7828dd3dd9574069187f5e89b49ce11bbd640877a4d0b35292442a https://files.pythonhosted.org/packages/af/4a/25e824344df92f604aa09022894efea1504c40e695143a49d481c823b12e/openfold3-0.4.2-py3-none-any.whl /tmp/openfold3-0.4.2-py3-none-any.whl
RUN /bin/bash -ec 'source /opt/fs2/activate.sh; python -m pip install --no-deps --force-reinstall /tmp/openfold3-0.4.2-py3-none-any.whl'
COPY fetch_artifacts.py /opt/fs2/openfold3-preview2/fetch_artifacts.py
RUN /bin/bash -ec 'source /opt/fs2/activate.sh; python fetch_artifacts.py'
COPY server.py /opt/fs2/openfold3-preview2/server.py
ENV FS2_RUNTIME_ID=openfold3-preview2 FS2_SOURCE_REVISION=4a0eaeaeae8ca1d815d0a97d8eb45d639b91a47e FS2_MODEL_DIR=/opt/fs2/openfold3-preview2-artifacts FS2_OPENFOLD3_CCD_PATH=/opt/fs2/openfold3-preview2-artifacts/components.bcif OPENFOLD_CACHE=/tmp/openfold-cache PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.revision="4a0eaeaeae8ca1d815d0a97d8eb45d639b91a47e" ai.nebius.fs2.runtime.id="openfold3-preview2" ai.nebius.fs2.model.parameters="of3-p2-155k" ai.nebius.fs2.database.policy="baked-ccd" ai.nebius.fs2.artifact.policy="baked-preview2-weights" ai.nebius.fs2.runtime.origin="upstream-source-not-nim"
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/bin/bash", "-ec", "source /opt/fs2/activate.sh; export TRITON_CACHE_DIR=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/triton TORCH_EXTENSIONS_DIR=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/torch-extensions XDG_CACHE_HOME=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/xdg CUDA_CACHE_PATH=${FS2_RUNTIME_CACHE_ROOT:-/tmp/of3-cache}/cuda; exec python /opt/fs2/openfold3-preview2/server.py"]
