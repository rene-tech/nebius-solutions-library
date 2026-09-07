"""Immutable RF model reuse; request-specific Sampler objects are never kept."""

import copy
from pathlib import Path


class ModelOnlyCache:
    def __init__(self, *, model, checkpoint_path, checkpoint_config, model_config,
                 d_t1d, d_t2d, to_container, configure_timestep):
        self.model = model
        self.checkpoint_path = str(Path(checkpoint_path).resolve())
        self.checkpoint_config = copy.deepcopy(checkpoint_config)
        self.model_config = copy.deepcopy(model_config)
        self.dimensions = (d_t1d, d_t2d)
        self.to_container = to_container
        self.configure_timestep = configure_timestep

    def load_checkpoint(self, sampler):
        if str(Path(sampler.ckpt_path).resolve()) != self.checkpoint_path:
            raise ValueError("request checkpoint differs from the captured RF model")
        # Native assemble_config_from_chk uses metadata, not checkpoint tensors.
        # Each new sampler receives its own copy of that mutable configuration.
        sampler.ckpt = {"config_dict": copy.deepcopy(self.checkpoint_config)}

    def load_model(self, sampler):
        observed = self.to_container(sampler._conf.model)
        dimensions = (sampler._conf.preprocess.d_t1d, sampler._conf.preprocess.d_t2d)
        if observed != self.model_config or dimensions != self.dimensions:
            raise ValueError("request RF model architecture differs from the captured checkpoint")
        sampler.d_t1d, sampler.d_t2d = dimensions
        # T only creates a non-learned sinusoidal lookup table in the pinned
        # upstream model. Rebuild it with the very same upstream function for
        # the request's T; never modify learned weights or the diffusion steps.
        self.configure_timestep(self.model, int(sampler._conf.diffuser.T))
        return self.model.eval()

    def install(self, sampler_class):
        cache = self

        def checkpoint(sampler):
            return cache.load_checkpoint(sampler)

        def model(sampler):
            return cache.load_model(sampler)

        sampler_class.load_checkpoint = checkpoint
        sampler_class.load_model = model
