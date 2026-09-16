FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /usr/local/bin/uv
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --no-create-home workshop
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/fs2-workshop"]
