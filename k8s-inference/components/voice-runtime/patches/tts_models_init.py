# SPDX-FileCopyrightText: Copyright (c) 2020, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Inference-only exports for the pinned resident Magpie/codec service.

The upstream package eagerly imports unrelated EasyMagpie training models and
their speech-LLM dependencies. Only package exports are restricted; the actual
Magpie and codec implementation files are unmodified.
"""

from nemo.collections.tts.models.audio_codec import AudioCodecModel
from nemo.collections.tts.models.magpietts import InferBatchOutput, MagpieTTSModel

__all__ = ["AudioCodecModel", "InferBatchOutput", "MagpieTTSModel"]
