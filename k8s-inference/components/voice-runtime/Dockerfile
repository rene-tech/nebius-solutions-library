# Reuse the already-qualified immutable Torch/CUDA/NeMo dependency environment.
FROM cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-speech-runtime@sha256:9c9f34b8682bf8d22536d2c342238eeced9c9428f5392e539bb65d8e12d0d491
USER root
ADD --checksum=sha256:b4c7b1d2c01dae5c87437212cede3181e385e1f3294461be7bec73566b067b4d \
    https://api.github.com/repos/NVIDIA-NeMo/Speech/tarball/3d91009e8f2ef690112eabeb6907da00ea308d7f /tmp/speech.tgz
ADD --checksum=sha256:66a85d7e5c1afd9f9e436fcbbd8c8605305c2f133bbc21b63d68ad6cba6262e7 \
    https://codeload.github.com/NVIDIA-NeMo/Voice-Agent/tar.gz/a01fa68f0907a52cca1b07115e0e37c68ca580e5 /tmp/voice.tgz
RUN mkdir -p /opt/nemo-speech /opt/voice-upstream /opt/fs2_voice_upstream \
    && tar -xzf /tmp/speech.tgz --strip-components=1 -C /opt/nemo-speech \
    && tar -xzf /tmp/voice.tgz --strip-components=1 -C /opt/voice-upstream \
    && cp /opt/voice-upstream/nemo_voice_agent/pipecat/services/nemo/streaming_asr.py \
          /opt/voice-upstream/nemo_voice_agent/pipecat/services/nemo/streaming_diar.py \
          /opt/voice-upstream/nemo_voice_agent/pipecat/services/nemo/utils.py /opt/fs2_voice_upstream/ \
    && cp /opt/voice-upstream/LICENSE /opt/fs2_voice_upstream/LICENSE \
    && touch /opt/fs2_voice_upstream/__init__.py \
    && sed -i 's/from nemo_voice_agent.pipecat.services.nemo.utils import/from fs2_voice_upstream.utils import/' \
       /opt/fs2_voice_upstream/streaming_asr.py /opt/fs2_voice_upstream/streaming_diar.py \
    && python -m pip install --no-cache-dir --no-deps /opt/nemo-speech
WORKDIR /opt/fs2-voice
COPY requirements-tts.lock ./requirements-tts.lock
RUN python -m pip install --no-cache-dir -r requirements-tts.lock \
    && python -c 'from nemo.collections.tts.models import MagpieTTSModel' \
    && python -c 'import pyopenjtalk; pyopenjtalk.g2p("こんにちは")'
COPY pyproject.toml ./pyproject.toml
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps . \
    && python -m pip freeze > installed-requirements.txt
ENV PYTHONPATH=/opt HF_HOME=/opt/fs2-voice/hf NLTK_DATA=/opt/fs2-voice/nltk \
    TMPDIR=/cache OMP_NUM_THREADS=2 FS2_VOICE_CHECKPOINT_DIR=/opt/fs2-voice/weights
# Download only tokenization data, never an unused text model.
RUN python -c 'from huggingface_hub import snapshot_download; snapshot_download("google/byt5-small", revision="68377bdc18a2ffec8a0533fef03b1c513a4dd49d", allow_patterns=["config.json", "tokenizer*", "special_tokens_map.json"])' \
    && python -c 'import nltk; nltk.download("punkt_tab", download_dir="/opt/fs2-voice/nltk"); nltk.download("cmudict", download_dir="/opt/fs2-voice/nltk")' \
    && chmod -R a+rX /opt/fs2-voice /opt/fs2_voice_upstream
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/rene-tech/nebius-solutions-library" \
      org.opencontainers.image.revision=$SOURCE_REVISION \
      ai.fs2.nemo.revision="3d91009e8f2ef690112eabeb6907da00ea308d7f" \
      ai.fs2.voice-agent.revision="a01fa68f0907a52cca1b07115e0e37c68ca580e5"
USER 10001
ENTRYPOINT ["fs2-voice-serve"]
