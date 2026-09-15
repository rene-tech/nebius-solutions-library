#!/usr/bin/env python3
"""Preserve Preview2 preprocessing, confidence scorer and HTTP boundary with BIR."""
import gc
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
import traceback

sys.path.insert(0, '/payload/server')
import server as current
current.ARTIFACTS = Path('/payload/weights')

class Runtime(current.Runtime):
    def __init__(self):
        self._candidate_ready = False
        self._loaded = False
        super().__init__()

    @property
    def ready(self):
        return self._loaded and self._candidate_ready

    @ready.setter
    def ready(self, value):
        self._loaded = value

    def load(self):
        started = time.perf_counter()
        try:
            import torch
            import bionemo_ir
            from bionemo_ir.models.openfold3 import OpenFold3
            from openfold3.core.utils.tensor_utils import tensor_tree_map
            checkpoint = current.ARTIFACTS / 'of3-p2-155k.pt'
            expected = 'af09eac4f29cef856633af07558cb143226fe95ebbef2c20921769d4a5f4bee4'
            h = hashlib.sha256()
            with checkpoint.open('rb') as f:
                for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != expected:
                raise RuntimeError('Preview2 checkpoint digest mismatch')
            super().load()
            if self.error:
                return
            original = self.module.model.cpu()
            shared = self.module.config.architecture.shared
            recycles = shared.num_recycles
            steps = shared.diffusion.no_full_rollout_steps
            samples = shared.diffusion.no_full_rollout_samples
            config = OpenFold3.get_pretrained_config('openfold3')
            precision = os.environ.get('BIOIR_EVAL_PRECISION', 'float32')
            if precision == 'float32':
                config.set_dtype('float32')
                config.trunk.pairformer.s_path_dtype = torch.float32
                config.auxiliary_heads_config.pairformer.s_path_dtype = torch.float32
                config.set_triangle_attention_backend('SDPA')
                config.set_pairwise_attention_backend('SDPA')
            candidate = OpenFold3(config=config, include_load_weights=False, model_name='openfold3')
            candidate.load_weights(original.state_dict())
            graph_requested = os.environ.get('BIOIR_CUDA_GRAPH') == '1'
            resident = os.environ.get('BIOIR_GPU_RESIDENT') == '1'
            if graph_requested:
                if not resident:
                    raise ValueError('Graph variant requires explicitly qualified resident lifecycle')
                from bionemo_ir.models.optimize_module_setter import AcceleratedConfig
                candidate.optimize({'diffusion_module': AcceleratedConfig(backend='torch')})
            version = original.version_tensor.detach().clone()

            class Shim(torch.nn.Module):
                def __init__(self, model):
                    super().__init__()
                    self.runtime = model
                    self.register_buffer('version_tensor', version)

                def forward(self, batch):
                    # Both paths receive the identical upstream tensors. Preserve
                    # the metadata/sample-axis contract expected by its scorer.
                    perms = batch.pop('ref_space_uid_to_perm', None)
                    # Native Lightning has already seeded torch with 42 before
                    # preprocessing. Preserve that advanced RNG state instead
                    # of restarting diffusion with a new private seed-42 stream.
                    output = self.runtime(batch, recycling_steps=recycles, num_sampling_steps=steps, diffusion_samples=samples, sampling_seed=None)
                    expanded = tensor_tree_map(lambda t: t.unsqueeze(1), batch)
                    expanded['ref_space_uid_to_perm'] = perms
                    output['recycles'] = recycles
                    return expanded, output

            self.module.model = Shim(candidate).cuda().eval()
            self._bir_model = candidate
            if resident:
                from resident_gpu import ResidentGPU
                self.module.model = ResidentGPU(self.module.model)
            del original, candidate
            gc.collect()
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
            self.identity.update({'runtime_origin': 'bioir-preview2-evaluation', 'bionemo_ir': bionemo_ir.__version__, 'precision_variant': precision, 'checkpoint_sha256': expected, 'recycling_steps': recycles, 'sampling_steps': steps, 'samples': samples, 'sampling_rng': 'preserve upstream global seed42 stream; BIR sampling_seed=None', 'bir_config': config.model_dump(mode='json'), 'load_seconds': time.perf_counter() - started, 'integration': 'BIR model under unchanged Preview2 Lightning/data/confidence/output code'})
            self._candidate_ready = True
            self.identity.update(cuda_graph_requested=graph_requested, gpu_resident_lifecycle=resident)
            current.event('BIR_MODEL_READY', **self.identity)
        except Exception as exc:
            self.error = str(exc)
            current.event('LOAD_FAILED', error=self.error)
            traceback.print_exc()

    def predict(self, payload):
        result = super().predict(payload)
        tracker = self._bir_model.diffusion_module
        if hasattr(tracker, 'graph_state_by_key'):
            states = [{'key': str(key), 'calls': state.num_prev_calls_by_input_key, 'state': state.preparation_state.name, 'graph_present': state.graph is not None, 'working_set_bytes': state.working_set_bytes} for key, state in tracker.graph_state_by_key.items()]
            self.identity['graph_state'] = {'tracker_type': type(tracker).__name__, 'states': states, 'fallback_to_eager_by_key': {str(k): v for k, v in tracker.fallback_to_eager_by_key.items()}}
            current.event('CUDA_GRAPH_STATE', **self.identity['graph_state'])
        if hasattr(self.module.model, 'skipped_cpu_transfers'):
            self.identity['skipped_cpu_transfers'] = self.module.model.skipped_cpu_transfers
        result['outputs'][0]['runtime_metrics']['runtime_origin'] = 'bioir-preview2-evaluation'
        result['outputs'][0]['structures_with_scores'][0]['source'] += '; BIR public evaluation'
        return result

if __name__ == '__main__':
    current.RUNTIME = Runtime()
    current.event('PROCESS_START')
    threading.Thread(target=current.RUNTIME.load, daemon=True).start()
    current.ThreadingHTTPServer(('0.0.0.0', 8000), current.Handler).serve_forever()
