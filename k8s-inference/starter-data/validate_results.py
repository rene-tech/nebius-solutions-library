#!/usr/bin/env python3
"""Validate downloaded starter results, never equating HTTP success with a pass.

This checks executable examples and output structure, not clinical accuracy or
scientific efficacy. A cohort explicitly names the exact pack used for its run.
No network calls or customer data discovery occur here.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import gzip
import io
import json
import subprocess
import tempfile
import wave
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import run_example as runner
from PIL import Image


def require(condition, code):
    if not condition:
        raise ValueError(code)


def finite(value):
    return bool(np.isfinite(np.asarray(value, dtype=float)).all())


class Result:
    def __init__(self, folder):
        self.folder = folder
        self.receipt = json.loads((folder / "receipt.json").read_bytes())
        self.files = {a["artifact_id"]: a for a in self.receipt["artifacts"]}
        self.value = json.loads((folder / "result.json").read_bytes())
        for item in self.files.values():
            self.read(item)
        self.binary = None
        if (
            self.value.get("schema")
            == "fs2-serve.nebius.ai/operation-artifact-result/v1"
        ):
            media = self.value["content_type"].split(";", 1)[0]
            self.binary = self.read(self.value["artifact"])
            if media == "application/json":
                self.value = json.loads(self.binary)

    def read(self, artifact):
        item = self.files[artifact["artifact_id"]]
        path = self.folder / item["local_path"]
        require(
            path.resolve().is_relative_to(self.folder.resolve()),
            "artifact_path_outside_run",
        )
        data = path.read_bytes()
        require(
            len(data) == artifact["size_bytes"]
            and runner.sha(data) == artifact["sha256"],
            "artifact_checksum_mismatch",
        )
        return data


def structure(text, format, expected_length=None):
    from Bio.PDB import MMCIFParser, PDBParser

    if format.lower() in ("cif", "mmcif"):
        # Occupancy is optional in a predicted mmCIF; Bio.PDB's structure
        # builder requires it. Validate the published coordinate columns
        # directly instead of altering a model's output to satisfy that parser.
        from Bio.PDB.MMCIF2Dict import MMCIF2Dict

        parsed = MMCIF2Dict(io.StringIO(text))
        coordinates = [parsed["_atom_site.Cartn_" + axis] for axis in "xyz"]
        require(
            len(set(map(len, coordinates))) == 1
            and len(coordinates[0]) >= 8
            and finite(coordinates),
            "structure_coordinates_invalid",
        )
        atoms = parsed.get(
            "_atom_site.label_atom_id", parsed.get("_atom_site.auth_atom_id", [])
        )
        residues = sum(atom == "CA" for atom in atoms)
        require(
            residues >= 8 and (expected_length is None or residues == expected_length),
            "structure_residues_invalid",
        )
        return {"atoms": len(coordinates[0]), "residues": residues}
    parser = (
        MMCIFParser(QUIET=True)
        if format.lower() in ("cif", "mmcif")
        else PDBParser(QUIET=True)
    )
    model = parser.get_structure("predicted", io.StringIO(text))
    atoms = list(model.get_atoms())
    require(
        len(atoms) >= 8 and finite([a.coord for a in atoms]),
        "structure_coordinates_invalid",
    )
    residues = [r for r in model.get_residues() if r.id[0] == " " and "CA" in r]
    require(len(residues) >= 8, "structure_residues_missing")
    if expected_length:
        require(len(residues) == expected_length, "structure_length_mismatch")
    return {"atoms": len(atoms), "residues": len(residues)}


def image_bytes(data, size):
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        require(image.size == tuple(size), "image_dimensions_mismatch")
        require(np.ptp(np.asarray(image).astype(float)) > 0, "image_is_constant")
        return {"width": image.width, "height": image.height}


def case_image_size(root, case):
    with Image.open(root / case["assets"][0]) as image:
        image.load()
        return image.size


def native(result, recipe, arguments, root, case):
    value, model = result.value, recipe["model_id"]
    if model == "sam2-1-hiera-large":
        width, height = case_image_size(root, case)
        with zipfile.ZipFile(io.BytesIO(result.binary)) as archive:
            require(
                set(archive.namelist()) == {"manifest.json", "mask.png", "overlay.png"},
                "sam_archive_members",
            )
            manifest = json.loads(archive.read("manifest.json"))
            source = (root / case["assets"][0]).read_bytes()
            require(
                manifest["input_sha256"] == runner.sha(source)
                and manifest["mode"] == arguments["mode"],
                "sam_input_identity",
            )
            labels = np.asarray(Image.open(io.BytesIO(archive.read("mask.png"))))
            require(
                labels.shape == (height, width)
                and labels.dtype.kind in "ui"
                and labels.max() > 0,
                "sam_labels_invalid",
            )
            require(
                manifest["objects"]
                and sum(o["area_px"] for o in manifest["objects"]) > 0,
                "sam_objects_empty",
            )
            image_bytes(archive.read("overlay.png"), (width, height))
            return {
                "objects": len(manifest["objects"]),
                "labelled_pixels": int(np.count_nonzero(labels)),
                "input_identity_verified": True,
            }
    if model == "cosmos3-nano":
        require(result.binary is not None, "video_missing")
        with tempfile.TemporaryDirectory(prefix="fs2-starter-video-") as temporary:
            path = Path(temporary) / "output.mp4"
            path.write_bytes(result.binary)
            probe = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-count_frames",
                        "-select_streams",
                        "v:0",
                        "-show_streams",
                        "-of",
                        "json",
                        str(path),
                    ]
                )
            )["streams"][0]
            width, height = map(int, arguments["size"].split("x"))
            require(
                (probe["width"], probe["height"]) == (width, height), "video_dimensions"
            )
            require(
                int(probe["nb_read_frames"]) == arguments["num_frames"],
                "video_frame_count",
            )
            pixels = subprocess.check_output(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-vf",
                    "scale=32:32",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "gray",
                    "pipe:1",
                ]
            )
            frames = np.frombuffer(pixels, dtype=np.uint8).reshape(-1, 32, 32)
            require(
                np.ptp(frames) > 10 and np.any(frames[1:] != frames[:-1]),
                "video_static_or_empty",
            )
            return {
                "width": width,
                "height": height,
                "decoded_frames": len(frames),
                "changed_frames": int(
                    np.any(frames[1:] != frames[:-1], axis=(1, 2)).sum()
                ),
                "physical_alignment_verified": False,
            }
    if model in {"qwen3-8b", "nv-reason-cxr-3b"}:
        choice = value["choices"][0]
        text = choice["message"].get("content", "")
        require(choice["finish_reason"] == "stop", "chat_truncated_or_unfinished")
        require(isinstance(text, str) and len(text.strip()) > 15, "chat_empty")
        final = text.split("</think>")[-1].strip()
        require(
            not ("<think>" in final) and len(final) > 15,
            "chat_reasoning_without_answer",
        )
        if model == "nv-reason-cxr-3b":
            require(
                "please provide" not in final.lower()
                and "no image" not in final.lower(),
                "cxr_image_not_interpreted",
            )
        return {"final_characters": len(final), "finish_reason": "stop"}
    if model in {"altumage", "phenoage"}:
        predictions = value["predictions"]
        require(len(predictions) == len(arguments["samples"]), "aging_sample_count")
        metric = (
            "predicted_chronological_age_years"
            if model == "altumage"
            else "phenotypic_age_years"
        )
        for prediction, sample in zip(predictions, arguments["samples"], strict=True):
            require(
                prediction["sample_id"] == sample["sample_id"]
                and finite(prediction[metric]),
                "aging_identity_or_value",
            )
        if model == "altumage":
            require(
                value["feature_count"] == 20318
                and all(p["imputed_cpg_count"] == 0 for p in predictions),
                "aging_features",
            )
        return {"predictions": len(predictions), "finite_values": True}
    if model == "evo2-40b":
        sequence = value["sequence"]
        require(
            len(sequence) == arguments["num_tokens"] and set(sequence) <= set("ACGT"),
            "dna_completion_invalid",
        )
        return {"generated_bases": len(sequence)}
    if model == "msa-search-pdb70":
        alignments = value["alignments"]
        text = next(iter(next(iter(alignments.values())).values()))["alignment"]
        sequence = arguments.get("sequence")
        require(text.startswith(">") and sequence in text, "msa_query_missing")
        return {"alignment_records": text.count(">")}
    if model == "openfold2":
        structures = value["structures_in_ranked_order"]
        require(bool(structures), "structures_missing")
        return {
            "structures": [
                structure(s["structure"], s["format"], len(arguments["sequence"]))
                for s in structures
            ]
        }
    if model in {"boltz2", "openfold3"}:
        candidates = []

        def visit(item):
            if isinstance(item, dict):
                if isinstance(item.get("structure"), str):
                    candidates.append(
                        structure(item["structure"], item.get("format", "pdb"))
                    )
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        require(bool(candidates), "structures_missing")
        return {"structures": candidates}
    if model == "proteinmpnn":
        fasta = value["mfasta"]
        sequences = [
            "".join(s.splitlines()[1:]).replace("/", "") for s in fasta.split(">")[1:]
        ]
        require(
            len(sequences) >= 2
            and all(
                set(s) <= set("ACDEFGHIKLMNPQRSTVWY") and len(s) >= 8 for s in sequences
            ),
            "designed_sequence_invalid",
        )
        require(len({len(s) for s in sequences}) == 1, "designed_sequence_length")
        return {"sequences": len(sequences), "residues": len(sequences[0])}
    if model in {"molmim", "genmol", "diffdock"}:
        from rdkit import Chem

        if model == "diffdock":
            mols = [
                Chem.MolFromMolBlock(s, sanitize=True, removeHs=False)
                for s in value["ligand_positions"]
            ]
            require(
                len(mols) == len(value["position_confidence"])
                and finite(value["position_confidence"]),
                "docking_scores_invalid",
            )
            for mol in mols:
                require(
                    mol is not None
                    and mol.GetNumConformers() > 0
                    and finite(mol.GetConformer().GetPositions()),
                    "docking_coordinates_invalid",
                )
        else:
            items = value.get("molecules", value.get("generated", []))
            mols = [
                Chem.MolFromSmiles(
                    item["sample"] if model == "molmim" else item["smiles"]
                )
                for item in items
            ]
            if model == "molmim":
                require(
                    all(
                        item["changed_from_input"]
                        and item["model_decoded"]
                        and finite(item["score"])
                        for item in items
                    ),
                    "molecule_not_generated_or_invalid_score",
                )
        require(
            bool(mols)
            and all(mol is not None and mol.GetNumAtoms() > 0 for mol in mols),
            "molecules_invalid",
        )
        return {"molecules": len(mols)}
    if model == "sdxl":
        data = base64.b64decode(value["data"][0]["b64_json"], validate=True)
        require(runner.sha(data) == value["png_sha256"], "image_digest_mismatch")
        return image_bytes(
            data, (arguments.get("width", 512), arguments.get("height", 512))
        )
    if model == "cellpose-cpsam-v2":
        width, height = case_image_size(root, case)
        data = base64.b64decode(value["mask_base64"], validate=True)
        with Image.open(io.BytesIO(data)) as mask:
            labels = np.asarray(mask)
            require(
                mask.size == (width, height)
                and np.issubdtype(labels.dtype, np.integer),
                "cell_mask_invalid",
            )
            require(
                len(np.unique(labels[labels > 0])) == value["object_count"],
                "cell_count_inconsistent",
            )
        return {"objects": value["object_count"], "shape": list(labels.shape)}
    if model == "nv-segment-ct":
        import nibabel as nib

        encoded = value.get(
            "segmentation_nifti_base64", value.get("output_nifti_base64", "")
        )
        data = base64.b64decode(encoded, validate=True)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        volume = nib.Nifti1Image.from_bytes(data).get_fdata()
        require(
            volume.shape == (64, 64, 64)
            and finite(volume)
            and np.all(volume == np.floor(volume)),
            "volume_mask_invalid",
        )
        return {"shape": list(volume.shape), "label_count": len(np.unique(volume))}
    if model == "scvi-scanvi":
        import anndata as ad

        with zipfile.ZipFile(io.BytesIO(result.binary)) as bundle:
            info = bundle.getinfo("integrated.h5ad")
            require(info.file_size < 32 * 1024 * 1024, "h5ad_output_too_large")
            with tempfile.TemporaryDirectory(prefix="starter-h5ad-") as tmp:
                path = Path(tmp) / "integrated.h5ad"
                path.write_bytes(bundle.read(info))
                data = ad.read_h5ad(path)
                original = ad.read_h5ad(root / case["assets"][0])
                require(
                    data.shape == original.shape
                    and list(data.obs_names) == list(original.obs_names),
                    "single_cell_identity_changed",
                )
                metadata = json.loads(bundle.read("manifest.json"))
                require(
                    metadata["method"] == arguments["method"],
                    "single_cell_method_mismatch",
                )
                require(
                    metadata["input_sha256"]
                    == runner.sha((root / case["assets"][0]).read_bytes()),
                    "single_cell_input_mismatch",
                )
                # Both trained-model types use the runtime's published export
                # key. scANVI exports its semi-supervised latent space, not
                # predicted labels; the example must not promise annotations.
                key = "X_scvi"
                latent = data.obsm[key]
                require(
                    latent.shape == (data.n_obs, arguments["n_latent"])
                    and finite(latent),
                    "single_cell_latent_invalid",
                )
                return {
                    "cells": data.n_obs,
                    "genes": data.n_vars,
                    "latent_dimensions": latent.shape[1],
                }
    if model == "magpie-tts-multilingual-357m":
        with wave.open(io.BytesIO(result.binary)) as wav:
            duration = wav.getnframes() / wav.getframerate()
            require(
                0.5 < duration < 120 and wav.getnchannels() in (1, 2),
                "tts_audio_invalid",
            )
            pcm = wav.readframes(wav.getnframes())
            require(len(set(pcm)) > 2, "tts_audio_silent")
            return {"audio_seconds": duration, "sample_rate": wav.getframerate()}
    if "speech" in model or model.startswith("parakeet") or model.startswith("diar-"):
        seconds = value["audio_seconds"]
        require(finite(seconds) and seconds > 400, "speech_full_recording_missing")
        if model.startswith("diar-"):
            events = value.get("events", [])
            require(bool(events), "speaker_activity_missing")
            last_time, frames = 0.0, 0
            for event in events:
                probabilities = np.asarray(event["probabilities"])
                require(
                    event["type"] == "speaker.activity"
                    and probabilities.ndim == 2
                    and probabilities.shape[1] == len(event["speakers"])
                    and finite(probabilities)
                    and np.all((probabilities >= 0) & (probabilities <= 1)),
                    "speaker_activity_invalid",
                )
                require(
                    last_time <= event["start_seconds"] <= seconds,
                    "speaker_timestamps_invalid",
                )
                last_time = event["start_seconds"]
                frames += len(probabilities)
            return {
                "audio_seconds": seconds,
                "speaker_activity_events": len(events),
                "frames": frames,
            }
        require(
            isinstance(value.get("text"), str) and len(value["text"]) > 500,
            "speech_transcript_missing",
        )
        for segment in value.get("segments", []):
            for item in segment.get("items", []):
                require(
                    0 <= item["start_seconds"] <= item["end_seconds"] <= seconds + 1,
                    "speech_timestamps_invalid",
                )
        return {"audio_seconds": seconds, "transcript_characters": len(value["text"])}
    raise ValueError("validator_not_implemented_" + model)


def scientific(result):
    value = result.value
    if isinstance(value.get("result"), dict):
        value = value["result"]
    require(value["terminal_status"] == "succeeded", "scientific_status_not_succeeded")
    require(
        value["semantic_validation"]["status"] == "passed",
        "runtime_semantic_validation_not_passed",
    )
    manifest = json.loads(result.read(value["output_manifest"]))
    entries = manifest["entries"]
    require(bool(entries), "scientific_outputs_missing")
    structures, tables = [], []
    for entry in entries:
        artifact = entry["artifact"]
        data = result.read(artifact)
        if artifact["media_type"] in {
            "chemical/x-pdb",
            "chemical/x-cif",
            "chemical/x-mmcif",
        }:
            structures.append(
                structure(
                    data.decode(), "cif" if "cif" in artifact["media_type"] else "pdb"
                )
            )
        elif artifact["media_type"] == "text/csv":
            rows = list(csv.DictReader(io.StringIO(data.decode())))
            require(
                bool(rows) and len(rows[0]) > 1 and None not in rows[0],
                "scientific_table_invalid",
            )
            tables.append({"rows": len(rows), "columns": len(rows[0])})
    return {
        "semantic_validation": "passed",
        "artifacts": len(entries),
        "parsed_structures": structures,
        "parsed_tables": tables,
    }


async def validate_cohort(root, folder):
    manifest = json.loads((root / "manifest.json").read_bytes())
    inputs = runner.Inputs(root, manifest, runner.offline_upload)
    results = []
    for case in manifest["cases"]:
        for recipe in json.loads(inputs.read(case["recipes"]))["recipes"]:
            location = folder / case["id"] / recipe["model_id"]
            if not (location / "receipt.json").exists():
                continue
            receipt = json.loads((location / "receipt.json").read_bytes())
            record = {
                "case_id": case["id"],
                "model_id": recipe["model_id"],
                "operation_id": receipt.get("operation_id"),
                "recipe_sha256": runner.sha(runner.encoded(recipe)),
                "state": "not-complete",
                "input_sha256": None,
            }
            try:
                arguments = await inputs.materialize(recipe["arguments"])
                record["input_sha256"] = runner.sha(runner.encoded(arguments))
                require(
                    record["recipe_sha256"] == receipt["identity"]["recipe_sha256"],
                    "receipt_recipe_mismatch",
                )
                require(
                    receipt.get("input_sha256", record["input_sha256"])
                    == record["input_sha256"],
                    "receipt_input_mismatch",
                )
                if receipt["state"] != "succeeded":
                    record.update(state=receipt["state"])
                else:
                    result = Result(location)
                    record["checks"] = (
                        scientific(result)
                        if recipe["protocol"] == "scientific-batch-v1"
                        else native(result, recipe, arguments, root, case)
                    )
                    if recipe["model_id"] == "cosmos3-lerobot-augmentation":
                        proof = json.loads(
                            (location / "lerobot-reader-proof.json").read_bytes()
                        )
                        source_sha = runner.sha((root / case["assets"][0]).read_bytes())
                        require(
                            proof["status"] == "passed"
                            and proof["operation_id"] == receipt["operation_id"]
                            and proof["source_sha256"] == source_sha
                            and proof["nonvideo_values_exact"] is True
                            and any(
                                a["sha256"] == proof["output_sha256"]
                                for a in receipt["artifacts"]
                            ),
                            "lerobot_independent_reader_proof_mismatch",
                        )
                        record["checks"]["independent_reader"] = proof
                    record.update(
                        state="passed",
                        completed_at=receipt["completed_at"],
                        observation_seconds=receipt["observation_seconds"],
                        result_sha256=runner.sha(
                            (location / "result.json").read_bytes()
                        ),
                        artifacts=[
                            {
                                k: a[k]
                                for k in (
                                    "artifact_id",
                                    "sha256",
                                    "size_bytes",
                                    "media_type",
                                )
                            }
                            for a in receipt["artifacts"]
                        ],
                    )
                    operation = json.loads((location / "operation.json").read_bytes())
                    if isinstance(operation.get("operation"), dict):
                        operation = operation["operation"]
                    record["lifecycle"] = {
                        k: operation.get(k)
                        for k in (
                            "accepted_at",
                            "activation_started_at",
                            "ready_at",
                            "started_at",
                            "completed_at",
                            "cold_start_seconds",
                            "model_revision",
                            "runtime",
                            "attempt",
                            "status",
                            "tenant_id",
                            "principal_id",
                        )
                    }
                    accepted, completed = (
                        operation.get("accepted_at"),
                        operation.get("completed_at"),
                    )
                    if accepted and completed:
                        record["server_elapsed_seconds"] = round(
                            (
                                datetime.fromisoformat(completed)
                                - datetime.fromisoformat(accepted)
                            ).total_seconds(),
                            3,
                        )
            except Exception as exc:
                record.update(
                    state="failed-validation",
                    error_type=type(exc).__name__,
                    error_code=str(exc)[:160],
                )
            results.append(record)
    return {
        "at": datetime.now(UTC).isoformat(),
        "pack_manifest_sha256": runner.sha((root / "manifest.json").read_bytes()),
        "scope": "example input/output functionality only; not clinical or scientific accuracy qualification",
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(validate_cohort(args.pack, args.cohort))
    runner.save(args.output, report)
    print(
        json.dumps(
            {
                "counts": {
                    s: sum(r["state"] == s for r in report["results"])
                    for s in sorted({r["state"] for r in report["results"]})
                },
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
