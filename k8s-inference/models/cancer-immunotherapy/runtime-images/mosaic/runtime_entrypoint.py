#!/usr/bin/env python3
"""Execute the mosaic scientific-batch argv contract with external artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any


BACKEND_ID = "mosaic-boltz2-proteinmpnn-v1"
SOURCE_REVISION = "70fec525423f5f87156a1a957b4a4048f9f8e676"
RECIPE_SHA256 = "cbfc7a88e6e7c2255730218bbdeaf6fc272d721b6c792231429a923309a8e0fe"
BOLTZ2_SHA256 = "090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1"
PROTEINMPNN_SHA256 = "c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd"
SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
# mosaic.common.TOKENS order, one-letter to PDB three-letter residue name.
AA3 = {
    "A": "ALA", "R": "ARG", "N": "ASN", "D": "ASP", "C": "CYS",
    "Q": "GLN", "E": "GLU", "G": "GLY", "H": "HIS", "I": "ILE",
    "L": "LEU", "K": "LYS", "M": "MET", "F": "PHE", "P": "PRO",
    "S": "SER", "T": "THR", "W": "TRP", "Y": "TYR", "V": "VAL",
}


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        + b"\n"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path, expected: str, label: str) -> Path:
    if not path.is_file() or _sha256(path) != expected:
        raise SystemExit(f"{label} is missing or differs from sha256:{expected}: {path}")
    return path


def _load(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return value


def _artifact_root() -> Path:
    return Path(os.environ.get("FS2_ARTIFACT_ROOT", "/opt/fs2/artifacts"))


def _target_sequence(manifest: dict[str, Any]) -> str:
    entries = manifest.get("entries", [])
    if len(entries) != 1 or entries[0].get("name") != "target_sequence":
        raise SystemExit("input manifest must contain exactly target_sequence")
    pointer = entries[0]["artifact"]
    artifact_id = pointer["artifact_id"]
    candidates = [
        _artifact_root() / "inputs" / artifact_id,
        _artifact_root() / artifact_id,
    ]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise SystemExit(f"external target artifact is missing: {artifact_id}")
    if path.stat().st_size != pointer["size_bytes"] or _sha256(path) != pointer["sha256"]:
        raise SystemExit(f"external target artifact differs from its pointer: {artifact_id}")
    lines = [line.strip() for line in path.read_text(encoding="ascii").splitlines() if line.strip()]
    sequence = "".join(lines[1:]).upper() if lines and lines[0].startswith(">") else ""
    if not sequence:
        raise SystemExit("external target artifact is not a FASTA record")
    return sequence


def _binder_pdb(structure: Any, sequence: str) -> bytes:
    """Serialise the binder chain with the designed residue identities.

    Mosaic optimises a relaxed 20-way distribution, so the Gemmi structure the
    Boltz-2 writer returns names every binder residue ``UNK``. Emitting that
    verbatim throws away the designed sequence: the canonical adapter output
    validator cannot recover a sequence from it and the structural validator
    counts zero standard residues. The designed one-letter identities are
    therefore written into the residue-name column, in residue order, and the
    residue count must agree with the sequence the same shard reported.
    """
    lines = structure.make_pdb_string().splitlines()
    atoms = [line for line in lines if line.startswith("ATOM")]
    if not atoms:
        raise SystemExit("mosaic prediction did not contain protein atoms")
    binder_chain = atoms[0][21:22]
    selected = [line for line in atoms if line[21:22] == binder_chain]
    if not selected:
        raise SystemExit("mosaic prediction did not contain a binder chain")
    order: dict[tuple[str, str], int] = {}
    named: list[str] = []
    for line in selected:
        if len(line) < 54:
            raise SystemExit("mosaic prediction contains a truncated ATOM record")
        residue = (line[22:26], line[26:27])
        position = order.setdefault(residue, len(order))
        if position >= len(sequence):
            raise SystemExit(
                "mosaic prediction has more binder residues than the designed sequence"
            )
        named.append(f"{line[:17]}{AA3[sequence[position]]:>3}{line[20:]}")
    if len(order) != len(sequence):
        raise SystemExit(
            f"mosaic prediction has {len(order)} binder residues "
            f"but the designed sequence has {len(sequence)}"
        )
    return ("\n".join(named) + "\nEND\n").encode("ascii")


def _run_shard(args: argparse.Namespace) -> None:
    request = _load(args.request)
    manifest = _load(args.input_manifest)
    if _sha256(Path(args.recipe)) != args.recipe_sha256 or args.recipe_sha256 != RECIPE_SHA256:
        raise SystemExit("mosaic recipe identity mismatch")
    parameters = request["parameters"]
    if args.seed != parameters["base_seed"] + args.shard_index:
        raise SystemExit("mosaic shard seed differs from request")
    target = _target_sequence(manifest)

    artifact_root = _artifact_root()
    boltz_cache = artifact_root / "mosaic" / "boltz"
    boltz_checkpoint = _verified(
        boltz_cache / "boltz2_conf.ckpt", BOLTZ2_SHA256, "Boltz-2 checkpoint"
    )
    mpnn_checkpoint = _verified(
        artifact_root / "mosaic" / "proteinmpnn" / "v_48_020.pt",
        PROTEINMPNN_SHA256,
        "ProteinMPNN checkpoint",
    )
    # Boltz2 mode loads canonical molecules directly from mols/.  The legacy
    # Boltz1 ccd.pkl path is deliberately not part of this adapter contract.
    if not (boltz_cache / "mols").is_dir():
        raise SystemExit(f"external Boltz-2 CCD artifacts are incomplete: {boltz_cache}")

    os.environ["MOSAIC_CACHE_DIR"] = str(artifact_root / "mosaic")
    os.environ["BOLTZ_CACHE"] = str(boltz_cache)
    import jax
    import jax.numpy as jnp
    import numpy as np
    from mosaic.common import TOKENS
    from mosaic.losses.protein_mpnn import InverseFoldingSequenceRecovery
    from mosaic.losses.structure_prediction import BinderTargetContact, PLDDTLoss, WithinBinderContact
    from mosaic.models.boltz2 import Boltz2
    from mosaic.optimizers import simplex_APGM
    from mosaic.proteinmpnn.mpnn import ProteinMPNN
    from mosaic.structure_prediction import TargetChain

    model = Boltz2(cache_path=boltz_checkpoint)
    features, writer = model.binder_features(
        binder_length=parameters["binder_length"],
        chains=[TargetChain(target, use_msa=False)],
    )
    mpnn = ProteinMPNN.from_pretrained(mpnn_checkpoint)
    loss = model.build_loss(
        loss=(
            2.0 * BinderTargetContact(epitope_idx=[item - 1 for item in parameters["hotspots"]])
            + WithinBinderContact()
            + 0.2 * PLDDTLoss()
            + 5.0 * InverseFoldingSequenceRecovery(mpnn, temp=jnp.array(0.01))
        ),
        features=features,
        recycling_steps=1,
        sampling_steps=25,
    )
    key = jax.random.key(args.seed)
    initial = jax.nn.softmax(
        0.5 * jax.random.gumbel(key, shape=(parameters["binder_length"], 20))
    )
    _, design = simplex_APGM(
        loss_function=loss,
        n_steps=parameters["optimizer_steps"],
        x=initial,
        stepsize=0.1,
        momentum=0.0,
        key=key,
    )
    objective, _ = loss(design, key=jax.random.fold_in(key, 1))
    prediction = model.predict(
        PSSM=design,
        features=features,
        writer=writer,
        recycling_steps=1,
        sampling_steps=25,
        key=jax.random.fold_in(key, 2),
    )
    sequence = "".join(TOKENS[index] for index in np.asarray(design).argmax(-1))
    mean_plddt = float(np.asarray(prediction.plddt)[: len(sequence)].mean())
    if mean_plddt > 1.0:
        mean_plddt /= 100.0
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "shard-result.json").write_bytes(_canonical({
        "backend_id": BACKEND_ID,
        "source_revision": SOURCE_REVISION,
        "recipe_sha256": RECIPE_SHA256,
        "index": args.shard_index,
        "seed": args.seed,
        "status": "succeeded",
    }))
    (output / "candidate-metrics.json").write_bytes(_canonical({
        "candidate_id": f"design-{args.shard_index:03d}",
        "shard_index": args.shard_index,
        "seed": args.seed,
        "sequence": sequence,
        "iptm": max(0.0, min(1.0, float(prediction.iptm))),
        "mean_plddt": max(0.0, min(1.0, mean_plddt)),
        "objective": float(objective),
    }))
    (output / "candidate.pdb").write_bytes(_binder_pdb(prediction.st, sequence))
    print(json.dumps({
        "status": "succeeded",
        "backend_id": BACKEND_ID,
        "shard_index": args.shard_index,
        "seed": args.seed,
        "gpu": jax.devices()[0].device_kind,
        "candidate_sequence": sequence,
    }, sort_keys=True))


def _pointer(path: Path, artifact_id: str, media_type: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "media_type": media_type,
        "compression": "none",
    }


def _aggregate(args: argparse.Namespace) -> None:
    request = _load(args.request)
    _load(args.input_manifest)
    digest = os.environ.get("FS2_RUNTIME_IMAGE_DIGEST", "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise SystemExit("FS2_RUNTIME_IMAGE_DIGEST must bind aggregate output to the admitted image")
    root = Path(args.output_manifest).parent
    root.mkdir(parents=True, exist_ok=True)
    artifact_dir = root / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    entries: list[dict[str, Any]] = []
    index: dict[str, str] = {}

    def add(name: str, semantic_type: str, source: Path, artifact_id: str, media: str) -> None:
        target = artifact_dir / artifact_id
        shutil.copyfile(source, target)
        index[artifact_id] = str(target)
        entries.append({"name": name, "semantic_type": semantic_type, "artifact": _pointer(target, artifact_id, media)})

    shards = Path(args.shards)
    for shard_index in range(args.expected_shards):
        shard = shards / f"{shard_index:03d}"
        add(f"shard-{shard_index:03d}", "mosaic-shard-result-json/v1", shard / "shard-result.json", f"artifact.mosaic.shard.{shard_index:03d}", "application/json")

    aggregate = {
        "backend_id": BACKEND_ID,
        "source_revision": SOURCE_REVISION,
        "recipe_sha256": RECIPE_SHA256,
        "request_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
        "runtime_image_digest": digest,
        "expected_shards": args.expected_shards,
        "succeeded_shards": args.expected_shards,
        "atomic_commit": bool(args.atomic_rename),
    }
    aggregate_path = artifact_dir / "aggregate.json"
    aggregate_path.write_bytes(_canonical(aggregate))
    add("aggregate", "mosaic-aggregate-json/v1", aggregate_path, "artifact.mosaic.aggregate", "application/json")
    for shard_index in range(args.expected_shards):
        shard = shards / f"{shard_index:03d}"
        add(f"candidate-{shard_index:03d}-metrics", "mosaic-design-metrics-json/v1", shard / "candidate-metrics.json", f"artifact.mosaic.candidate.{shard_index:03d}.metrics", "application/json")
        add(f"candidate-{shard_index:03d}-structure", "protein-structure-pdb/v1", shard / "candidate.pdb", f"artifact.mosaic.candidate.{shard_index:03d}.pdb", "chemical/x-pdb")
    manifest = {"schema": SCHEMA, "manifest_id": "manifest.mosaic.output", "entries": entries}
    staging = Path(args.staging_manifest)
    staging.write_bytes(_canonical(manifest))
    (root / "artifact-index.json").write_bytes(_canonical(index))
    if not args.atomic_rename:
        raise SystemExit("mosaic aggregate requires --atomic-rename")
    os.replace(staging, args.output_manifest)
    print(json.dumps({"status": "succeeded", "candidates": args.expected_shards, "output_manifest": args.output_manifest}, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    shard = commands.add_parser("run-shard")
    shard.add_argument("--request", required=True)
    shard.add_argument("--input-manifest", required=True)
    shard.add_argument("--recipe", required=True)
    shard.add_argument("--recipe-sha256", required=True)
    shard.add_argument("--shard-index", type=int, required=True)
    shard.add_argument("--seed", type=int, required=True)
    shard.add_argument("--output", required=True)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--request", required=True)
    aggregate.add_argument("--input-manifest", required=True)
    aggregate.add_argument("--shards", required=True)
    aggregate.add_argument("--expected-shards", type=int, required=True)
    aggregate.add_argument("--staging-manifest", required=True)
    aggregate.add_argument("--output-manifest", required=True)
    aggregate.add_argument("--atomic-rename", action="store_true")
    return parser


if __name__ == "__main__":
    arguments = _parser().parse_args()
    _run_shard(arguments) if arguments.action == "run-shard" else _aggregate(arguments)
