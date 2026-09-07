"""Bounded native H100 kernel check; not an Evo2 model readiness claim."""
import json
import sys
import time

sys.path.insert(0, "/opt/fs2-evo2")
from evo2_h100 import validate_devices
import torch
import transformer_engine.pytorch as te
from flash_attn import flash_attn_func

devices = [{"name": torch.cuda.get_device_name(i), "capability": torch.cuda.get_device_capability(i)} for i in range(torch.cuda.device_count())]
validate_devices(devices)
started = time.monotonic()
for index in range(2):
    with torch.cuda.device(index), torch.inference_mode():
        tensor = torch.randn(1, 16, 2, 64, device=f"cuda:{index}", dtype=torch.bfloat16)
        attention = flash_attn_func(tensor, tensor, tensor, causal=True)
        assert torch.isfinite(attention).all().item()
        linear = te.Linear(128, 128, params_dtype=torch.bfloat16, device=f"cuda:{index}")
        with te.fp8_autocast(enabled=True):
            output = linear(tensor.reshape(16, 128))
        assert torch.isfinite(output).all().item()
        torch.cuda.synchronize(index)
print(json.dumps({"event": "h100-native-kernel-preflight", "status": "PASS", "model_loaded": False, "devices": devices, "torch": torch.__version__, "cuda": torch.version.cuda, "seconds": time.monotonic() - started}), flush=True)
