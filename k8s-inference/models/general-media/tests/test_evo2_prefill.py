"""Independent reference state equivalence; no model weights or GPU required."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import torch

spec = importlib.util.spec_from_file_location("evo2_prefill", Path(__file__).parents[1] / "evo2_prefill.py")
prefill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefill)


def original_state(x1v, poles, t, groups):
    """Literal tensor operations from the pinned Vortex engine.py:609–616."""
    batch, hidden, length = x1v.shape
    frequency = torch.fft.fft(x1v.to(torch.float32), n=2 * length)
    state_s = (poles.to(torch.float32) * t).exp()
    state_S = torch.fft.fft(state_s, n=2 * length).repeat(batch, 1, 1, 1)
    if groups > 1:
        state_S = state_S.repeat_interleave(hidden // groups, 1)
    state = torch.fft.ifft(frequency[..., None, :] * state_S, n=2 * length)
    return state[..., length - 1].to(dtype=torch.float32)


class ModalPrefillTests(unittest.TestCase):
    def test_full_states_equal_reference_across_grouping_lengths_batches_and_tiles(self):
        torch.manual_seed(180926)
        for length in (17, 64, 1024):
            for batch, hidden, groups, state_size, tile in (
                (1, 16, 1, 4, 5), (1, 32, 4, 16, 7), (2, 64, 64, 16, 16), (1, 32, 32, 16, 128),
            ):
                with self.subTest(length=length, batch=batch, groups=groups, tile=tile):
                    x = torch.randn(batch, hidden, length, dtype=torch.bfloat16)
                    poles = -torch.rand(groups, state_size, 1)
                    t = torch.arange(length)[None, None]
                    expected = original_state(x, poles, t, groups)
                    params = types.SimpleNamespace(state_dict={})
                    prefill.modal_fft_prefill_tiled(
                        None, params, x, length, poles, t, (hidden, 0, 0, state_size, groups), 2,
                        X_s=torch.fft.fft(x.to(torch.float32), n=2 * length), channel_tile=tile,
                    )
                    actual = params.state_dict[2]
                    torch.testing.assert_close(actual, expected, rtol=0, atol=0)
                    self.assertEqual(actual.untyped_storage().nbytes(), actual.numel() * actual.element_size())

    def test_install_changes_only_engine_prefill_and_retains_original_for_paired_qualification(self):
        original = lambda *a, **k: None
        engine = types.SimpleNamespace(prefill_via_modal_fft=original)
        model = types.SimpleNamespace(blocks=[
            types.SimpleNamespace(filter=types.SimpleNamespace(engine=engine)), types.SimpleNamespace(),
        ])
        bindings = prefill.install_modal_fft_tiling(model)
        self.assertEqual(len(bindings), 1)
        self.assertIs(bindings[0][1], original)
        self.assertIs(bindings[0][2], engine.prefill_via_modal_fft)

    def test_missing_runtime_shape_does_not_silently_skip_fix(self):
        with self.assertRaisesRegex(ValueError, "no pinned"):
            prefill.install_modal_fft_tiling(types.SimpleNamespace(blocks=[]))


if __name__ == "__main__":
    unittest.main()
