"""Do not instantiate NanoCodec's training-only WavLM discriminator.

Pinned NeMo immediately deletes this module after restoring NanoCodec, but its
constructor otherwise downloads a second, unused pretrained network. The codec
already supports discriminator=None and the caller restores strict=False.
No encoder, quantizer, decoder, checkpoint tensor or inference math changes.
"""
from pathlib import Path

import nemo.collections.tts.models.magpietts as magpie

path = Path(magpie.__file__)
source = path.read_text()
before = "            codec_model_cfg = AudioCodecModel.restore_from(codec_model_path, return_config=True)\n"
after = before + "            codec_model_cfg.discriminator = None  # inference does not use training discriminator\n"
if source.count(before) != 1:
    raise RuntimeError("pinned Magpie codec initialization changed")
path.write_text(source.replace(before, after))
