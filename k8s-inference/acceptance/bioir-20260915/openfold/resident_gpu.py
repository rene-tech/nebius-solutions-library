"""Evaluation-only inner-model residency control for Lightning predict teardown."""
import torch

class ResidentGPU(torch.nn.Module):
    def __init__(self, inner):
        super().__init__()
        self.inner = inner
        self.skipped_cpu_transfers = 0

    @property
    def version_tensor(self):
        return self.inner.version_tensor

    def forward(self, *args, **kwargs):
        return self.inner(*args, **kwargs)

    def _apply(self, fn, recurse=True):
        parameter = next(self.inner.parameters())
        marker = torch.empty(0, device=parameter.device)
        if marker.is_cuda and fn(marker).device.type == 'cpu':
            self.skipped_cpu_transfers += 1
            return self
        return super()._apply(fn, recurse=recurse)
