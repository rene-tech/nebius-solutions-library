FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /usr/local/bin/uv
COPY src ./src
ADD --checksum=sha256:1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3 --chmod=0444 https://raw.githubusercontent.com/snakers4/silero-vad/be95df9152c0d7618fa1edfeb296fc3dae32376f/src/silero_vad/data/silero_vad.onnx /opt/voice-policy/silero_vad.onnx
ADD --checksum=sha256:ebedb32bbfdd9b0177b708e93b7755b2dcfff766b7d1fe6f94289aac338fc96d --chmod=0444 https://raw.githubusercontent.com/NVIDIA-NeMo/Voice-Agent/a01fa68f0907a52cca1b07115e0e37c68ca580e5/LICENSE /opt/voice-policy/LICENSE.NeMo
RUN uv sync --frozen --no-dev && useradd --uid 10001 --no-create-home workshop
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/fs2-workshop"]
