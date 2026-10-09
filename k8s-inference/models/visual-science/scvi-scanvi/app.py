from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tempfile
import zipfile
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import anndata as ad
import matplotlib
import numpy as np
import pandas as pd
import scvi
import torch
import umap
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_CELLS = 100_000
MAX_GENES = 50_000
MAX_NNZ = 200_000_000
training_lock = asyncio.Lock()
logger = logging.getLogger("fs2.visual_science.scvi")


class TrainingError(RuntimeError):
    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause_type = type(cause).__name__
        super().__init__(f"{stage} failed with {self.cause_type}")


app = FastAPI(
    title="Nebius Scientific AI scVI/scANVI",
    version="1.1.0",
    description="Research-only GPU fit/transform backend for bounded raw-count AnnData inputs.",
)


@app.get("/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/v1/health/ready")
def ready() -> dict[str, str]:
    if not torch.cuda.is_available():
        raise HTTPException(status_code=503, detail="CUDA is unavailable")
    return {"status": "ready", "scvi_tools": version("scvi-tools")}


async def save_upload(upload: UploadFile, destination: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(8 * 1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="AnnData upload exceeds 64 MiB")
            digest.update(chunk)
            handle.write(chunk)
    if size == 0:
        raise HTTPException(status_code=422, detail="AnnData upload is empty")
    return digest.hexdigest()


def validate_counts(adata: ad.AnnData) -> None:
    if not (1 <= adata.n_obs <= MAX_CELLS and 1 <= adata.n_vars <= MAX_GENES):
        raise HTTPException(status_code=422, detail="AnnData shape is outside supported limits")
    nnz = int(adata.X.nnz) if hasattr(adata.X, "nnz") else int(np.prod(adata.X.shape))
    if nnz > MAX_NNZ:
        raise HTTPException(status_code=422, detail="AnnData nonzero count exceeds the interactive limit")
    sample = adata.X[: min(128, adata.n_obs), : min(128, adata.n_vars)]
    if hasattr(sample, "toarray"):
        sample = sample.toarray()
    values = np.asarray(sample)
    if not np.isfinite(values).all() or (values < 0).any():
        raise HTTPException(status_code=422, detail="X must contain finite nonnegative raw counts")
    if not np.allclose(values, np.rint(values), rtol=0, atol=1e-6):
        raise HTTPException(status_code=422, detail="X appears processed; raw integer counts are required")


def _plot_values(adata: ad.AnnData, key: str | None) -> tuple[np.ndarray | None, str | None]:
    if key is None:
        return None, None
    categories = adata.obs[key].astype(str)
    codes, labels = pd.factorize(categories, sort=True)
    return codes, ", ".join(map(str, labels[:12]))


def write_visualization(
    adata: ad.AnnData,
    latent: np.ndarray,
    output_dir: Path,
    *,
    batch_key: str | None,
    labels_key: str | None,
    seed: int,
) -> None:
    if len(latent) < 4:
        coordinates = np.pad(latent[:, :2], ((0, 0), (0, max(0, 2 - latent.shape[1]))))[:, :2]
    else:
        coordinates = umap.UMAP(
            n_components=2,
            n_neighbors=min(15, len(latent) - 1),
            min_dist=0.3,
            random_state=seed,
            transform_seed=seed,
        ).fit_transform(latent)
    pd.DataFrame(coordinates, index=adata.obs_names, columns=["umap_1", "umap_2"]).to_csv(
        output_dir / "umap_embeddings.csv"
    )

    color_specs = [("batch",) + _plot_values(adata, batch_key), ("label",) + _plot_values(adata, labels_key)]
    color_specs = [spec for spec in color_specs if spec[1] is not None]
    if not color_specs:
        color_specs = [("cells", np.arange(len(coordinates)), None)]
    figure, axes = plt.subplots(1, len(color_specs), figsize=(6 * len(color_specs), 5), squeeze=False)
    for axis, (title, colors, legend) in zip(axes[0], color_specs, strict=True):
        axis.scatter(coordinates[:, 0], coordinates[:, 1], c=colors, cmap="turbo", s=8, alpha=0.8, linewidths=0)
        axis.set_title(title if legend is None else f"{title}: {legend}")
        axis.set_xlabel("UMAP 1")
        axis.set_ylabel("UMAP 2")
        axis.set_xticks([])
        axis.set_yticks([])
    figure.suptitle("scVI/scANVI latent embedding")
    figure.tight_layout()
    figure.savefig(output_dir / "preview.png", dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(figure)


def fit_transform(
    input_path: Path,
    output_dir: Path,
    method: str,
    batch_key: str | None,
    labels_key: str | None,
    unlabeled_category: str,
    max_epochs: int,
    n_latent: int,
    seed: int,
    input_sha256: str,
) -> Path:
    stage = "initialize"
    try:
        scvi.settings.seed = seed
        stage = "read_anndata"
        adata = ad.read_h5ad(input_path)
        validate_counts(adata)
        for key in (batch_key, labels_key):
            if key and key not in adata.obs:
                raise HTTPException(status_code=422, detail=f"obs key not found: {key}")

        stage = "setup_scvi"
        scvi.model.SCVI.setup_anndata(adata, batch_key=batch_key)
        base_model = scvi.model.SCVI(adata, n_latent=n_latent)
        stage = "train_scvi"
        base_model.train(max_epochs=max_epochs, accelerator="gpu", devices=1)
        trained_model: scvi.model.SCVI | scvi.model.SCANVI = base_model

        if method == "scanvi":
            if not labels_key:
                raise HTTPException(status_code=422, detail="labels_key is required for scANVI")
            if unlabeled_category not in set(adata.obs[labels_key].astype(str)):
                raise HTTPException(status_code=422, detail="unlabeled_category is absent from labels_key")
            stage = "setup_scanvi"
            trained_model = scvi.model.SCANVI.from_scvi_model(
                base_model,
                labels_key=labels_key,
                unlabeled_category=unlabeled_category,
            )
            stage = "train_scanvi"
            trained_model.train(max_epochs=max_epochs, accelerator="gpu", devices=1)

        stage = "export_embeddings"
        latent = np.asarray(trained_model.get_latent_representation())
        adata.obsm["X_scvi"] = latent
        ad.settings.allow_write_nullable_strings = True
        adata.write_h5ad(output_dir / "integrated.h5ad", compression="gzip")
        pd.DataFrame(latent, index=adata.obs_names).to_csv(output_dir / "latent_embeddings.csv")
        stage = "render_preview"
        write_visualization(
            adata,
            latent,
            output_dir,
            batch_key=batch_key,
            labels_key=labels_key,
            seed=seed,
        )
        stage = "save_model"
        trained_model.save(output_dir / "model", overwrite=True, save_anndata=False)
    except HTTPException:
        raise
    except Exception as exc:
        raise TrainingError(stage, exc) from exc

    manifest = {
        "method": method,
        "input_sha256": input_sha256,
        "cells": int(adata.n_obs),
        "genes": int(adata.n_vars),
        "nonzero_counts": int(adata.X.nnz) if hasattr(adata.X, "nnz") else int(np.prod(adata.X.shape)),
        "latent_dimensions": int(latent.shape[1]),
        "batch_key": batch_key,
        "labels_key": labels_key,
        "unlabeled_category": unlabeled_category if method == "scanvi" else None,
        "max_epochs": max_epochs,
        "seed": seed,
        "scvi_tools_version": version("scvi-tools"),
        "torch_version": torch.__version__,
        "gpu": torch.cuda.get_device_name(0),
        "artifacts": [
            "integrated.h5ad",
            "latent_embeddings.csv",
            "umap_embeddings.csv",
            "preview.png",
            "model/",
        ],
        "research_only": True,
        "clinical_use": False,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    archive_path = output_dir.parent / "scvi-result.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir))
    return archive_path


@app.post("/v1/fit-transform", response_class=FileResponse)
async def train(
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    method: Annotated[str, Form(pattern="^(scvi|scanvi)$")] = "scvi",
    batch_key: Annotated[str | None, Form()] = None,
    labels_key: Annotated[str | None, Form()] = None,
    unlabeled_category: Annotated[str, Form(min_length=1, max_length=128)] = "Unknown",
    max_epochs: Annotated[int, Form(ge=1, le=20)] = 20,
    n_latent: Annotated[int, Form(ge=2, le=64)] = 10,
    seed: Annotated[int, Form(ge=0, le=2_147_483_647)] = 0,
    research_only: Annotated[bool, Form()] = False,
) -> FileResponse:
    if not research_only:
        raise HTTPException(status_code=403, detail="research_only=true acknowledgement is required")
    if not torch.cuda.is_available():
        raise HTTPException(status_code=503, detail="CUDA is unavailable")

    temp_dir = Path(tempfile.mkdtemp(prefix="fs2-scvi-"))
    input_path = temp_dir / "input.h5ad"
    output_dir = temp_dir / "output"
    output_dir.mkdir()
    try:
        input_sha256 = await save_upload(file, input_path)
        async with training_lock:
            archive_path = await asyncio.to_thread(
                fit_transform,
                input_path,
                output_dir,
                method,
                batch_key,
                labels_key,
                unlabeled_category,
                max_epochs,
                n_latent,
                seed,
                input_sha256,
            )
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, TrainingError):
            logger.exception("fit-transform failed at stage=%s cause=%s", exc.stage, exc.cause_type)
            raise HTTPException(
                status_code=500,
                detail={"error": "training_failed", "stage": exc.stage, "cause": exc.cause_type},
            ) from exc
        logger.exception("fit-transform failed before training completed")
        raise HTTPException(
            status_code=500,
            detail={"error": "training_failed", "stage": "request", "cause": type(exc).__name__},
        ) from exc

    background_tasks.add_task(shutil.rmtree, temp_dir, True)
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename="scvi-result.zip",
        background=background_tasks,
    )
