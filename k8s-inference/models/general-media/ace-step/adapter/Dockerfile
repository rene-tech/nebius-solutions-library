FROM python:3.12.14-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.title="Nebius Scientific AI ACE-Step 1.5 adapter"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN groupadd --gid 10001 fs2 && useradd --uid 10001 --gid 10001 --no-create-home fs2
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY app.py /app/app.py

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
