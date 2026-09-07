#!/usr/bin/env python3
"""Optional input-free RFdiffusion model worker over the unchanged native loop.

Only checkpoint weights/configuration are captured. The pinned upstream Hydra
configuration, Sampler, target parsing, contig construction, seeds, diffusion
schedule and result writing are recreated for every original wrapper request.
"""

import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import time

from rfdiffusion_model_cache import ModelOnlyCache
from scientific_server import serve


class RFdiffusionBackend:
    command = "run-inference"
    environment_keys = ("HOME", "XDG_CACHE_HOME", "MPLCONFIGDIR", "DGL_HOME", "OMP_NUM_THREADS",
                        "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")

    def __init__(self):
        started = time.monotonic()
        import hydra
        import torch
        from omegaconf import OmegaConf
        from rfdiffusion.inference.model_runners import Sampler
        from rfdiffusion.Embeddings import get_timestep_embedding

        self.root = Path(os.environ.get("FS2_RFDIFFUSION_HOME", "/opt/rfdiffusion"))
        checkpoint = Path(os.environ["FS2_RFDIFFUSION_CHECKPOINT"])
        checksum = hashlib.sha256()
        with checkpoint.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(block)
        digest = checksum.hexdigest()
        expected = "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"
        if digest != expected:
            raise ValueError("RF Base checkpoint differs from the accepted artifact")
        with hydra.initialize_config_dir(version_base=None, config_dir=str(self.root / "config/inference")):
            conf = hydra.compose(config_name="base")
        # Do not construct Sampler: its constructor also parses a target.
        donor = Sampler.__new__(Sampler)
        donor._log = logging.getLogger(__name__)
        donor.device = torch.device("cuda")
        donor.ckpt_path = str(checkpoint)
        donor._conf = conf
        donor.load_checkpoint()
        donor.assemble_config_from_chk()
        model = donor.load_model()

        def timestep(model, steps):
            embedding = getattr(model, "timestep_embedder", None)
            if embedding is not None:
                embedding.T = steps
                embedding.source_embeddings = get_timestep_embedding(torch.arange(steps + 1), embedding.input_size)
                embedding.source_embeddings.requires_grad = False

        self.cache = ModelOnlyCache(
            model=model, checkpoint_path=checkpoint, checkpoint_config=donor.ckpt["config_dict"],
            model_config=OmegaConf.to_container(conf.model, resolve=True),
            d_t1d=donor.d_t1d, d_t2d=donor.d_t2d,
            to_container=lambda value: OmegaConf.to_container(value, resolve=True), configure_timestep=timestep,
        )
        self.cache.install(Sampler)
        del donor  # Do not retain checkpoint tensor duplicates or any sampler.
        spec = importlib.util.spec_from_file_location("fs2_native_rf_inference", self.root / "scripts/run_inference.py")
        self.upstream = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.upstream
        spec.loader.exec_module(self.upstream)
        self.torch = torch
        torch.cuda.synchronize()
        self.ready = {"ready": True, "model": "rfdiffusion", "pid": os.getpid(),
                      "model_load_seconds": time.monotonic() - started, "checkpoint_sha256": digest,
                      "boundary": "immutable model weights initialized and CUDA synchronized; no Sampler or target captured",
                      "allocated_bytes": torch.cuda.memory_allocated()}

    def execute(self, arguments):
        import hydra

        if arguments[0] != self.command:
            raise ValueError("expected original RF inference overrides")
        before = time.monotonic()
        first_design = None
        destination = sys.stdout

        class TimedLog:
            def write(self, text):
                nonlocal first_design
                if first_design is None and "Making design " in text:
                    first_design = time.monotonic() - before
                return destination.write(text)

            def flush(self):
                destination.flush()

        logger = logging.getLogger()
        original_handlers, original_level = logger.handlers[:], logger.level
        try:
            logger.handlers = [logging.StreamHandler(TimedLog())]
            logger.setLevel(logging.INFO)
            with hydra.initialize_config_dir(version_base=None, config_dir=str(self.root / "config/inference")):
                # Native @hydra.main passes only the task configuration. Its
                # internal sweep metadata contains mandatory values even for a
                # single run and must not enter the scientific result .trb.
                conf = hydra.compose(config_name="base", overrides=arguments[1:])
                self.upstream.main.__wrapped__(conf)
            self.torch.cuda.synchronize()
        finally:
            logger.handlers, logger.level = original_handlers, original_level
        print("FS2_RF_SNAPSHOT_EXECUTION " + json.dumps({"first_design_seconds": first_design,
              "upstream_seconds": time.monotonic() - before}), flush=True)

    def prepare_snapshot(self):
        import gc

        gc.collect()
        self.torch.cuda.empty_cache()
        self.torch.cuda.synchronize()
        return {"allocated_bytes": self.torch.cuda.memory_allocated(), "reserved_bytes": self.torch.cuda.memory_reserved()}


if __name__ == "__main__":
    serve(RFdiffusionBackend())
