#!/usr/bin/env python3
"""Reusable immutable Boltz2/ProteinMPNN state, never captured request features."""

import importlib.util
import os
from pathlib import Path
import time

from scientific_server import serve


class MosaicBackend:
    command = "run-shard"
    environment_keys = ("FS2_INPUT_ARTIFACT_ROOT",)

    def __init__(self):
        started = time.monotonic()
        # JAX otherwise reserves about 60 GiB for this 2 GiB model on H100.
        # Avoid checkpointing an unused allocator arena; this changes neither
        # model precision nor request optimizer/sampling semantics.
        os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
        original = Path("/opt/fs2/mosaic/runtime_entrypoint_original.py")
        if not original.is_file():
            original = original.with_name("runtime_entrypoint.py")
        spec = importlib.util.spec_from_file_location("fs2_mosaic_original", original)
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        root = self.runtime._artifact_root()
        self.boltz_path = self.runtime._verified(
            root / "mosaic/boltz/boltz2_conf.ckpt", self.runtime.BOLTZ2_SHA256, "Boltz2 checkpoint")
        self.mpnn_path = self.runtime._verified(
            root / "mosaic/proteinmpnn/v_48_020.pt", self.runtime.PROTEINMPNN_SHA256, "ProteinMPNN checkpoint")
        if not (root / "mosaic/boltz/mols").is_dir():
            raise ValueError("the immutable Boltz2 CCD artifact is incomplete")
        os.environ["MOSAIC_CACHE_DIR"] = str(root / "mosaic")
        os.environ["BOLTZ_CACHE"] = str(root / "mosaic/boltz")
        import jax
        from mosaic.models import boltz2
        from mosaic.proteinmpnn.mpnn import ProteinMPNN

        self.jax = jax
        self.boltz = boltz2.Boltz2(cache_path=self.boltz_path)
        self.mpnn = ProteinMPNN.from_pretrained(self.mpnn_path)
        self.weights = [leaf for leaf in jax.tree_util.tree_leaves((self.boltz.model, self.mpnn))
                        if isinstance(leaf, jax.Array)]
        if not self.weights:
            raise ValueError("no actual Mosaic model arrays were found for synchronization")
        self.synchronize_weights()
        backend = self

        def existing_boltz(cache_path=None):
            if Path(cache_path) != backend.boltz_path:
                raise ValueError("request differs from the captured Boltz2 checkpoint")
            return backend.boltz

        def existing_mpnn(cls, checkpoint_path):
            if Path(checkpoint_path) != backend.mpnn_path:
                raise ValueError("request differs from the captured ProteinMPNN checkpoint")
            return backend.mpnn

        boltz2.Boltz2 = existing_boltz
        ProteinMPNN.from_pretrained = classmethod(existing_mpnn)
        self.ready = {
            "ready": True, "model": "mosaic", "pid": os.getpid(),
            "model_load_seconds": time.monotonic() - started,
            "boundary": "immutable JAX model arrays synchronized; request features and compilation excluded",
            "array_count": len(self.weights), "array_bytes": sum(leaf.nbytes for leaf in self.weights),
            "xla_preallocate": os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"],
            "xla_allocator": os.environ.get("XLA_PYTHON_CLIENT_ALLOCATOR", "default"),
            "binding": {"boltz2_sha256": self.runtime.BOLTZ2_SHA256,
                        "proteinmpnn_sha256": self.runtime.PROTEINMPNN_SHA256,
                        "source_revision": self.runtime.SOURCE_REVISION},
        }

    def synchronize_weights(self):
        for leaf in self.weights:
            leaf.block_until_ready()

    def execute(self, arguments):
        args = self.runtime._parser().parse_args(arguments)
        self.runtime._run_shard(args)
        # The original runtime materializes finite coordinates/metrics before
        # returning. Do not precompute another input or reduce optimizer steps.

    def prepare_snapshot(self):
        import gc

        self.synchronize_weights()
        gc.collect()
        self.jax.effects_barrier()
        return {"model_arrays": len(self.weights), "model_bytes": sum(leaf.nbytes for leaf in self.weights)}


if __name__ == "__main__":
    serve(MosaicBackend())
