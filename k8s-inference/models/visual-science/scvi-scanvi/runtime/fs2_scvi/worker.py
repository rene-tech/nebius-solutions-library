"""GPU worker using the platform's existing durable checkpoint companion.

Inputs stay immutable outside data/. Only closed training state and result
files enter the checkpoint journal; a large input is not re-uploaded per epoch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import resource
import signal
import tarfile
import time
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import scvi
import torch
from lightning.pytorch.callbacks import Callback, ModelCheckpoint
from scvi.train import SaveCheckpoint

from fs2_gromacs.files import atomic_json, digest_file, extract_inputs, inventory

from . import RESULT_SCHEMA, RUNTIME_ID
from .contracts import canonical, normalize
from .data import load_counts

STATE_SCHEMA = "fs2-serve.nebius.ai/scvi-checkpoint/v1"


class RandomState(Callback):
    """Persist RNG state as well as Lightning's optimizer/scheduler/loop state."""

    def on_save_checkpoint(self, trainer, pl_module, checkpoint):
        numpy_state = np.random.get_state()
        checkpoint["fs2_rng"] = {
            "python": random.getstate(),
            # Lists/primitives retain PyTorch's weights_only loader compatibility.
            "numpy": (numpy_state[0], numpy_state[1].tolist(), *numpy_state[2:]),
            "torch": torch.get_rng_state(),
            "cuda": torch.cuda.get_rng_state_all(),
        }

    def on_load_checkpoint(self, trainer, pl_module, checkpoint):
        state = checkpoint["fs2_rng"]
        random.setstate(state["python"])
        numpy_state = state["numpy"]
        np.random.set_state(
            (
                numpy_state[0],
                np.asarray(numpy_state[1], dtype=np.uint32),
                *numpy_state[2:],
            )
        )
        torch.set_rng_state(state["torch"])
        torch.cuda.set_rng_state_all(state["cuda"])


class DurableCheckpoint(SaveCheckpoint):
    """Use full Lightning state, not scVI's model-only SaveCheckpoint format."""

    def __init__(self, workflow, stage):
        self.workflow, self.stage = workflow, stage
        # scVI recognizes this SaveCheckpoint subclass and does not add its own
        # model-only callback. Initialize Lightning directly: scVI's constructor
        # discards save_last, and its hooks replace optimizer checkpoints with
        # model-only directories. This worker needs resumable training state.
        ModelCheckpoint.__init__(
            self,
            dirpath=str(workflow.data / "checkpoints" / stage),
            filename="epoch-{epoch}",
            monitor=None,
            save_top_k=0,
            save_last=True,
            every_n_epochs=workflow.parameters["checkpoint_every_n_epochs"],
            save_on_train_epoch_end=True,
        )

    on_save_checkpoint = Callback.on_save_checkpoint
    on_train_end = ModelCheckpoint.on_train_end
    on_train_batch_end = ModelCheckpoint.on_train_batch_end
    on_exception = ModelCheckpoint.on_exception
    _update_best_and_save = ModelCheckpoint._update_best_and_save

    def on_train_epoch_end(self, trainer, pl_module):
        # Lightning 2.6 only saves `last` after a top-k save. We intentionally
        # retain one atomic last checkpoint, not a second per-epoch copy.
        if (
            not self._should_skip_saving_checkpoint(trainer)
            and (trainer.current_epoch + 1) % self._every_n_epochs == 0
        ):
            self._save_last_checkpoint(trainer, self._monitor_candidates(trainer))

    def _save_checkpoint(self, trainer, filepath):
        temporary = filepath + ".partial"
        ModelCheckpoint._save_checkpoint(self, trainer, temporary)
        os.replace(temporary, filepath)
        self._last_checkpoint_saved = filepath
        self.workflow.state["active_stage"] = self.stage
        self.workflow.state["checkpoint"] = str(
            Path(filepath).relative_to(self.workflow.data)
        )
        self.workflow.state["epoch"] = int(trainer.current_epoch)
        self.workflow.publish()

    def _remove_checkpoint(self, trainer, filepath):
        # Parent SaveCheckpoint treats paths as scVI model directories.
        ModelCheckpoint._remove_checkpoint(self, trainer, filepath)


