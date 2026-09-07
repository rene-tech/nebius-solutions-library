#!/usr/bin/env python3
"""Request-independent Protenix v2 worker for optional CUDA+CRIU restore.

Only the immutable model object is retained. The pinned upstream Click CLI
still parses each request and constructs its own configuration, input loader,
seed sequence, output directories and dumper. The ordinary FS2 pred wrapper
must validate the localized artifacts and prepared handoff before its CLI
proxy calls this localhost worker.
"""

from __future__ import annotations

import copy
import os
import sys
import time

from scientific_server import serve


class ProtenixBackend:
    command = "pred"

    def __init__(self):
        started = time.monotonic()
        sys.path.insert(0, "/opt/fs2")
        import run_protenix
        import torch
        from runner import batch_inference
        from runner.inference import InferenceRunner

        run_protenix._validate_installed_runtime()
        artifact_digest = run_protenix._validate_artifact()
        os.environ["PROTENIX_ROOT_DIR"] = str(run_protenix.PROTENIX_ROOT)
        # Loading needs an output directory, but no customer input is consumed
        # and this empty donor directory is never reused as a request output.
        batch_inference.inference_configs["dump_dir"] = "/tmp/fs2-protenix-donor"
        donor = batch_inference.get_default_runner(
            seeds=[101], n_cycle=10, n_step=200, n_sample=1, dtype="bf16",
            model_name="protenix-v2", use_msa=False, use_template=False,
            use_rna_msa=False,
        )
        torch.cuda.synchronize()
        self.model = donor.model
        self.torch = torch
        self.upstream = batch_inference
        self.inference_defaults = copy.deepcopy(batch_inference.inference_configs)
        self.binding = {
            "artifact_manifest_sha256": artifact_digest,
            "checkpoint_sha256": run_protenix.CHECKPOINT_SHA256,
            "source_revision": run_protenix.CODE_REVISION,
            "dtype": donor.configs.dtype,
            "model_name": donor.configs.model_name,
            "load_checkpoint_dir": donor.configs.load_checkpoint_dir,
        }
        backend = self

        def use_loaded_model(runner):
            for field in ("dtype", "model_name", "load_checkpoint_dir"):
                if runner.configs[field] != backend.binding[field]:
                    raise ValueError(f"request differs from captured Protenix {field}")
            runner.model = backend.model
            # Upstream itself supports repeated different inputs this way.
            # Request sampling/seed settings are not captured configuration.
            runner.update_model_configs(runner.configs)

        def use_loaded_checkpoint(runner):
            if runner.model is not backend.model:
                raise ValueError("request is not bound to the captured Protenix model")
            runner.model.eval()

        InferenceRunner.init_model = use_loaded_model
        InferenceRunner.load_checkpoint = use_loaded_checkpoint
        self.ready = {
            "ready": True, "model": "protenix-v2", "pid": os.getpid(),
            "model_load_seconds": time.monotonic() - started,
            "boundary": "immutable model loaded and CUDA synchronized; request compilation excluded",
            "binding": self.binding,
        }

    def execute(self, arguments):
        # The upstream module mutates these global defaults. Reset them for
        # each invocation; otherwise a prior output directory can leak into
        # a later request even when model weights are unchanged.
        self.upstream.inference_configs.clear()
        self.upstream.inference_configs.update(copy.deepcopy(self.inference_defaults))
        self.upstream.protenix_cli.main(args=arguments, standalone_mode=False)
        self.torch.cuda.synchronize()

    def prepare_snapshot(self):
        import gc

        self.torch.cuda.synchronize()
        gc.collect()
        self.torch.cuda.empty_cache()
        self.torch.cuda.synchronize()
        return {"allocated_bytes": self.torch.cuda.memory_allocated(),
                "reserved_bytes": self.torch.cuda.memory_reserved()}


if __name__ == "__main__":
    serve(ProtenixBackend())
