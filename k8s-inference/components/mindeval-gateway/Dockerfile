FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /data/mindeval && chown -R 10001:10001 /data /app
USER 10001:10001
EXPOSE 8080
CMD ["fs2-mindeval-serve"]
