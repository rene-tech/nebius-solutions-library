FROM python@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

RUN python -m pip install --no-cache-dir \
      fastapi==0.141.1 \
      pydantic==2.13.4 \
      requests==2.32.5 \
      uvicorn==0.52.4

ENV COLABFOLD_SOURCE_REVISION=0c788a0e8dca909f2c669784f9dd38e8c9682ff0 \
    COLABFOLD_API=https://api.colabfold.com \
    MSA_CACHE=/cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/fs2
COPY k8s-inference/models/bionemo/msa-search-pdb70/server.py /opt/fs2/server.py

EXPOSE 8000
ENTRYPOINT ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
