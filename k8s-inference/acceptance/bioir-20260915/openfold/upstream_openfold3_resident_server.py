#!/usr/bin/env python3
"""Exact upstream model with the same bounded residency change as graph BIR."""
import sys
import threading
sys.path.insert(0, '/opt/fs2/openfold3-preview2')
import server as current
from resident_gpu import ResidentGPU

class Runtime(current.Runtime):
    def __init__(self):
        self._loaded = self._resident_ready = False
        super().__init__()

    @property
    def ready(self):
        return self._loaded and self._resident_ready

    @ready.setter
    def ready(self, value):
        self._loaded = value

    def load(self):
        super().load()
        if self.error:
            return
        self.module.model = ResidentGPU(self.module.model)
        self.identity['gpu_lifecycle'] = 'inner model stays CUDA across Lightning teardown'
        self._resident_ready = True
        current.event('RESIDENT_MODEL_READY', **self.identity)

    def predict(self, payload):
        result = super().predict(payload)
        self.identity['skipped_cpu_transfers'] = self.module.model.skipped_cpu_transfers
        current.event('GPU_RESIDENCY_STATE', skipped_cpu_transfers=self.module.model.skipped_cpu_transfers)
        return result

if __name__ == '__main__':
    current.RUNTIME = Runtime()
    current.event('PROCESS_START')
    threading.Thread(target=current.RUNTIME.load, daemon=True).start()
    current.ThreadingHTTPServer(('0.0.0.0', 8000), current.Handler).serve_forever()
