"""Bounded-workspace equivalent of Vortex's modal FFT state prefill.

Only independent channels are tiled. FFT length, FP32 exponentials,
complex64 transforms, recurrence state dtype and sequence order are unchanged.
"""
from __future__ import annotations

import functools


DEFAULT_CHANNEL_TILE = 256


class ModelMemoryExhausted(RuntimeError):
    """A valid request exceeded this runtime's memory, not network capacity."""

    def __init__(self, input_length: int, num_tokens: int):
        super().__init__("Evo2 GPU memory exhausted for the accepted request shape")
        self.input_length = input_length
        self.num_tokens = num_tokens


def modal_fft_prefill_tiled(
    engine, inference_params, x1v, L, poles, t, dims, layer_idx,
    X_s=None, use_flashfft=False, fftconv_fn=None, state_dtype=None,
    *args, channel_tile=DEFAULT_CHANNEL_TILE, **kwargs,
):
    import torch

    hidden_size, _, _, state_size, filter_groups = dims
    if X_s is None or channel_tile < 1 or hidden_size % filter_groups:
        raise ValueError("unsupported modal FFT prefill shape")
    state_dtype = torch.float32 if state_dtype is None else state_dtype
    batch = x1v.shape[0]
    fft_size = 2 * L
    channels_per_group = hidden_size // filter_groups
    final_state = torch.empty((batch, hidden_size, state_size), device=X_s.device, dtype=state_dtype)
    for start in range(0, hidden_size, channel_tile):
        end = min(start + channel_tile, hidden_size)
        groups = torch.arange(start, end, device=poles.device) // channels_per_group
        selected_poles = poles.index_select(0, groups)
        state_s = (selected_poles.to(torch.float32) * t).exp()
        state_S = torch.fft.fft(state_s, n=fft_size)
        state = torch.fft.ifft(X_s[:, start:end, None, :] * state_S[None, ...], n=fft_size)
        final_state[:, start:end, :] = state[..., L - 1].to(dtype=state_dtype)
        del selected_poles, state_s, state_S, state
    inference_params.state_dict[layer_idx] = final_state


def install_modal_fft_tiling(model, *, channel_tile=DEFAULT_CHANNEL_TILE):
    """Wrap each actual Hyena engine; return originals for paired qualification."""
    originals = []
    for block in model.blocks:
        engine = getattr(getattr(block, "filter", None), "engine", None)
        if engine is None:
            continue
        original = engine.prefill_via_modal_fft

        @functools.wraps(original)
        def tiled(*args, _engine=engine, **kwargs):
            return modal_fft_prefill_tiled(_engine, *args, channel_tile=channel_tile, **kwargs)

        engine.prefill_via_modal_fft = tiled
        originals.append((engine, original, tiled))
    if not originals:
        raise ValueError("no pinned Vortex Hyena engines found for modal FFT tiling")
    return originals
