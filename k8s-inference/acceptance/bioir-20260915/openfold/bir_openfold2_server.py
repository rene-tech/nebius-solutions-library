#!/usr/bin/env python3
"""Evaluation-only BIR replacement of the exact deployed OpenFold2 model seam."""
import os
from pathlib import Path
import sys
import threading
import time
import traceback

sys.path[:0] = ['/payload/server', '/payload/openfold']
import server as current

class Runtime(current.Runtime):
    def load(self):
        started = time.perf_counter()
        try:
            import torch
            import bionemo_ir
            from bionemo_ir.models.openfold2 import OpenFold2
            from bionemo_ir.hubs import FoldingSupportMatrix as SupMat
            from openfold.config import model_config
            torch.set_grad_enabled(False)
            torch.set_float32_matmul_precision('high')
            if current.file_sha256(current.CHECKPOINT) != current.CHECKPOINT_SHA256:
                raise RuntimeError('Exact deployed checkpoint SHA256 mismatch')
            # Retain the deployed upstream feature pipeline and four recycling
            # slices (initial pass + three recycles). No BIR pipeline defaults.
            self.config = model_config('model_3_ptm')
            key = SupMat.OpenFold2_NoTempl_PTM1
            config = OpenFold2.get_pretrained_config(key)
            precision = os.environ.get('BIOIR_EVAL_PRECISION', 'float32')
            if precision == 'float32':
                config.set_dtype('float32')
                config.set_triangle_attention_backend('SDPA')
            config.recycle_early_stop_tolerance = -1.0
            state = torch.load(current.CHECKPOINT, map_location='cpu', weights_only=False)
            if isinstance(state, dict) and set(state) == {'ema'}:
                state = state['ema']['params']
            model = OpenFold2(config=config, model_name=key, include_load_weights=False)
            model.load_weights(state)
            self.model = model.cuda().eval()
            torch.cuda.synchronize()
            self.identity = {'model': 'openfold2', 'runtime_origin': 'bioir-public-evaluation', 'bionemo_ir': bionemo_ir.__version__, 'torch': torch.__version__, 'cuda': torch.version.cuda, 'gpu': torch.cuda.get_device_name(), 'checkpoint': current.CHECKPOINT.name, 'checkpoint_sha256': current.CHECKPOINT_SHA256, 'upstream_source_revision': current.SOURCE_REVISION, 'precision_variant': precision, 'feature_pipeline': 'unchanged-current-upstream-model_3_ptm', 'numpy_seed': 0, 'torch_seed': 1, 'recycling': 'all original feature slices; early stop disabled', 'bir_config': config.model_dump(mode='json'), 'load_seconds': time.perf_counter() - started}
            self.ready = True
            current.event('MODEL_READY', **self.identity)
        except Exception as exc:
            self.error = str(exc)
            current.event('LOAD_FAILED', error=self.error)
            traceback.print_exc()

    def predict(self, input_id, sequence):
        result = super().predict(input_id, sequence)
        result['structures_in_ranked_order'][0]['runtime_origin'] = 'bioir-public-evaluation'
        return result

if __name__ == '__main__':
    current.RUNTIME = Runtime()
    current.event('PROCESS_START')
    threading.Thread(target=current.RUNTIME.load, daemon=True).start()
    current.ThreadingHTTPServer(('0.0.0.0', 8000), current.Handler).serve_forever()
