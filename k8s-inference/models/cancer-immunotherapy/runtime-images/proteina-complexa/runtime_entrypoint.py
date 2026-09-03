#!/usr/bin/env python3
"""Shell-free Proteina-Complexa scientific-batch runtime entrypoint.

The scientific batch controller launches this module directly as ``argv`` --
there is no shell, no ``.env`` file and no interactive wrapper anywhere in the
path.  It owns five responsibilities that the upstream project does not:

1. **Exact checkpoint-pair selection.**  Each Complexa variant is driven by a
   *pair* of public checkpoints (a score model and its partial autoencoder).
   The pairs are pinned here by artifact id, file name, byte count and
   SHA-256, so a run can never silently mix the ligand score model with the
   AME autoencoder.
2. **Immutable generation verification.**  Checkpoints arrive as content
   addressed generations on the shared reference-data host plane, promoted by
   the ingestion successor's terminal run.  Before a GPU is touched the mount
   must prove it is the pinned generation: the reserved marker must describe
   this artifact, generation, sub-path, plane, licence visibility and inventory
   algorithm, the marker's own document digest must equal what the promotion
   receipt published, and a tree digest recomputed from the mounted bytes must
   reproduce the generation name.  Per-file byte counts are always checked and
   content digests are verified when asked for.
3. **Target resolution.**  Upstream's target dictionaries carry *relative*
   ``target_path`` values (``./assets/target_data/...``), so a run only
   resolves its target when the process happens to be started from the source
   tree.  Under a container ``workingDir`` of ``/workspace`` the
   conditional-feature constructor raises ``FileNotFoundError``.  Passing an
   absolute Hydra override is not available as a fix: most upstream task names
   begin with a digit (``02_PDL1``, ``39_7V11_LIGAND``) and Hydra's override
   grammar only admits key segments that start with a letter or underscore, so
   ``++generation.target_dict_cfg.02_PDL1.target_path=...`` is a parse error.
   Instead the image bakes a read-only working directory whose ``assets``
   link resolves upstream's own relative path, so the binding is part of the
   image digest rather than a writable ``.env`` or a symlink created at run
   time.  Target structures are treated as a real artifact: each is pinned by
   digest against the upstream source archive and verified before model load,
   because a run that loads both checkpoints and then cannot find its target
   has failed on a missing artifact.
4. **Phase timing and truthful cache reporting.**  Upstream reports one
   ``Total generation time``.  The controller needs the split between
   interpreter import, checkpoint load and sampling, which is recovered from
   the upstream log's own timestamps rather than guessed.
5. **Model-specific semantic validation.**  A zero exit code is necessary but
   not sufficient.  Structures are parsed, coordinates are checked for
   degeneracy, and each variant's own expectations (a binder chain within the
   target's length envelope, a ligand residue actually present in the complex,
   LoRA adapters actually re-applied) must hold before the run is called PASS.

Nothing here claims a GPU snapshot: ``gpu_snapshot`` is always reported as not
captured and not restored, because no device snapshot is taken or restored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import zlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_REQUEST = "fs2-serve.nebius.ai/proteina-complexa-batch-request/v1"
SCHEMA_RESULT = "fs2-serve.nebius.ai/proteina-complexa-batch-result/v1"

BACKEND_ID = "proteina-complexa-native-upstream-v1"
MODEL_ID = "proteina-complexa"
SOURCE_REVISION = "54058860d43444c7289873f77d3e50b5b02348cd"
SOURCE_ROOT = Path("/opt/fs2/source")

# The three public Complexa releases, each a (score model, partial autoencoder)
# pair.  Byte counts and digests are the upstream published identities; they are
# also what the regional ingestion receipt records for the staged copies.
VARIANTS: dict[str, dict[str, Any]] = {
    "protein": {
        "artifact_id": "complexa-protein",
        "model_id": "proteina-complexa-protein-target-160m-v1",
        "source_uri": "hf://nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1",
        "source_revision": "ffed199e32612b98ffa04f4640d34d37b137fca5",
        "checkpoint": {
            "name": "complexa.ckpt",
            "bytes": 2934289381,
            "sha256": "589db1741f29838c7961386f6b873087238c72682e56189b89e0ae02610c19e9",
        },
        "autoencoder": {
            "name": "complexa_ae.ckpt",
            "bytes": 4100101779,
            "sha256": "35f8865efd269995eeaf1670e1c1085acfe2988c40abdeda8e09a0e15eb40816",
        },
        "pipeline": "search_binder_local_pipeline",
        "target_namespace": "target_dict_cfg",
        "target_dictionary": "configs/targets/targets_dict.yaml",
        "lora": False,
        "environment": {},
        "expects_ligand": False,
        "expects_motif": False,
        "default_task": "02_PDL1",
    },
    "ligand": {
        "artifact_id": "complexa-ligand",
        "model_id": "proteina-complexa-ligand-target-160m-v1",
        "source_uri": "hf://nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1",
        "source_revision": "bc90c8b2c701ceb52d5faef72600b6b5be880244",
        "checkpoint": {
            "name": "complexa_ligand.ckpt",
            "bytes": 1790554392,
            "sha256": "8175213eac5ec6433fed1756d055ce3f867129bfdb033040e1a0560ca558bfe5",
        },
        "autoencoder": {
            "name": "complexa_ligand_ae.ckpt",
            "bytes": 4100184649,
            "sha256": "898da17022cdeaaaea7caace41c8b6fe7bfcb78be4876b0113127eb9bb1527e6",
        },
        "pipeline": "search_ligand_binder_local_pipeline",
        "target_namespace": "target_dict_cfg",
        "target_dictionary": "configs/targets/ligand_targets_dict.yaml",
        "lora": True,
        "environment": {},
        "expects_ligand": True,
        "expects_motif": False,
        "default_task": "39_7V11_LIGAND",
    },
    "ame": {
        "artifact_id": "complexa-ame",
        "model_id": "proteina-complexa-ame-160m-v1",
        "source_uri": "hf://nvidia/NV-Proteina-Complexa-AME-160M-v1",
        "source_revision": "9743d749a8754080a32fda857d95579dfa4dabae",
        "checkpoint": {
            "name": "complexa_ame.ckpt",
            "bytes": 1792013880,
            "sha256": "d11319693d024d0427abc356a86350a694a1cb7dceb8db642c8041e5a20a9f7b",
        },
        "autoencoder": {
            "name": "complexa_ame_ae.ckpt",
            "bytes": 4100197925,
            "sha256": "63b1358c5459e968628094fdc9a2a6a95ac003606fcf7b1a3a21174458a69734",
        },
        "pipeline": "search_ame_local_pipeline",
        "target_namespace": "motif_target_dict_cfg",
        "target_dictionary": "configs/design_tasks/ame_dict_v2.yaml",
        "lora": True,
        # The AME release is trained against the v2 Complexa architecture; the
        # upstream pipeline config carries the same value in ``env_vars`` but
        # only the process environment is read by the architecture switch.
        "environment": {"USE_V2_COMPLEXA_ARCH": "True"},
        "expects_ligand": True,
        "expects_motif": True,
        "default_task": "M0024_1nzy_og",
    },
}

# RosettaFold3 is the reward and evaluation folding model for the ligand and AME
# pipelines.  It is bound (mounted and pointed at) for every variant so the
# reward path is available, and marker-verified, but it is only *exercised* when
# a request asks for the upstream default reward model.
RF3_ARTIFACT = {
    "artifact_id": "rosettafold3-checkpoint",
    "name": "rf3_foundry_01_24_latest_remapped.ckpt",
    "bytes": 3038876446,
    "sha256": "364ef592fd8042a9cf4176d045015190f8322f961ccca38d891b20ca578d3bb0",
    "license_id": "BSD-3-Clause",
    "role": "reward-and-evaluation",
}

# The immutable public generations promoted onto the shared reference-data host
# plane by the ingestion successor's terminal run r20260903b. A generation is
# named by its own content: the directory name equals the fs2-tree-inventory/v2
# digest of the tree it holds, so a mount either is that generation or is not.
HOST_ROOT = "/mnt/fs2-reference-data/data"
GENERATION_ROOT = "scientific-localization/public/generations"
MARKER_NAME = ".fs2-runtime-tree.json"
MARKER_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-generation-marker/v1"
INVENTORY_ALGORITHM = "fs2-tree-inventory/v2"

GENERATIONS: dict[str, dict[str, Any]] = {
    "complexa-protein": {
        "marker_sha256": "b84403b26e5ac4acd2ea3203d3ea7936e8ef009a9ac1c19fbcfddd7c40667851",
        "generation": "eaaf891e89935b909f13bece3ff1e8c4a1ae43d0e2378b834e07ca74e2607536",
        "entry_count": 2,
        "total_bytes": 7034391160,
        "license_id": "NVIDIA-Open-Model-License-2024-06",
        "source_uri": "hf://nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1",
        "source_revision": "ffed199e32612b98ffa04f4640d34d37b137fca5",
    },
    "complexa-ligand": {
        "marker_sha256": "868551c2ec89d8296155ac5e5e0a674205cbc851c301ed58f32014d7fe404177",
        "generation": "61247c8dbf261307d708be53decfda69f21e73ff421556662366045c30d9cea5",
        "entry_count": 2,
        "total_bytes": 5890739041,
        "license_id": "NVIDIA-Open-Model-License-2024-06",
        "source_uri": "hf://nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1",
        "source_revision": "bc90c8b2c701ceb52d5faef72600b6b5be880244",
    },
    "complexa-ame": {
        "marker_sha256": "18199cf0468000baefa7ed0cb604a2a7c64d0228ddf8d8e5579d6c1e543267db",
        "generation": "d38c622eaa0dad419f0ff0af72f36ab49299c533f5f56bbf08fa180e829afa5a",
        "entry_count": 2,
        "total_bytes": 5892211805,
        "license_id": "NVIDIA-Open-Model-License-2024-06",
        "source_uri": "hf://nvidia/NV-Proteina-Complexa-AME-160M-v1",
        "source_revision": "9743d749a8754080a32fda857d95579dfa4dabae",
    },
    "rosettafold3-checkpoint": {
        "marker_sha256": "94002c4a6a8c7fdbdda20e658785cf63a7708728afccf9615f1d54ea71657822",
        "generation": "d909fe65e86670b0a18a7494dd06811d301d0899e30778442e8ca6a343164bce",
        "entry_count": 1,
        "total_bytes": 3038876446,
        "license_id": "BSD-3-Clause",
        "source_uri": "https://files.ipd.uw.edu/pub/rf3/",
        "source_revision": "foundry-production-b02eed6a-checksum-lock",
    },
}

# Target structures are a real runtime dependency, not incidental image content:
# a run that loads both checkpoints and then cannot find its target has failed
# on a missing artifact. They ship inside the pinned upstream source archive, so
# their public identity is that archive at its pinned revision, and each file is
# pinned by its own digest and verified before the model is loaded.
TARGET_DATA = {
    "source_uri": "https://github.com/NVIDIA-BioNeMo/Proteina-Complexa",
    "source_revision": SOURCE_REVISION,
    "archive_sha256": "4a9448653fe9ae4e9e46c3204ef0e3c6ac9563a4cc5626c7a11d8441c485fb3b",
    "license_id": "Apache-2.0",
    "binding": "baked into the image at /opt/fs2/source/assets and reached through an "
    "image-baked read-only working directory; never a writable .env and never a "
    "symlink created at run time",
    "files": {
        "assets/target_data/bindcraft_targets/PD-L1.pdb": {
            "bytes": 74614,
            "sha256": "5e949950d8255ec3bf390b8bf24f5c71c6ac8fc65bc553d7e477563c9599b93e",
        },
        "assets/target_data/ligand_targets/7v11_ligand_centered.pdb": {
            "bytes": 4045,
            "sha256": "ffa26a9b1ed40e7a0cdc4a9f74ce105b965f65d095b66d60b30c8e1cf79fb391",
        },
        "assets/target_data/ame_input_structures/M0024_1nzy.pdb": {
            "bytes": 8586,
            "sha256": "70cff8a74c8fa5628a94a93828e98e9b16651c83927feb038b834e46696372eb",
        },
    },
}

# The image bakes this directory with a read-only ``assets`` symlink to the
# source asset root, so upstream's relative ./assets/... target paths resolve
# from a working directory that is part of the image digest.
BAKED_WORKDIR = Path("/opt/fs2/complexa/workdir")

STANDARD_RESIDUES = frozenset(
    """ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP
    TYR VAL""".split()
)

# Consecutive C-alpha atoms in a real protein backbone sit ~3.8 A apart.  A
# generous envelope still rejects collapsed or exploded coordinate fields.
CA_MIN_A = 2.5
CA_MAX_A = 4.6

# A designed chain of this length or more must show real sequence variety.
MIN_CHAIN_FOR_DIVERSITY = 20
MIN_DISTINCT_RESIDUES = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(message: str) -> "RuntimeFailure":
    return RuntimeFailure(message)


class RuntimeFailure(Exception):
    """A condition that must terminate the run without a PASS."""


def _artifact_root() -> Path:
    return Path(os.environ.get("FS2_ARTIFACT_ROOT", "/opt/fs2/artifacts"))


_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]*")


def _safe_relative(value: str) -> bool:
    """The plane's own rule for an inventoried entry path.

    No absolute path, no traversal and no leading dot at any depth, which is
    what keeps the reserved marker name outside the inventory and therefore
    unforgeable.
    """
    if not value or value.startswith("/") or len(value) > 1024:
        return False
    return all(_SAFE_SEGMENT.fullmatch(segment) for segment in value.split("/"))


def _crc32(path: Path) -> int:
    value = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value = zlib.crc32(chunk, value)
    return value & 0xFFFFFFFF


def tree_inventory_v2(root: Path) -> tuple[str, int, int, int]:
    """Recompute ``fs2-tree-inventory/v2`` over a mounted generation.

    This is the check that cannot be forged by editing a document: the digest is
    taken from the bytes actually mounted, and a generation directory is named
    by that digest, so a mount either is the generation it claims or is not.
    Returns (digest, file count, total bytes, directory count).
    """
    rows: list[dict[str, Any]] = []
    files = total = directories = 0
    for base, directory_names, file_names in os.walk(root):
        for name in sorted(directory_names):
            relative = os.path.relpath(os.path.join(base, name), root)
            if not _safe_relative(relative):
                continue
            rows.append({"kind": "directory", "path": relative})
            directories += 1
        for name in sorted(file_names):
            relative = os.path.relpath(os.path.join(base, name), root)
            if relative == MARKER_NAME or not _safe_relative(relative):
                continue
            entry = Path(base, name)
            size = entry.stat().st_size
            rows.append(
                {
                    "bytes": size,
                    "crc32": f"{_crc32(entry):08x}",
                    "kind": "file",
                    "path": relative,
                }
            )
            files += 1
            total += size
    rows.sort(key=lambda row: row["path"])
    payload = (json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), files, total, directories


def _marker_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()


def verify_generation(mount: Path, artifact_id: str) -> dict[str, Any]:
    """Prove a mount is exactly the pinned immutable generation.

    Mirrors the plane's own admission rules: the marker must describe this
    artifact, this generation, this sub-path, this plane and this licence
    visibility, its own document digest must match what the promotion receipt
    published, and the tree recomputed from the mounted bytes must reproduce the
    generation name. Bytes that are right in the wrong place, under the wrong
    licence, or measured by the wrong algorithm are still wrong.
    """
    pinned = GENERATIONS.get(artifact_id)
    if pinned is None:
        raise _fail(f"no generation is pinned for {artifact_id}")
    started = time.monotonic()

    marker_path = mount / MARKER_NAME
    if not marker_path.is_file():
        raise _fail(f"{artifact_id} mount carries no {MARKER_NAME}: {mount}")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))

    expected_sub_path = f"{GENERATION_ROOT}/{artifact_id}/sha256/{pinned['generation']}"
    checks: list[tuple[str, Any, Any]] = [
        ("schema", marker.get("schema"), MARKER_SCHEMA),
        ("artifact_id", marker.get("artifact_id"), artifact_id),
        ("generation", marker.get("generation"), pinned["generation"]),
        ("inventory_sha256", marker.get("inventory_sha256"), pinned["generation"]),
        ("sub_path", marker.get("sub_path"), expected_sub_path),
        ("read_only", marker.get("read_only"), True),
        ("inventory_algorithm", marker.get("inventory_algorithm"), INVENTORY_ALGORITHM),
        ("volume_kind", marker.get("volume_kind"), "host-path"),
        ("visibility", marker.get("visibility"), "public"),
        ("host_root", marker.get("host_root"), HOST_ROOT),
        ("namespace", marker.get("namespace"), ""),
        ("claim", marker.get("claim"), ""),
        ("entry_count", marker.get("entry_count"), pinned["entry_count"]),
        ("total_bytes", marker.get("total_bytes"), pinned["total_bytes"]),
        ("license_id", marker.get("license_id"), pinned["license_id"]),
        ("source_uri", marker.get("source_uri"), pinned["source_uri"]),
        ("source_revision", marker.get("source_revision"), pinned["source_revision"]),
    ]
    for field, observed, expected in checks:
        if observed != expected:
            raise _fail(
                f"{artifact_id} generation marker {field} is {observed!r}, expected {expected!r}"
            )

    observed_marker_digest = _marker_sha256(marker)
    if observed_marker_digest != pinned["marker_sha256"]:
        raise _fail(
            f"{artifact_id} generation marker digest is {observed_marker_digest}, "
            f"and {pinned['marker_sha256']} was published by the promotion receipt"
        )

    digest, files, total, directories = tree_inventory_v2(mount)
    if digest != pinned["generation"]:
        raise _fail(
            f"{artifact_id} mount recomputes to {digest}, "
            f"which is not the pinned generation {pinned['generation']}"
        )
    if files != pinned["entry_count"] or total != pinned["total_bytes"]:
        raise _fail(
            f"{artifact_id} mount holds {files} files and {total} bytes, expected "
            f"{pinned['entry_count']} and {pinned['total_bytes']}"
        )

    return {
        "artifact_id": artifact_id,
        "mount": str(mount),
        "generation": pinned["generation"],
        "sub_path": expected_sub_path,
        "host_root": HOST_ROOT,
        "volume_kind": "host-path",
        "visibility": "public",
        "license_id": pinned["license_id"],
        "source_uri": pinned["source_uri"],
        "source_revision": pinned["source_revision"],
        "marker_sha256": observed_marker_digest,
        "inventory_algorithm": INVENTORY_ALGORITHM,
        "recomputed_generation": digest,
        "entry_count": files,
        "total_bytes": total,
        "directory_count": directories,
        "seconds": round(time.monotonic() - started, 3),
    }


def verify_target_structure(relative: str) -> dict[str, Any]:
    """Verify one bundled target structure against its pinned identity."""
    pinned = TARGET_DATA["files"].get(relative)
    if pinned is None:
        raise _fail(f"target structure {relative} carries no pinned identity")
    path = SOURCE_ROOT / relative
    record = verify_file(path, pinned, f"target structure {relative}", digests=True)
    record.update(
        {
            "relative_path": relative,
            "source_uri": TARGET_DATA["source_uri"],
            "source_revision": TARGET_DATA["source_revision"],
            "archive_sha256": TARGET_DATA["archive_sha256"],
            "license_id": TARGET_DATA["license_id"],
            "binding": TARGET_DATA["binding"],
        }
    )
    return record


def verify_file(path: Path, expected: dict[str, Any], label: str, *, digests: bool) -> dict[str, Any]:
    """Verify one artifact marker: presence, exact size, optionally content."""
    record: dict[str, Any] = {
        "label": label,
        "path": str(path),
        "expected_bytes": expected["bytes"],
        "expected_sha256": expected["sha256"],
        "digest_verified": False,
    }
    if not path.is_file():
        raise _fail(f"{label} is absent: {path}")
    size = path.stat().st_size
    record["observed_bytes"] = size
    if size != expected["bytes"]:
        raise _fail(f"{label} is {size} bytes, expected {expected['bytes']}: {path}")
    if digests:
        started = time.monotonic()
        observed = _sha256(path)
        record["observed_sha256"] = observed
        record["digest_seconds"] = round(time.monotonic() - started, 3)
        if observed != expected["sha256"]:
            raise _fail(f"{label} is sha256:{observed}, expected sha256:{expected['sha256']}")
        record["digest_verified"] = True
    return record


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # provided by the runtime venv

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise _fail(f"expected a YAML mapping: {path}")
    return value


def resolve_target(variant: dict[str, Any], task_name: str) -> dict[str, Any]:
    """Resolve a task to an absolute, existing target structure.

    Upstream stores ``target_path`` relative to the repository root.  The value
    is resolved against the in-image source root so the contract does not
    depend on the working directory.
    """
    dictionary = SOURCE_ROOT / variant["target_dictionary"]
    entries = _load_yaml(dictionary).get(variant["target_namespace"]) or {}
    if task_name not in entries:
        available = ", ".join(sorted(entries)[:8])
        raise _fail(
            f"task {task_name!r} is not in {variant['target_namespace']} "
            f"({dictionary}); available include: {available}"
        )
    entry = dict(entries[task_name])
    relative = entry.get("target_path")
    if not relative:
        raise _fail(
            f"task {task_name!r} declares no target_path; this contract only "
            "admits tasks whose structure ships with the image"
        )
    resolved = Path(relative)
    if not resolved.is_absolute():
        resolved = (SOURCE_ROOT / str(relative).lstrip("./")).resolve()
    if not resolved.is_file():
        raise _fail(f"target structure for {task_name!r} is absent: {resolved}")
    ligand = entry.get("ligand")
    ligands = [ligand] if isinstance(ligand, str) else list(ligand or [])
    return {
        "task_name": task_name,
        "target_path": str(resolved),
        "declared_target_path": relative,
        "binder_length": entry.get("binder_length"),
        "ligand_residues": [str(item) for item in ligands if item],
        "contig_atoms": entry.get("contig_atoms"),
    }


def verify_working_directory(work_directory: Path) -> dict[str, Any]:
    """Confirm the image-baked working directory resolves upstream assets.

    Upstream target dictionaries declare ./assets/target_data/..., resolved
    against the process working directory, and Hydra cannot express an absolute
    override for them because most task names begin with a digit while its key
    grammar admits only a leading letter or underscore. The image therefore
    bakes a working directory holding a read-only assets link to the source
    asset root, so the binding is part of the image digest. Nothing is created
    here: a missing or redirected link fails the run rather than being repaired,
    because repairing it at run time is the mutable side effect this contract
    exists to avoid.
    """
    link = work_directory / "assets"
    expected = SOURCE_ROOT / "assets"
    if not work_directory.is_dir():
        raise _fail(f"the image-baked working directory is missing: {work_directory}")
    if not link.is_symlink():
        raise _fail(f"{link} is not the image-baked asset link")
    resolved = link.resolve()
    if resolved != expected.resolve():
        raise _fail(f"{link} resolves to {resolved}, expected {expected}")
    if not (link / "target_data").is_dir():
        raise _fail(f"{link}/target_data is not readable")
    return {
        "working_directory": str(work_directory),
        "asset_link": str(link),
        "resolves_to": str(resolved),
        "created_at_run_time": False,
    }


def cuda_preflight() -> dict[str, Any]:
    """Record the device identity this process can actually see."""
    import torch

    if not torch.cuda.is_available():
        raise _fail("CUDA is not available; Proteina-Complexa generation requires a GPU")
    index = torch.cuda.current_device()
    major, minor = torch.cuda.get_device_capability(index)
    properties = torch.cuda.get_device_properties(index)
    return {
        "available": True,
        "device_count": torch.cuda.device_count(),
        "device_name": torch.cuda.get_device_name(index),
        "compute_capability": f"{major}.{minor}",
        "total_memory_bytes": int(properties.total_memory),
        "torch_version": torch.__version__,
        "torch_cuda_version": torch.version.cuda,
        "driver_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        # Capability-driven, never a device-name allowlist: Hopper is the present
        # acceptance target but Blackwell and Ada report their own capabilities
        # and are admitted by the same check.
        "architecture_policy": "capability-driven; no device-name allowlist",
    }


def build_argv(
    variant_name: str,
    variant: dict[str, Any],
    request: dict[str, Any],
    target: dict[str, Any],
    artifact_dir: Path,
    output_root: Path,
) -> list[str]:
    task = target["task_name"]
    argv = [
        sys.executable,
        "-m",
        "proteinfoundation.generate",
        f"--config-path={SOURCE_ROOT / 'configs'}",
        f"--config-name={variant['pipeline']}",
        f"++ckpt_path={artifact_dir}",
        f"++ckpt_name={variant['checkpoint']['name']}",
        f"++autoencoder_ckpt_path={artifact_dir / variant['autoencoder']['name']}",
        f"++generation.task_name={task}",
        # No target_path override: Hydra's override grammar rejects key
        # segments that begin with a digit, and most upstream task names do.
        # The run directory's ``assets`` symlink is what makes upstream's own
        # relative target_path resolve. See verify_working_directory().
        f"++generation.dataloader.batch_size={request['batch_size']}",
        f"++generation.dataloader.dataset.nres.nsamples={request['samples']}",
        f"++generation.args.nsteps={request['nsteps']}",
        f"++seed={request['seed']}",
        f"++root_path={output_root}",
        "++save_timing=true",
        # Hydra must not relocate the process: every path this contract passes
        # is absolute, and a chdir would only make upstream's relative defaults
        # resolve somewhere unpredictable.
        "hydra.job.chdir=False",
        f"hydra.run.dir={output_root / 'hydra'}",
    ]
    if request["reward_model"] == "none":
        # Isolate the Complexa forward model: the composite reward models pull
        # AlphaFold2 (protein) or RosettaFold3 (ligand, AME) into the sampling
        # loop, and a reward failure would be indistinguishable from a Complexa
        # failure.
        # Only an assignment, never a delete: the AME pipeline already ships
        # reward_model: null, and "~generation.reward_model" then aborts
        # composition with "Could not delete from config". Assignment composes
        # cleanly whether the node is a dict or already null.
        argv += [
            "++generation.search.algorithm=single-pass",
            "++generation.reward_model=null",
        ]
    else:
        argv += [f"++generation.search.algorithm={request['search_algorithm']}"]
    return argv


# Upstream emits loguru lines of the form
# "2026-09-03 04:36:52.223 | INFO | __main__:validate_checkpoint_paths:117 - ..."
_LOG_LINE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| \w+\s+\| [^-]+- (?P<message>.*)$"
)


def upstream_generation_seconds(output_root: Path) -> float | None:
    """Read upstream's own generation total from the timing CSV it writes.

    This is preferred over the log: upstream emits "Total generation time"
    through a Rich handler that wraps the line and right-aligns the source
    location, whereas the CSV is machine-readable and written by the same code
    path that measures it.
    """
    for path in sorted(output_root.glob("timing_*.csv")):
        rows = [row for row in path.read_text(encoding="utf-8").splitlines() if row.strip()]
        if len(rows) < 2:
            continue
        header = [column.strip() for column in rows[0].split(",")]
        if "total_time" not in header:
            continue
        values = rows[1].split(",")
        try:
            return float(values[header.index("total_time")])
        except (ValueError, IndexError):
            continue
    return None


# Upstream emits loguru lines of the form
# "2026-09-03 04:36:52.223 | INFO | __main__:validate_checkpoint_paths:117 - ..."
_LOG_LINE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| \w+\s+\| [^-]+- (?P<message>.*)$"
)

# The anchors below are the plain-format lines upstream always emits, in the
# order it emits them. Note that "Starting generation job at" is logged *before*
# the checkpoint is loaded, so upstream's own total spans model load plus
# sampling -- it is not the sampling time on its own.
_ANCHORS = (
    ("checkpoint_validated", "Checkpoint validated:"),
    ("generation_job_started", "Starting generation job at"),
    ("checkpoint_load_started", "Using checkpoint "),
    ("lora_reapplied", "Re-create LoRA layers"),
    ("model_ready", "cfg_gen:"),
)


def parse_phases(
    log_text: str, started_at: float, generation_seconds: float | None = None
) -> dict[str, Any]:
    """Split the upstream run into phases using its own timestamps.

    ``generation_seconds`` is upstream's measured generation total. Sampling is
    derived from it by subtracting the observed model-load span, rather than
    timing sampling directly, because upstream logs no plain-format marker at
    the moment sampling begins.
    """
    marks: dict[str, float] = {}
    for raw in log_text.splitlines():
        match = _LOG_LINE.match(raw.strip())
        if not match:
            continue
        moment = datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S.%f")
        epoch = moment.replace(tzinfo=timezone.utc).timestamp()
        message = match.group("message")
        for key, needle in _ANCHORS:
            if message.startswith(needle) and key not in marks:
                marks[key] = epoch

    def span(first: str, second: str) -> float | None:
        if first in marks and second in marks:
            return round(marks[second] - marks[first], 3)
        return None

    model_load = span("checkpoint_load_started", "model_ready")
    sampling = None
    if generation_seconds is not None and model_load is not None:
        sampling = round(generation_seconds - model_load, 3)

    phases: dict[str, Any] = {
        "interpreter_and_import_seconds": (
            round(marks["checkpoint_validated"] - started_at, 3)
            if "checkpoint_validated" in marks
            else None
        ),
        "model_load_seconds": model_load,
        "sampling_seconds": sampling,
        # The canonical active-compute phase. Sampling, not the whole job:
        # upstream's total also covers loading two multi-gigabyte checkpoints.
        "compute_seconds": sampling,
        "upstream_reported_generation_seconds": generation_seconds,
        "lora_reapplied": "lora_reapplied" in marks,
    }
    if "lora_reapplied" in marks:
        phases["checkpoint_load_to_lora_seconds"] = span(
            "checkpoint_load_started", "lora_reapplied"
        )
    return phases


def _atoms(pdb_text: str) -> list[str]:
    return [
        line
        for line in pdb_text.splitlines()
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 54
    ]


def _coord(line: str) -> tuple[float, float, float]:
    return (float(line[30:38]), float(line[38:46]), float(line[46:54]))


def inspect_structure(path: Path) -> dict[str, Any]:
    """Parse one PDB file and summarise what it actually contains."""
    text = path.read_text(encoding="utf-8", errors="strict")
    atoms = _atoms(text)
    if not atoms:
        raise _fail(f"structure has no parseable ATOM/HETATM records: {path}")
    residues: dict[tuple[str, str], str] = {}
    chains: set[str] = set()
    ca_by_chain: dict[str, list[tuple[float, float, float]]] = {}
    residue_names: set[str] = set()
    for line in atoms:
        name = line[17:20].strip()
        chain = line[21:22]
        key = (chain, line[22:27])
        residues[key] = name
        residue_names.add(name)
        chains.add(chain)
        coord = _coord(line)
        if not all(math.isfinite(value) for value in coord):
            raise _fail(f"structure contains a non-finite coordinate: {path}")
        if line[12:16].strip() == "CA":
            ca_by_chain.setdefault(chain, []).append(coord)

    standard = {key: name for key, name in residues.items() if name in STANDARD_RESIDUES}
    if not standard:
        raise _fail(f"structure contains no standard amino-acid residue: {path}")

    # Per chain, because the binder pipelines write the target and the designed
    # binder into one file: a whole-file residue count is the sum of both and
    # says nothing about the binder's length.
    per_chain: dict[str, dict[str, int]] = {}
    kinds: dict[str, set[str]] = {}
    for (chain, _number), name in residues.items():
        bucket = per_chain.setdefault(chain, {"residues": 0, "standard": 0})
        bucket["residues"] += 1
        if name in STANDARD_RESIDUES:
            bucket["standard"] += 1
            kinds.setdefault(chain, set()).add(name)
    for chain, bucket in per_chain.items():
        bucket["distinct_standard"] = len(kinds.get(chain, ()))

    spans: dict[str, Any] = {}
    for chain, trace in ca_by_chain.items():
        if len(trace) < 2:
            continue
        steps = [
            math.dist(trace[index], trace[index + 1]) for index in range(len(trace) - 1)
        ]
        extent = max(
            math.dist(trace[i], trace[j])
            for i in range(0, len(trace), max(1, len(trace) // 12))
            for j in range(i + 1, len(trace), max(1, len(trace) // 12))
        )
        spans[chain] = {
            "ca_count": len(trace),
            "min_step_a": round(min(steps), 3),
            "max_step_a": round(max(steps), 3),
            "mean_step_a": round(sum(steps) / len(steps), 3),
            "max_extent_a": round(extent, 3),
        }
        if extent < 1e-3:
            raise _fail(f"chain {chain} C-alpha trace is fully degenerate: {path}")

    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "atom_records": len(atoms),
        "residue_count": len(residues),
        "standard_residue_count": len(standard),
        "chains": sorted(chains),
        "chain_residues": per_chain,
        "residue_names": sorted(residue_names),
        "ca_traces": spans,
    }


def validate_backbone(structures: list[dict[str, Any]]) -> list[str]:
    """Flag C-alpha traces whose bond geometry is not protein-like."""
    notes: list[str] = []
    for structure in structures:
        for chain, span in structure["ca_traces"].items():
            if span["ca_count"] < 2:
                continue
            if not (CA_MIN_A <= span["mean_step_a"] <= CA_MAX_A):
                notes.append(
                    f"{Path(structure['path']).name} chain {chain} mean C-alpha step "
                    f"{span['mean_step_a']} A is outside {CA_MIN_A}-{CA_MAX_A} A"
                )
    return notes


def validate_sequence_diversity(structures: list[dict[str, Any]]) -> list[str]:
    """Reject a designed chain that collapsed onto one or two residue types.

    Complexa is a design model: a chain of plausible geometry but near-uniform
    composition (poly-alanine, poly-glycine) is a real failure mode that every
    geometry check passes.  Only substantial chains are judged, so a short
    peptide or a ligand-only chain is not penalised.
    """
    notes: list[str] = []
    for structure in structures:
        for chain, counts in structure["chain_residues"].items():
            if counts["standard"] < MIN_CHAIN_FOR_DIVERSITY:
                continue
            distinct = counts["distinct_standard"]
            if distinct < MIN_DISTINCT_RESIDUES:
                notes.append(
                    f"{Path(structure['path']).name} chain {chain} has "
                    f"{counts['standard']} residues but only {distinct} distinct "
                    f"amino-acid type(s), below the minimum of {MIN_DISTINCT_RESIDUES}"
                )
    return notes


def discover_structures(output_root: Path) -> list[Path]:
    """Find produced PDBs without ever following a symlink out of the tree."""
    found: list[Path] = []
    for base, directories, files in os.walk(output_root, followlinks=False):
        directories[:] = [
            name for name in directories if not Path(base, name).is_symlink()
        ]
        found.extend(
            Path(base, name) for name in files if name.endswith(".pdb")
        )
    return sorted(found)


def validate_outputs(
    variant_name: str,
    variant: dict[str, Any],
    request: dict[str, Any],
    target: dict[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    """Model-specific semantic validation of the produced artifacts."""
    pdbs = discover_structures(output_root)
    if not pdbs:
        raise _fail(f"generation produced no PDB structure under {output_root}")
    structures = [inspect_structure(path) for path in pdbs]

    binder_pdbs = [item for item in structures if "_binder" in Path(item["path"]).name]
    # The ligand and AME pipelines write both a binder-only structure and a
    # protein+ligand complex; the protein pipeline writes the binder only.
    complexes = [item for item in structures if item not in binder_pdbs]

    findings: list[str] = validate_backbone(structures)
    findings += validate_sequence_diversity(structures)

    ligand_hits: list[str] = []
    if variant["expects_ligand"]:
        wanted = {name.upper() for name in target["ligand_residues"]}
        if not wanted:
            raise _fail(f"variant {variant_name} expects a ligand but the target declares none")
        pool = complexes or structures
        for structure in pool:
            present = wanted.intersection(structure["residue_names"])
            if present:
                ligand_hits.extend(sorted(present))
        if not ligand_hits:
            raise _fail(
                f"no produced complex contains any expected ligand residue "
                f"{sorted(wanted)}; got residue names "
                f"{sorted({name for item in pool for name in item['residue_names']})}"
            )

    envelope = target.get("binder_length") or []
    if isinstance(envelope, (int, float)):
        envelope = [envelope]
    envelope = [int(value) for value in envelope if value is not None]
    length_pool = binder_pdbs or structures
    observed_lengths = [item["standard_residue_count"] for item in length_pool]
    chain_lengths = {
        Path(item["path"]).name: {
            chain: counts["standard"] for chain, counts in item["chain_residues"].items()
        }
        for item in length_pool
    }
    # A two-element envelope is a real upstream ``[min, max]`` binder-length
    # range and is enforced.  A single-element envelope is upstream's
    # ``UniformInt(low=N, high=null)`` shape, whose sampled length is not a
    # documented bound, so it is recorded rather than enforced.
    length_notes: list[str] = []
    if envelope:
        low = min(envelope)
        high = max(envelope) if len(envelope) > 1 else None
        for name, chains in chain_lengths.items():
            if not any(count > 0 for count in chains.values()):
                raise _fail(f"{name} has no standard residues in any chain")
            if high is not None:
                # The designed binder is whichever chain lands in the declared
                # envelope; the other chain is the supplied target.
                if not any(low <= count <= high for count in chains.values()):
                    findings.append(
                        f"{name} has no chain within the target binder envelope "
                        f"{low}-{high}; per-chain standard residues {chains}"
                    )
            elif not any(count == low for count in chains.values()):
                length_notes.append(
                    f"{name} has no chain of exactly the declared length {low}; "
                    f"per-chain standard residues {chains}"
                )

    rewards = sorted(output_root.glob("rewards_*.csv"))
    timing = sorted(output_root.glob("timing_*.csv"))
    if not timing:
        raise _fail(f"upstream wrote no timing CSV under {output_root}")

    reward_rows = 0
    if rewards:
        header, *rows = rewards[0].read_text(encoding="utf-8").splitlines()
        reward_rows = len([row for row in rows if row.strip()])
        if "pdb_path" not in header:
            raise _fail(f"rewards CSV has no pdb_path column: {rewards[0]}")

    if variant_name == "protein" and not envelope:
        raise _fail("the protein target declares no binder length envelope")

    return {
        "structure_count": len(structures),
        "binder_structure_count": len(binder_pdbs),
        "complex_structure_count": len(complexes),
        "structures": structures,
        "expected_ligand_residues": target["ligand_residues"],
        "observed_ligand_residues": sorted(set(ligand_hits)),
        "binder_lengths": observed_lengths,
        "chain_lengths": chain_lengths,
        "binder_length_envelope": envelope,
        "rewards_csv": [str(path) for path in rewards],
        "reward_rows": reward_rows,
        "timing_csv": [str(path) for path in timing],
        "geometry_findings": findings,
        "length_notes": length_notes,
    }


def normalise_request(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("schema") not in (None, SCHEMA_REQUEST):
        raise _fail(f"unsupported request schema: {raw.get('schema')}")
    variant_name = raw.get("variant")
    if variant_name not in VARIANTS:
        raise _fail(f"variant must be one of {sorted(VARIANTS)}; got {variant_name!r}")
    variant = VARIANTS[variant_name]
    reward = raw.get("reward_model", "none")
    if reward not in ("none", "upstream-default"):
        raise _fail("reward_model must be 'none' or 'upstream-default'")
    algorithm = raw.get("search_algorithm", "single-pass")
    if reward == "none" and algorithm != "single-pass":
        raise _fail("search_algorithm must be 'single-pass' when reward_model is 'none'")

    def positive(key: str, default: int) -> int:
        value = int(raw.get(key, default))
        if value < 1:
            raise _fail(f"{key} must be >= 1; got {value}")
        return value

    return {
        "run_id": str(raw.get("run_id") or f"complexa-{variant_name}-{int(time.time())}"),
        "variant": variant_name,
        "task_name": str(raw.get("task_name") or variant["default_task"]),
        "samples": positive("samples", 1),
        "batch_size": positive("batch_size", 1),
        "nsteps": positive("nsteps", 400),
        "seed": int(raw.get("seed", 20260903)),
        "reward_model": reward,
        "search_algorithm": "single-pass" if reward == "none" else algorithm,
        "verify_content_digests": bool(raw.get("verify_content_digests", False)),
    }


def describe() -> dict[str, Any]:
    return {
        "schema": "fs2-serve.nebius.ai/proteina-complexa-runtime-descriptor/v1",
        "backend_id": BACKEND_ID,
        "model_id": MODEL_ID,
        "source_revision": SOURCE_REVISION,
        "request_schema": SCHEMA_REQUEST,
        "result_schema": SCHEMA_RESULT,
        "variants": {
            name: {
                "artifact_id": spec["artifact_id"],
                "model_id": spec["model_id"],
                "checkpoint": spec["checkpoint"]["name"],
                "autoencoder": spec["autoencoder"]["name"],
                "pipeline": spec["pipeline"],
                "lora": spec["lora"],
                "environment": spec["environment"],
                "default_task": spec["default_task"],
                "expects_ligand": spec["expects_ligand"],
                "expects_motif": spec["expects_motif"],
            }
            for name, spec in VARIANTS.items()
        },
        "shared_artifacts": [RF3_ARTIFACT],
        "generations": {
            artifact_id: {
                "generation": pinned["generation"],
                "marker_sha256": pinned["marker_sha256"],
                "sub_path": f"{GENERATION_ROOT}/{artifact_id}/sha256/{pinned['generation']}",
                "host_root": HOST_ROOT,
                "volume_kind": "host-path",
                "visibility": "public",
                "license_id": pinned["license_id"],
                "entry_count": pinned["entry_count"],
                "total_bytes": pinned["total_bytes"],
            }
            for artifact_id, pinned in GENERATIONS.items()
        },
        "target_data": TARGET_DATA,
        "working_directory": str(BAKED_WORKDIR),
        "reward_models": {
            "protein": "AlphaFold2 through ColabDesign, bound by AF2_DIR",
            "ligand": "RosettaFold3, bound by RF3_CKPT_PATH and RF3_EXEC_PATH",
            "ame": "RosettaFold3, bound by RF3_CKPT_PATH and RF3_EXEC_PATH",
        },
        "gpu_snapshot": {
            "captured": False,
            "restored": False,
            "reason": "no device snapshot mechanism is used by this runtime",
        },
    }


def run(arguments: argparse.Namespace) -> int:
    wall_started = time.time()
    started_monotonic = time.monotonic()
    request = normalise_request(json.loads(Path(arguments.request).read_text(encoding="utf-8")))
    variant_name = request["variant"]
    variant = VARIANTS[variant_name]

    output_root = Path(arguments.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    upstream_log = output_root / "upstream.log"
    result_path = output_root / "result.json"

    result: dict[str, Any] = {
        "schema": SCHEMA_RESULT,
        "backend_id": BACKEND_ID,
        "model_id": MODEL_ID,
        "source_revision": SOURCE_REVISION,
        "run_id": request["run_id"],
        "variant": variant_name,
        "request": request,
        "started_at": _now(),
        "terminal_state": "FAILED",
        "gpu_snapshot": describe()["gpu_snapshot"],
        "cache_level": arguments.cache_level,
    }

    try:
        artifact_dir = Path(arguments.artifact_dir or (_artifact_root() / variant["artifact_id"]))
        phase_started = time.monotonic()

        # The mount must be the pinned immutable generation before anything else
        # happens: marker, plane, licence, document digest, and a tree digest
        # recomputed from the mounted bytes.
        generation = verify_generation(artifact_dir, variant["artifact_id"])

        markers = [
            verify_file(
                artifact_dir / variant["checkpoint"]["name"],
                variant["checkpoint"],
                f"{variant_name} score checkpoint",
                digests=request["verify_content_digests"],
            ),
            verify_file(
                artifact_dir / variant["autoencoder"]["name"],
                variant["autoencoder"],
                f"{variant_name} partial autoencoder",
                digests=request["verify_content_digests"],
            ),
        ]

        rf3_root = Path(
            os.environ.get("FS2_RF3_ROOT", str(_artifact_root() / "rosettafold3-checkpoint"))
        )
        rf3_path = rf3_root / RF3_ARTIFACT["name"]
        rf3_required = request["reward_model"] == "upstream-default" and variant_name in (
            "ligand",
            "ame",
        )
        if rf3_root.is_dir() and (rf3_root / MARKER_NAME).is_file():
            rf3_record: dict[str, Any] = verify_generation(rf3_root, "rosettafold3-checkpoint")
            rf3_record.update(
                verify_file(
                    rf3_path,
                    RF3_ARTIFACT,
                    "rosettafold3 checkpoint",
                    digests=request["verify_content_digests"],
                )
            )
            rf3_record["bound"] = True
            rf3_record["exercised"] = rf3_required
            rf3_record["role"] = RF3_ARTIFACT["role"]
        else:
            rf3_record = {
                "label": "rosettafold3 checkpoint",
                "path": str(rf3_path),
                "bound": False,
                "exercised": False,
                "role": RF3_ARTIFACT["role"],
                "note": "no RosettaFold3 generation is mounted for this run",
            }
            if rf3_required:
                raise _fail(
                    "this request asks for the upstream default reward model, which for "
                    f"the {variant_name} pipeline is RosettaFold3, but no RosettaFold3 "
                    f"generation is mounted at {rf3_root}"
                )

        result["artifact_verification"] = {
            "artifact_dir": str(artifact_dir),
            "generation": generation,
            "markers": markers,
            "rosettafold3": rf3_record,
            "seconds": round(time.monotonic() - phase_started, 3),
            "content_digests_verified": request["verify_content_digests"],
        }

        result["cuda"] = cuda_preflight()
        target = resolve_target(variant, request["task_name"])
        relative_target = os.path.relpath(target["target_path"], SOURCE_ROOT)
        result["target"] = {**target, "identity": verify_target_structure(relative_target)}
        work_root = Path(arguments.work_dir)
        result["working_directory"] = verify_working_directory(work_root)

        environment = dict(os.environ)
        environment.update(variant["environment"])
        environment.setdefault("DATA_PATH", str(SOURCE_ROOT / "assets"))
        environment.setdefault("RF3_EXEC_PATH", "/opt/venv/bin/rf3")
        if rf3_record.get("bound"):
            environment["RF3_CKPT_PATH"] = str(rf3_path)

        argv = build_argv(
            variant_name, variant, request, target, artifact_dir, output_root
        )
        result["argv"] = argv
        result["environment_overrides"] = {
            key: environment[key]
            for key in sorted(set(variant["environment"]) | {"DATA_PATH", "RF3_EXEC_PATH", "RF3_CKPT_PATH"})
            if key in environment
        }

        upstream_started_wall = time.time()
        upstream_started = time.monotonic()
        with upstream_log.open("wb") as sink:
            process = subprocess.Popen(
                argv,
                cwd=str(work_root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None
            for chunk in iter(lambda: process.stdout.read(65536), b""):
                sink.write(chunk)
                sink.flush()
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            exit_code = process.wait()
        upstream_seconds = round(time.monotonic() - upstream_started, 3)

        log_text = upstream_log.read_text(encoding="utf-8", errors="replace")
        phases = parse_phases(
            log_text, upstream_started_wall, upstream_generation_seconds(output_root)
        )
        phases["upstream_process_seconds"] = upstream_seconds
        result["phases"] = phases
        result["upstream_exit_code"] = exit_code
        result["cuda_used_by_upstream"] = "GPU available: True (cuda), used: True" in log_text

        # Terminal PASS is only reachable after upstream exits zero.
        if exit_code != 0:
            raise _fail(f"upstream generation exited {exit_code}")
        if not result["cuda_used_by_upstream"]:
            raise _fail("upstream did not report CUDA use; refusing to call this a GPU run")
        if variant["lora"] and not phases.get("lora_reapplied"):
            raise _fail(
                f"variant {variant_name} ships LoRA adapters but upstream never "
                "re-created and reloaded them"
            )
        if not variant["lora"] and phases.get("lora_reapplied"):
            raise _fail(
                f"variant {variant_name} must not carry LoRA adapters but upstream re-applied them"
            )

        result["validation"] = validate_outputs(
            variant_name, variant, request, target, output_root
        )
        if result["validation"]["geometry_findings"]:
            raise _fail(
                "semantic validation findings: "
                + "; ".join(result["validation"]["geometry_findings"])
            )

        result["terminal_state"] = "PASS"
        return 0
    except RuntimeFailure as failure:
        result["failure"] = str(failure)
        return 1
    except Exception as unexpected:  # noqa: BLE001 - recorded, then re-raised as failure
        result["failure"] = f"{type(unexpected).__name__}: {unexpected}"
        return 1
    finally:
        result["finished_at"] = _now()
        result["wall_seconds"] = round(time.time() - wall_started, 3)
        result["entrypoint_seconds"] = round(time.monotonic() - started_monotonic, 3)
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"[complexa-batch] {result['variant']} {result['terminal_state']} "
            f"-> {result_path}",
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("describe", help="print the runtime descriptor and exit")

    runner = sub.add_parser("run", help="execute one Complexa generation request")
    runner.add_argument("--request", required=True, help="path to the request JSON")
    runner.add_argument("--output-root", required=True, help="directory for artifacts")
    runner.add_argument(
        "--artifact-dir",
        default=None,
        help="override the checkpoint-pair directory (defaults to FS2_ARTIFACT_ROOT/<artifact id>)",
    )
    runner.add_argument(
        "--work-dir",
        default=str(BAKED_WORKDIR),
        help="the image-baked working directory that resolves relative asset "
        "paths; it is read-only and outside the output root",
    )
    runner.add_argument(
        "--cache-level",
        default="unknown",
        choices=["cold", "image-local", "artifact-local", "warm", "unknown"],
        help="truthful cache level of this run, supplied by the caller",
    )

    arguments = parser.parse_args(argv)
    if arguments.command == "describe":
        print(json.dumps(describe(), indent=2, sort_keys=True))
        return 0
    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())
