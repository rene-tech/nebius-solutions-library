from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest

from fs2_serve.runtime import RuntimeClient, RuntimeProtocolError


def _result_zip(*, method: str = "scvi", research_only: bool = True) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "method": method,
                    "cells": 32,
                    "genes": 16,
                    "latent_dimensions": 4,
                    "research_only": research_only,
                    "clinical_use": False,
                }
            ),
        )
        archive.writestr("integrated.h5ad", b"HDF5-fixture")
        archive.writestr("latent_embeddings.csv", "cell,z0,z1\ncell-0,0,1\n")
        archive.writestr("model/model.pt", b"model-fixture")
    return target.getvalue()


def _sam2_result_zip(
    *,
    mode: str = "prompted-image",
    checkpoint: str = "2647878d5dfa5098f2f8649825738a9345572bae2d4350a2468587ece47dd318",
) -> bytes:
    target = io.BytesIO()
    manifest = {
        "schema": "fs2.nebius.ai/sam2-result/v1",
        "model": "facebook/sam2.1-hiera-large",
        "revision": "665f8e2ad61cf5f53d65644ff27c8ee525124610",
        "checkpoint_sha256": checkpoint,
        "mode": mode,
        "input_sha256": "0" * 64,
        "width": 320,
        "height": 240,
        "objects": [{"object_id": 1, "area_px": 100}],
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("mask.png", b"PNG-mask")
        archive.writestr("overlay.png", b"PNG-overlay")
    return target.getvalue()


def test_scvi_json_contract_translates_to_bounded_multipart_fields() -> None:
    body = json.dumps(
        {
            "anndata_base64": base64.b64encode(b"HDF5-input").decode(),
            "filename": "cells.h5ad",
            "method": "scanvi",
            "batch_key": "batch",
            "labels_key": "label",
            "unlabeled_category": "Unknown",
            "max_epochs": 2,
            "n_latent": 4,
            "seed": 7,
            "research_only": True,
        }
    ).encode()
    form, files = RuntimeClient._scvi_multipart(body)
    assert form == {
        "method": "scanvi",
        "batch_key": "batch",
        "labels_key": "label",
        "unlabeled_category": "Unknown",
        "max_epochs": "2",
        "n_latent": "4",
        "seed": "7",
        "research_only": "true",
    }
    assert files == {"file": ("cells.h5ad", b"HDF5-input", "application/x-hdf5")}


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"anndata_base64":"not-base64","filename":"cells.h5ad"}',
        json.dumps({"anndata_base64": "", "filename": "cells.h5ad"}).encode(),
    ],
)
def test_scvi_multipart_rejects_missing_invalid_or_empty_content(payload: bytes) -> None:
    with pytest.raises(RuntimeProtocolError, match="scVI"):
        RuntimeClient._scvi_multipart(payload)


def test_scvi_zip_validator_accepts_complete_research_result() -> None:
    RuntimeClient._scvi_zip_valid(_result_zip(), "application/zip")


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not-a-zip", "application/zip"),
        (_result_zip(), "application/octet-stream"),
        (_result_zip(method="other"), "application/zip"),
        (_result_zip(research_only=False), "application/zip"),
    ],
)
def test_scvi_zip_validator_rejects_wrong_container_or_manifest(body: bytes, content_type: str) -> None:
    with pytest.raises(RuntimeProtocolError, match="scVI"):
        RuntimeClient._scvi_zip_valid(body, content_type)


def test_sam2_zip_validator_accepts_pinned_image_result() -> None:
    RuntimeClient._sam2_zip_valid(_sam2_result_zip(), "application/zip")


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not-a-zip", "application/zip"),
        (_sam2_result_zip(), "application/octet-stream"),
        (_sam2_result_zip(checkpoint="0" * 64), "application/zip"),
        (_sam2_result_zip(mode="invented"), "application/zip"),
    ],
)
def test_sam2_zip_validator_rejects_wrong_container_or_manifest(body: bytes, content_type: str) -> None:
    with pytest.raises(RuntimeProtocolError, match="SAM 2"):
        RuntimeClient._sam2_zip_valid(body, content_type)