class Workflow:
    def __init__(self, parameters, workspace, operation_id, *, checkpoint_mode="local"):
        self.parameters = normalize(parameters)
        self.root = Path(workspace).resolve()
        self.data, self.meta = self.root / "data", self.root / ".fs2"
        self.operation_id, self.checkpoint_mode = operation_id, checkpoint_mode
        self.stopped = False
        self.started = time.monotonic()
        self.stage_times = {}
        self.state = {
            "schema": STATE_SCHEMA,
            "operation_id": operation_id,
            "job_id": "main",
            "generation": 0,
            "completed_stages": [],
            "active_stage": None,
            "checkpoint": None,
            "elapsed_seconds": 0.0,
        }

    def stop(self, signum, frame):
        self.stopped = True
        raise InterruptedError(
            "Worker interrupted; resume the last durably committed training checkpoint"
        )

    def wait(self, path, predicate):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if self.stopped or (self.meta / "transport-error.json").exists():
                raise RuntimeError(
                    "Checkpoint transport interrupted; durable prior generation remains valid"
                )
            if path.is_file():
                value = json.loads(path.read_text())
                if predicate(value):
                    return
            time.sleep(0.25)
        raise TimeoutError("Checkpoint transport acknowledgement timed out")

    def initialize(self):
        self.data.mkdir(parents=True, exist_ok=True)
        self.meta.mkdir(parents=True, exist_ok=True)
        if self.checkpoint_mode == "companion":
            self.wait(
                self.meta / "restore-complete.json",
                lambda v: v.get("status") == "ready",
            )
        self.input_sha = digest_file(self.root / "input.h5ad")
        self.reference_sha = (
            digest_file(self.root / "reference.tar.gz")
            if self.parameters["mode"] == "map-query"
            else None
        )
        self.recipe = hashlib.sha256(
            canonical(
                {
                    "parameters": self.parameters,
                    "input": self.input_sha,
                    "reference": self.reference_sha,
                    "runtime": RUNTIME_ID,
                }
            )
        ).hexdigest()
        if (path := self.meta / "scvi-state.json").exists():
            state = json.loads(path.read_text())
            if (
                state.get("schema"),
                state.get("operation_id"),
                state.get("recipe_sha256"),
            ) != (STATE_SCHEMA, self.operation_id, self.recipe):
                raise ValueError(
                    "Checkpoint identity differs from this input, configuration or operation"
                )
            self.state = state
        else:
            self.state["recipe_sha256"] = self.recipe
        self.deadline = (
            self.started
            + self.parameters["max_wall_seconds"]
            - self.state["elapsed_seconds"]
        )
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required for the GPU execution profile")

    def publish(self):
        self.state["elapsed_seconds"] += time.monotonic() - self.started
        self.started = time.monotonic()
        self.state["generation"] += 1
        files = inventory(self.data, max_bytes=self.parameters["max_output_bytes"])
        atomic_json(self.meta / "scvi-state.json", self.state)
        atomic_json(
            self.meta / "checkpoint-ready.json", {"state": self.state, "files": files}
        )
        if self.checkpoint_mode == "companion":
            generation = self.state["generation"]
            self.wait(
                self.meta / "checkpoint-ack.json",
                lambda v: v.get("generation") == generation
                and v.get("status") == "committed",
            )
        print(
            json.dumps(
                {
                    "event": "scvi_checkpoint",
                    "generation": self.state["generation"],
                    "stage": self.state["active_stage"],
                    "epoch": self.state.get("epoch"),
                }
            ),
            flush=True,
        )

    def train(self, model, stage, epochs):
        start = time.monotonic()
        remaining = self.deadline - start
        if remaining < 1:
            raise TimeoutError(
                "Training wall-clock budget exhausted; checkpoint retained"
            )
        parameters = self.parameters
        checkpoint = (
            self.state.get("checkpoint")
            if self.state["active_stage"] == stage
            else None
        )
        kwargs = {
            "max_epochs": epochs,
            "accelerator": "gpu",
            "devices": 1,
            "batch_size": parameters["batch_size"],
            "train_size": parameters["train_size"],
            "early_stopping": parameters["early_stopping"],
            "early_stopping_patience": parameters["early_stopping_patience"],
            "check_val_every_n_epoch": 1,
            "enable_progress_bar": False,
            "callbacks": [RandomState(), DurableCheckpoint(self, stage)],
            "max_time": timedelta(seconds=remaining),
            "default_root_dir": str(self.data / "logs" / stage),
        }
        if checkpoint:
            kwargs["ckpt_path"] = str(self.data / checkpoint)
        model.train(**kwargs)
        if time.monotonic() >= self.deadline:
            raise TimeoutError(
                "Training wall-clock budget exhausted; incomplete training is not a successful result"
            )
        model.save(self.data / stage, overwrite=True, save_anndata=False)
        for metric, values in model.history.items():
            values.to_csv(self.data / f"{stage}-{metric}.csv")
        self.state["completed_stages"].append(stage)
        self.state["active_stage"], self.state["checkpoint"] = None, None
        self.stage_times[stage] = time.monotonic() - start
        self.publish()

    def run(self):
        self.initialize()
        parameters = self.parameters
        scvi.settings.seed = parameters["seed"]
        start = time.monotonic()
        adata, preflight = load_counts(self.root / "input.h5ad", parameters)
        atomic_json(self.data / "preflight.json", preflight)
        self.stage_times["load_and_select"] = time.monotonic() - start
        cls = scvi.model.SCANVI if parameters["method"] == "scanvi" else scvi.model.SCVI
        if parameters["mode"] == "map-query":
            reference_dir = self.root / "reference"
            if not reference_dir.exists():
                extract_inputs(
                    self.root / "reference.tar.gz", reference_dir, max_bytes=1024**3
                )
            reference = json.loads((reference_dir / "reference.json").read_text())
            if (
                reference["method"] != parameters["method"]
                or reference["runtime"] != RUNTIME_ID
            ):
                raise ValueError(
                    "Reference method/runtime differs; use the matching published reference"
                )
            cls.prepare_query_anndata(adata, str(reference_dir))
            if "query" in self.state["completed_stages"]:
                trained = cls.load(self.data / "query", adata=adata)
            else:
                trained = cls.load_query_data(adata, str(reference_dir))
                self.train(trained, "query", parameters["query_max_epochs"])
            final_stage = "query"
        else:
            scvi.model.SCVI.setup_anndata(adata, batch_key=parameters["batch_key"])
            if "scvi" in self.state["completed_stages"]:
                base = scvi.model.SCVI.load(self.data / "scvi", adata=adata)
            else:
                base = scvi.model.SCVI(
                    adata,
                    n_latent=parameters["n_latent"],
                    n_hidden=parameters["n_hidden"],
                    n_layers=parameters["n_layers"],
                    gene_likelihood=parameters["gene_likelihood"],
                )
                self.train(base, "scvi", parameters["max_epochs"])
            trained, final_stage = base, "scvi"
            if parameters["method"] == "scanvi":
                labels = adata.obs[parameters["labels_key"]].astype(str)
                if not (labels != parameters["unlabeled_category"]).any():
                    raise ValueError(
                        "scANVI needs at least one labelled cell; all labels are the unlabeled category"
                    )
                # Fully labelled datasets are valid: Unknown need not occur.
                if "scanvi" in self.state["completed_stages"]:
                    trained = scvi.model.SCANVI.load(self.data / "scanvi", adata=adata)
                else:
                    trained = scvi.model.SCANVI.from_scvi_model(
                        base,
                        labels_key=parameters["labels_key"],
                        unlabeled_category=parameters["unlabeled_category"],
                    )
                    self.train(trained, "scanvi", parameters["scanvi_max_epochs"])
                final_stage = "scanvi"
        start = time.monotonic()
        latent = trained.get_latent_representation(batch_size=parameters["batch_size"])
        if not np.isfinite(latent).all():
            raise ValueError("Training produced non-finite latent embeddings")
        adata.obsm["X_scvi"] = latent
        pd.DataFrame(latent, index=adata.obs_names).to_csv(
            self.data / "latent_embeddings.csv", index_label="cell_id"
        )
        if parameters["method"] == "scanvi":
            probabilities = trained.predict(
                soft=True, batch_size=parameters["batch_size"]
            )
            if not np.isfinite(probabilities.to_numpy()).all() or not np.allclose(
                probabilities.sum(axis=1), 1, atol=1e-4
            ):
                raise ValueError(
                    "scANVI label probabilities are not finite and normalized"
                )
            probabilities.to_csv(
                self.data / "label_probabilities.csv", index_label="cell_id"
            )
            adata.obs["scanvi_prediction"] = probabilities.idxmax(axis=1).astype(str)
            adata.obs[["scanvi_prediction"]].to_csv(
                self.data / "predicted_labels.csv", index_label="cell_id"
            )
        if parameters["write_integrated_h5ad"]:
            ad.settings.allow_write_nullable_strings = True
            adata.write_h5ad(self.data / "integrated.h5ad", compression="gzip")
        self.visualize(adata, latent)
        reference = {
            "method": parameters["method"],
            "runtime": RUNTIME_ID,
            "input_sha256": self.input_sha,
            "genes": list(map(str, adata.var_names)),
            "parameters": parameters,
        }
        atomic_json(self.data / final_stage / "reference.json", reference)
        with tarfile.open(self.data / "reference.tar.gz", "w:gz") as archive:
            for path in sorted((self.data / final_stage).iterdir()):
                if path.is_file():
                    archive.add(path, arcname=path.name)
        self.stage_times["export_and_visualize"] = time.monotonic() - start
        self.publish()
        result = {
            "schema": RESULT_SCHEMA,
            "operation_id": self.operation_id,
            "job_id": "main",
            "status": "succeeded",
            "recipe_sha256": self.recipe,
            "runtime": RUNTIME_ID,
            "parameters": parameters,
            "input_sha256": self.input_sha,
            "reference_sha256": self.reference_sha,
            "preflight": preflight,
            "cells": adata.n_obs,
            "genes": adata.n_vars,
            "latent_dimensions": latent.shape[1],
            "completed_stages": self.state["completed_stages"],
            "checkpoint_generation": self.state["generation"],
            "timings_seconds": self.stage_times,
            "elapsed_seconds": self.state["elapsed_seconds"],
            "peak_host_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * 1024,
            "peak_gpu_memory_bytes": torch.cuda.max_memory_allocated(),
            "gpu": torch.cuda.get_device_name(0),
            "versions": {
                p: version(p)
                for p in ("scvi-tools", "torch", "anndata", "scanpy", "lightning")
            },
            "gpu_snapshot_used": False,
            "scientific_convergence_claimed": False,
            "files": inventory(self.data, max_bytes=parameters["max_output_bytes"]),
        }
        atomic_json(self.root / "result.json", result)
        return result

    def visualize(self, adata, latent):
        parameters = self.parameters
        if parameters["visualization"] == "none":
            return
        import umap
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import pyplot as plt

        count = (
            len(latent)
            if parameters["visualization"] == "full"
            else min(len(latent), parameters["visualization_cells"])
        )
        indices = np.sort(
            np.random.default_rng(parameters["seed"]).choice(
                len(latent), count, replace=False
            )
        )
        coordinates = (
            latent[indices, :2]
            if count < 4
            else umap.UMAP(
                n_neighbors=min(15, count - 1),
                random_state=parameters["seed"],
                n_jobs=1,
            ).fit_transform(latent[indices])
        )
        pd.DataFrame(
            coordinates, index=adata.obs_names[indices], columns=["UMAP1", "UMAP2"]
        ).to_csv(self.data / "umap.csv", index_label="cell_id")
        fig, ax = plt.subplots(figsize=(8, 6))
        key = (
            "scanvi_prediction"
            if "scanvi_prediction" in adata.obs
            else parameters["batch_key"]
        )
        colors = pd.factorize(adata.obs[key].iloc[indices])[0] if key else None
        ax.scatter(coordinates[:, 0], coordinates[:, 1], c=colors, s=2, alpha=0.5)
        ax.set(
            title=f"{parameters['method']} UMAP: {count:,} / {len(latent):,} cells",
            xlabel="UMAP 1",
            ylabel="UMAP 2",
        )
        fig.savefig(self.data / "preview.png", dpi=150)
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument(
        "--checkpoint-mode", choices=("local", "companion"), default="companion"
    )
    args = parser.parse_args()
    worker = Workflow(
        json.loads(args.request.read_text()),
        args.workspace,
        args.operation_id,
        checkpoint_mode=args.checkpoint_mode,
    )
    signal.signal(signal.SIGTERM, worker.stop)
    try:
        worker.run()
    except BaseException as error:
        args.workspace.mkdir(parents=True, exist_ok=True)
        atomic_json(
            args.workspace / "result.json",
            {
                "schema": RESULT_SCHEMA,
                "operation_id": args.operation_id,
                "parameters": worker.parameters,
                "status": "interrupted"
                if isinstance(error, (InterruptedError, KeyboardInterrupt))
                else "failed",
                "error_type": type(error).__name__,
                "stage": worker.state["active_stage"],
                "checkpoint_generation": worker.state["generation"],
                "scientific_convergence_claimed": False,
            },
        )
        raise


if __name__ == "__main__":
    main()
