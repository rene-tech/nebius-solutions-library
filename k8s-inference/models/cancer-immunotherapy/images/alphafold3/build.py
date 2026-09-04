#!/usr/bin/env python3
"""Build, attest, inspect, lock and publish the fs2 AlphaFold 3 runtime image.

The build is deliberately split into separate verbs. Building produces a local
OCI archive with SBOM and provenance attestations and nothing else; proving the
image carries no licensed parameters and no reference-database bytes is its own
verb; and publishing to the project registry is a separate, reviewed action.
A local tag is never a registry digest and a build receipt is never a
deployment.

Verbs
-----
check     Validate the committed contracts, schemas and Dockerfile invariants.
build     Build linux/amd64 with SBOM and SLSA provenance into an OCI archive.
inspect   Walk every layer of an OCI archive and prove no payload is embedded.
smoke     Load the archive into the local daemon and run the entrypoint probes.
lock      Assemble the committed image lock from the build and inspect receipts.
publish   Push the built image to the project registry without overwriting.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
CONTRACTS = ROOT / "contracts"
SCHEMAS = ROOT / "schemas"

RUNTIME_ID = "alphafold3"
UPSTREAM_COMMIT = "85c4d20505fd5cef05eac22b534d4e793971ae69"
UPSTREAM_TREE = "efa1a376c9cf94d517d70e68425bc1ed3b17a570"
UPSTREAM_VERSION = "3.0.4"
BASE_IMAGE_DIGEST = "sha256:c87e78933f4c16e3272123bf2f75537306596d0fbaa395a29696a22786e5ee0e"
DEFAULT_TAG = f"fs2-cancer/{RUNTIME_ID}:{UPSTREAM_VERSION}-{UPSTREAM_COMMIT[:8]}"

# Pinned by digest so the SBOM scanner itself is reproducible, matching the
# convention already used by the attested platform builds in this repository.
SBOM_GENERATOR = (
    "docker.io/docker/buildkit-syft-scanner"
    "@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9"
)
EXPECTED_ATTESTATIONS = {"https://spdx.dev/Document", "https://slsa.dev/provenance/v1"}
SUPPORTED_STATEMENT_TYPES = {
    "https://in-toto.io/Statement/v0.1",
    "https://in-toto.io/Statement/v1",
}

IMAGE_LOCK_SCHEMA = "fs2-serve.nebius.ai/alphafold3-image-lock/v1"
COMMAND_IO_SCHEMA = "fs2-serve.nebius.ai/alphafold3-command-io/v1"
COMMAND_IO_PATH = CONTRACTS / "af3-command-io-contract.json"

# Sentinels substituted for placeholders so the published argv templates are
# generated from the implementation rather than transcribed by hand.
SENTINELS = {
    "/sentinel/json-path": "{json_path}",
    "/sentinel/output-dir": "{output_dir}",
    "/sentinel/database-root": "{database_root}",
    "/sentinel/model-dir": "{model_dir}",
    "/sentinel/cache/jax": "{jax_compilation_cache_dir}",
    "=127": "={msa_threads}",
}

# Anything matching these must never appear in an image layer.
FORBIDDEN_NAME_RE = re.compile(r"(^|/)(af3\.bin|.*\.bin\.zst$)")
REFERENCE_DB_NAMES = frozenset(
    {
        "bfd-first_non_consensus_sequences.fasta",
        "mgy_clusters_2022_05.fa",
        "uniprot_all_2021_04.fa",
        "uniref90_2022_05.fa",
        "nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta",
        "rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta",
        "rnacentral_active_seq_id_90_cov_80_linclust.fasta",
        "pdb_seqres_2022_09_28.fasta",
    }
)
REFERENCE_DB_DIRS = frozenset({"mmcif_files"})

# The parameter object is roughly 1.0 GB. No single image entry should come
# close, so a very large entry is treated as a payload smell worth reporting.
LARGE_ENTRY_BYTES = 400 * 1024 * 1024


class BuildError(RuntimeError):
    """A build, verification or publication requirement was not met."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], *, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=check,
        capture_output=capture,
        text=True,
    )


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise BuildError(f"required tool {name!r} is not installed")


def emit(document: dict[str, Any], destination: Path | None = None) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(payload)
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BuildError(f"missing required file {path}") from error
    except json.JSONDecodeError as error:
        raise BuildError(f"{path} is not valid JSON: {error}") from error


# The exact files the build context admits, per .dockerignore. BuildKit's
# resolvedDependencies records base images but not the local context, so the
# context is digested here and bound into the lock.
CONTEXT_FILES = (
    "Dockerfile",
    "runtime/af3_runtime.py",
    "contracts/af3-runtime-source-lock.json",
    "contracts/af3-parameter-binding.json",
)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def source_state() -> dict[str, Any]:
    """The committed identity of the build context, and whether it is clean.

    An image's provenance is only useful if the revision it records is the
    revision a reviewer can check out. Building from a dirty tree, or from a
    tree whose HEAD has since been amended, produces an attestation that points
    at source which no longer matches the image.
    """
    commit = _git("rev-parse", "HEAD")
    tree = _git("rev-parse", "HEAD^{tree}")
    dirty = _git("status", "--porcelain", "--", *CONTEXT_FILES)
    untracked = [
        name
        for name in CONTEXT_FILES
        if not (ROOT / name).exists()
    ]
    return {
        "commit": commit,
        "tree": tree,
        "dirty_context_paths": [line[3:] for line in dirty.splitlines() if line],
        "missing_context_paths": untracked,
    }


def context_digest() -> dict[str, Any]:
    """A digest binding the exact local build context into the receipt."""
    entries = []
    for name in CONTEXT_FILES:
        path = ROOT / name
        if not path.is_file():
            raise BuildError(f"build context file {name} is missing")
        entries.append({"path": name, "sha256": sha256_of(path)})
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "files": entries,
        "context_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def attestation_vcs_revision(oci_file: Path) -> str | None:
    """The source revision BuildKit recorded in the SLSA provenance."""
    with tarfile.open(oci_file) as archive:
        index = json.loads(_oci_blob_index(archive))
        top = json.loads(_oci_blob(archive, index["manifests"][0]["digest"]))
        attestations = [
            entry
            for entry in top.get("manifests", [])
            if (entry.get("annotations") or {}).get("vnd.docker.reference.type")
            == "attestation-manifest"
        ]
        if not attestations:
            return None
        manifest = json.loads(_oci_blob(archive, attestations[0]["digest"]))
        for layer in manifest.get("layers", []):
            statement = json.loads(_oci_blob(archive, layer["digest"]))
            if "slsa.dev/provenance" not in str(statement.get("predicateType", "")):
                continue
            found = re.findall(
                r'"vcs:revision"\s*:\s*"([0-9a-f]{40})"', json.dumps(statement)
            )
            if found:
                unique = set(found)
                if len(unique) != 1:
                    raise BuildError(
                        f"the attestation records conflicting source revisions: {sorted(unique)}"
                    )
                return found[0]
    return None


def _runtime_module():
    """Import the entrypoint so contracts are generated from real behaviour."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "af3_runtime_for_contract", ROOT / "runtime" / "af3_runtime.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise BuildError("cannot import the runtime entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.modules["af3_runtime_for_contract"] = module
    spec.loader.exec_module(module)
    return module


def _templatize(argv: list[str]) -> list[str]:
    rendered = []
    for item in argv:
        for sentinel, placeholder in SENTINELS.items():
            item = item.replace(sentinel, placeholder)
        rendered.append(item)
    return rendered


def command_io_contract() -> dict[str, Any]:
    """Generate the single machine-readable command and IO contract.

    Both argv templates are produced by calling the runtime's own composers, so
    the published contract cannot drift away from what the image actually runs.
    """
    af3 = _runtime_module()
    cache = af3.CacheReport(
        root="/cache/alphafold3",
        jax_dir="/sentinel/cache/jax",
        triton_dir="/cache/alphafold3/triton",
        xdg_dir="/cache/alphafold3/xdg",
        writable=True,
    )
    data_plan = af3.compose_data_argv(
        json_path=Path("/sentinel/json-path"),
        output_dir=Path("/sentinel/output-dir"),
        database_root=Path("/sentinel/database-root"),
        cache=cache,
        threads=127,
    )
    inference_plan = af3.compose_inference_argv(
        json_path=Path("/sentinel/json-path"),
        output_dir=Path("/sentinel/output-dir"),
        model_dir=Path("/sentinel/model-dir"),
        cache=cache,
    )
    interpreter = data_plan.argv[0]

    return {
        "contract": "fs2.cancer-immunotherapy.alphafold3.command-io",
        "schema": COMMAND_IO_SCHEMA,
        "contract_version": "1.0.0",
        "runtime_id": RUNTIME_ID,
        "generated_by": "models/cancer-immunotherapy/images/alphafold3/build.py contract",
        "generated_from": (
            "runtime/af3_runtime.py compose_data_argv and compose_inference_argv, so these "
            "templates are the argv the image actually runs"
        ),
        "entrypoint": {
            "command": ["/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py"],
            "default_args": ["verify"],
            "cli_name": "af3-runtime",
            "interpreter": interpreter,
            "upstream_script": af3._run_script(),
            "workdir": "/app/alphafold",
            "modes": sorted(
                ["verify", "smoke", "params-load", "data", "inference", "plan"]
            ),
        },
        "stages": {
            "data": {
                "role": "cpu",
                "gpu": 0,
                "runtime_args": [
                    "data",
                    "--json-path", "{json_path}",
                    "--output-dir", "{output_dir}",
                    "--reference-receipt", "{reference_receipt}",
                    "--threads", "{msa_threads}",
                    "--cpu-request", "{cpu_request}",
                    "--raw-input-artifact-id", "{raw_input_artifact_id}",
                    "--raw-input-sha256", "{raw_input_sha256}",
                ],
                "optional_runtime_args": [
                    "--database-root", "--reference-manifest", "--manifest-uri",
                    "--emit-preprocess-reference", "--receipt", "--dry-run", "--extra-arg",
                ],
                "composed_upstream_argv": _templatize(data_plan.argv),
                "inputs": [
                    {
                        "name": "fold_input",
                        "placeholder": "{json_path}",
                        "media_type": "application/json",
                        "description": "AlphaFold 3 fold input JSON",
                        "read_only": True,
                        "required": True,
                    },
                    {
                        "name": "reference_receipt",
                        "placeholder": "{reference_receipt}",
                        "media_type": "application/json",
                        "schema": af3.TERMINAL_RECEIPT_SCHEMA,
                        "description": "producer-generated terminal reference-data receipt",
                        "read_only": True,
                        "required": True,
                    },
                    {
                        "name": "reference_root",
                        "mount_path": af3.REFERENCE_MOUNT_PATH,
                        "host_root": af3.REFERENCE_HOST_ROOT,
                        "description": (
                            "the single read-only reference root; both the receipt's "
                            "dataset_sub_path and manifests/sha256/<manifest>.json resolve "
                            "beneath it"
                        ),
                        "read_only": True,
                        "required": True,
                    },
                ],
                "outputs": [
                    {
                        "name": "data_pipeline_output",
                        "placeholder": "{output_dir}",
                        "description": (
                            "AlphaFold 3 writes one directory per fold job, "
                            "<output_dir>/<sanitized_name>/<sanitized_name>_data.json, and "
                            "processes one entry per input, so an input directory yields "
                            "several outputs whose names depend on upstream's own "
                            "sanitization of the job name."
                        ),
                        "writable": True,
                    },
                    {
                        "name": "data_handoff",
                        "path": "{output_dir}/" + af3.DATA_HANDOFF_DIRNAME,
                        "index": af3.DATA_HANDOFF_INDEX,
                        "schema": af3.DATA_HANDOFF_SCHEMA,
                        "description": (
                            "A packaged, relocatable handoff the wrapper writes after a "
                            "successful data stage. Every produced *_data.json is copied "
                            "under it as <fold_job>/<fold_job>_data.json, and index.json "
                            "records one entry per fold job with a path relative to the "
                            "handoff directory, a byte count and a SHA-256."
                        ),
                        "multiplicity": (
                            "one entry per fold job the stage processed, at least one; "
                            "duplicate sanitized names are refused"
                        ),
                        "portability": (
                            "Mount this directory into the GPU stage and pass --handoff-dir. "
                            "Paths are relative, so the GPU pod reconstructs them under its "
                            "own artifact mount. No absolute path from the CPU pod is "
                            "recorded or reused."
                        ),
                        "writable": True,
                    },
                    {
                        "name": "runtime_receipt",
                        "stream": "stdout",
                        "schema": af3.RECEIPT_SCHEMA,
                        "description": "the stage receipt, also written to --receipt when given",
                    },
                ],
                "required_env": {},
                "forbidden": [
                    "the licensed parameter binding",
                    "any GPU request or GPU node selector",
                    "relying on the upstream MSA thread defaults",
                ],
                "extra_arg_policy": {
                    "mode": "positive-allowlist",
                    "allowed_flags": sorted(af3.ALLOWED_EXTRA_FLAGS),
                    "stage_critical_flags": sorted(af3.STAGE_CRITICAL_FLAGS),
                    "rejected_parser_directives": sorted(af3.PARSER_META_FLAGS),
                    "denies_duplicates": True,
                    "reason": (
                        "Anything not on the allowlist is refused. A denylist cannot be "
                        "complete, because Abseil resolves --flagfile by reading further "
                        "flags out of a file, so a denied flag could be smuggled back in "
                        "through one indirection and every new upstream flag would default "
                        "to allowed. absl's --no<flag> negation is normalised before the "
                        "check."
                    ),
                },
            },
            "inference": {
                "role": "gpu",
                "gpu": 1,
                "runtime_args": [
                    "inference",
                    "--handoff-dir", "{handoff_dir}",
                    "--output-dir", "{output_dir}",
                    "--expected-raw-input-artifact-id", "{raw_input_artifact_id}",
                    "--expected-raw-input-sha256", "{raw_input_sha256}",
                ],
                "input_selection": (
                    "Exactly one of --handoff-dir, the directory the data stage packaged, or "
                    "--json-path for a fold input that already carries its MSAs. "
                    "--fold-job is required when the handoff holds more than one job and is "
                    "rejected as ambiguous otherwise."
                ),
                "optional_runtime_args": [
                    "--fold-job", "--json-path", "--parameter-path", "--flash-attention",
                    "--deep-verify", "--receipt", "--dry-run", "--extra-arg",
                ],
                "composed_upstream_argv": _templatize(inference_plan.argv),
                "inputs": [
                    {
                        "name": "handoff",
                        "placeholder": "{handoff_dir}",
                        "index": af3.DATA_HANDOFF_INDEX,
                        "schema": af3.DATA_HANDOFF_SCHEMA,
                        "description": (
                            "The directory the CPU stage packaged, mounted read-only. The "
                            "stage reconstructs each payload path under this mount and "
                            "verifies its SHA-256 before AlphaFold 3 sees it."
                        ),
                        "read_only": True,
                        "required": True,
                        "alternative": "--json-path for a direct fold input",
                    },
                    {
                        "name": "parameters",
                        "mount_path": "/models/af3.bin.zst",
                        "claim": "academic-assets-runtime-rwx",
                        "claim_namespace": "fs2-academic-poc",
                        "source_sub_path": "alphafold3/af3.bin.zst",
                        "sha256": (
                            "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
                        ),
                        "size_bytes": 1020545840,
                        "read_only": True,
                        "required": True,
                        "supplemental_group": 65532,
                    },
                ],
                "outputs": [
                    {
                        "name": "structures",
                        "placeholder": "{output_dir}",
                        "description": "structure and confidence outputs",
                        "writable": True,
                    },
                    {
                        "name": "runtime_receipt",
                        "stream": "stdout",
                        "schema": af3.RECEIPT_SCHEMA,
                        "description": "the stage receipt, also written to --receipt when given",
                    },
                ],
                "required_env": {},
                "forbidden": [
                    "the reference database tree, whether mounted as the whole reference "
                    "root or as a single dataset",
                    "running the data pipeline",
                ],
                "extra_arg_policy": {
                    "mode": "positive-allowlist",
                    "allowed_flags": sorted(af3.ALLOWED_EXTRA_FLAGS),
                    "stage_critical_flags": sorted(af3.STAGE_CRITICAL_FLAGS),
                    "rejected_parser_directives": sorted(af3.PARSER_META_FLAGS),
                    "denies_duplicates": True,
                    "reason": (
                        "Anything not on the allowlist is refused. A denylist cannot be "
                        "complete, because Abseil resolves --flagfile by reading further "
                        "flags out of a file, so a denied flag could be smuggled back in "
                        "through one indirection and every new upstream flag would default "
                        "to allowed. absl's --no<flag> negation is normalised before the "
                        "check."
                    ),
                },
            },
        },
        "root_layout": {
            "reference_root": {
                "mount_path": af3.REFERENCE_MOUNT_PATH,
                "host_root": af3.REFERENCE_HOST_ROOT,
                "read_only": True,
                "single_mount": True,
                "resolves": [
                    "{mount_path}/{dataset_sub_path}",
                    "{mount_path}/manifests/sha256/{manifest_sha256}.json",
                ],
                "dataset_sub_path_template": (
                    "datasets/<bundle_id>/<revision>/sha256/<content_tree_sha256>"
                ),
                "readiness_marker": af3.MANIFEST_MARKER,
                "note": (
                    "Mounting only the dataset is not a supported shape, because the manifest "
                    "could then not be verified against the tree it describes."
                ),
            },
            "parameters": {"mount_path": "/models/af3.bin.zst", "model_dir": "/models"},
            "cache": {
                "root": "/cache/alphafold3",
                "jax": "/cache/alphafold3/jax",
                "triton": "/cache/alphafold3/triton",
                "xdg": "/cache/alphafold3/xdg",
                "kind": "xla-and-triton-compilation-cache",
                "is_gpu_snapshot": False,
                "optional": True,
            },
            "scratch": {"path": "/scratch", "writable": True, "per_pod": True},
            "output": {"path": "/output", "writable": True},
        },
        "result_envelope": {
            "schema": af3.RECEIPT_SCHEMA,
            "json_schema": "schemas/af3-runtime-receipt.schema.json",
            "stream": "stdout",
            "also_written_to": "--receipt <path>",
            "required_fields": ["schema", "mode", "status"],
            "status_values": ["PASS", "PLANNED", "FAIL"],
            "failure_field": "error",
            "terminal_rule": (
                "For the data and inference stages the receipt is written only after "
                "run_alphafold.py exits, and it carries an execution block with that exit "
                "code and a terminal_state of succeeded or failed. A failed run never leaves "
                "a PASS receipt behind."
            ),
            "execution_block": ["upstream", "exit_code", "terminal_state"],
            "exit_codes": {
                "0": "the requested mode succeeded",
                "2": "a binding, identity or stage-separation requirement was not met",
                "other": "the exit code of run_alphafold.py, passed through unchanged",
            },
        },
        "legacy_aliases": {
            "fs2-run-alphafold3": {
                "supported": False,
                "reason": (
                    "No such entrypoint or flag set exists in this runtime. The adapter that "
                    "targets it cannot invoke this image. Supporting an undocumented alias "
                    "would create a second, untested command surface for the same runtime."
                ),
                "action": (
                    "The adapter must target this contract's runtime_args. Coordinate the "
                    "change with the adapter task rather than adding an alias here."
                ),
            },
            "policy": (
                "This contract is the only supported command surface. No undocumented alias "
                "is accepted without an explicit, recorded justification."
            ),
        },
    }


def entrypoint_source() -> str:
    return (ROOT / "runtime" / "af3_runtime.py").read_text(encoding="utf-8")


def check() -> dict[str, Any]:
    """Validate every committed contract and the Dockerfile's invariants."""
    findings: list[str] = []

    source_lock = _load(CONTRACTS / "af3-runtime-source-lock.json")
    binding = _load(CONTRACTS / "af3-parameter-binding.json")
    reference = _load(CONTRACTS / "af3-reference-data-binding.json")
    handoff = _load(CONTRACTS / "af3-runtime-handoff.json")

    upstream = source_lock.get("upstream", {})
    if upstream.get("commit") != UPSTREAM_COMMIT:
        findings.append("source lock commit does not match the pinned upstream revision")
    if upstream.get("tree") != UPSTREAM_TREE:
        findings.append("source lock tree does not match the pinned upstream tree")
    if upstream.get("version") != UPSTREAM_VERSION:
        findings.append("source lock version is not 3.0.4")
    if source_lock.get("access_class", {}).get("parameters") == "public":
        findings.append("source lock must not describe the parameters as public")

    artifact = binding.get("artifact", {})
    if artifact.get("size_bytes") != 1020545840:
        findings.append("parameter binding size does not match the authorized artifact")
    if artifact.get("sha256") != (
        "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
    ):
        findings.append("parameter binding digest does not match the authorized artifact")
    if artifact.get("content_manifest_algorithm") is not None:
        findings.append("a single-file artifact must not declare a tree manifest algorithm")
    if binding.get("license", {}).get("embed_in_image") is not False:
        findings.append("parameter binding must declare embed_in_image false")
    modes = {mode.get("mode"): mode for mode in binding.get("delivery", {}).get("supported_modes", [])}
    canonical = modes.get("subpath-file-mount", {})
    if canonical.get("consumer_path") != "/models/af3.bin.zst":
        findings.append("canonical binding must consume /models/af3.bin.zst")
    if canonical.get("source_sub_path") != "alphafold3/af3.bin.zst":
        findings.append("canonical binding must use subPath alphafold3/af3.bin.zst")
    if binding.get("delivery", {}).get("permissions", {}).get("asset_gid") != 65532:
        findings.append("parameter binding must require supplemental group 65532")
    if binding.get("delivery", {}).get("permissions", {}).get("fs_group_forbidden") is not True:
        findings.append("parameter binding must forbid fsGroup")

    identities = reference.get("identities", {})
    if set(identities.get("required", [])) != {"content_tree_sha256", "manifest_sha256"}:
        findings.append(
            "reference binding must require exactly the producer's two identities, "
            "content_tree_sha256 and manifest_sha256"
        )
    if identities.get("content_tree_sha256") is not None or (
        identities.get("manifest_sha256") is not None
    ):
        findings.append(
            "reference identities must stay null until the reference worker publishes them"
        )
    if reference.get("state") != "pending-publication":
        findings.append("reference binding state must remain pending-publication until published")
    receipt_contract = reference.get("producer_receipt", {})
    if receipt_contract.get("schema") != "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1":
        findings.append("reference binding must name the producer's terminal receipt schema")
    if receipt_contract.get("exact_fields") != [
        "schema", "bundle_id", "revision", "created_at", "storage", "content", "placement"
    ]:
        findings.append("reference binding must declare the producer's exact receipt fields")
    if receipt_contract.get("exact_storage_fields") != [
        "host_root", "mount_path", "dataset_sub_path", "read_only"
    ]:
        findings.append("reference binding must declare the producer's exact storage fields")
    if receipt_contract.get("carries_manifest_uri") is not False:
        findings.append("the producer receipt carries no manifest URI; the contract must say so")
    transform = reference.get("preprocess_request_transform", {})
    if transform.get("output_fields") != [
        "bundle_id", "revision", "manifest_uri", "manifest_sha256"
    ]:
        findings.append("the preprocess transform must emit exactly the producer's four fields")
    storage = reference.get("storage", {})
    if storage.get("shared_filesystem_host_root") != "/mnt/fs2-reference-data/data":
        findings.append("reference binding must name the shared filesystem host root")
    if storage.get("container_mount_path") != "/reference-data":
        findings.append("reference binding must name the read-only container mount path")
    if storage.get("dataset_sub_path_template") != (
        "datasets/<bundle_id>/<revision>/sha256/<content_tree_sha256>"
    ):
        findings.append("reference binding must declare the publisher's exact dataset layout")

    entrypoint = entrypoint_source()
    for required in (
        "def validate_terminal_receipt",
        "reference-data-terminal-receipt/v1",
        "HANDOFF_ALIASES",
        "REFERENCE_HOST_ROOT",
        "def read_tree_manifest_marker",
    ):
        if required not in entrypoint:
            findings.append(f"the entrypoint is missing the required element {required!r}")

    image = handoff.get("image", {})
    if image.get("repository") != "withheld":
        findings.append("the handoff must not commit a concrete registry account path")
    if image.get("digest") is not None and image.get("digest_state") != "published":
        findings.append("handoff digest state is inconsistent with its digest")
    if handoff.get("cache_levels", {}).get("is_gpu_snapshot") is not False:
        findings.append("the handoff must never describe the compiler cache as a GPU snapshot")
    if handoff.get("cache_levels", {}).get("claimed_levels_above_L1"):
        findings.append("no cache level above L1 may be claimed without measured evidence")
    stages = {stage.get("stage"): stage for stage in handoff.get("stages", [])}
    if set(stages) != {"data", "inference"}:
        findings.append("the handoff must declare exactly the data and inference stages")
    if stages.get("data", {}).get("gpu") != 0:
        findings.append("the CPU data stage must request no GPU")
    if stages.get("inference", {}).get("gpu") != 1:
        findings.append("the GPU inference stage must request exactly one GPU")
    readiness = handoff.get("readiness", {})
    # "ready" is reserved for a model the platform can actually serve. The
    # runtime being qualified on real hardware is a different, weaker claim.
    if readiness.get("state") not in {"not-ready", "runtime-qualified"}:
        findings.append(
            "readiness state must be not-ready, or runtime-qualified once the runtime has "
            "passed real hardware acceptance; it may not claim the model is servable"
        )
    if not readiness.get("blocking"):
        findings.append("readiness must list what still blocks the model from being servable")
    if readiness.get("state") == "runtime-qualified":
        completed = " ".join(readiness.get("completed", [])).lower()
        if "h100" not in completed:
            findings.append(
                "runtime-qualified requires recorded real hardware acceptance in completed"
            )

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [
        line for line in dockerfile.splitlines() if line.startswith("FROM ") and "scratch" not in line
    ]
    if not from_lines or not all(
        re.search(r"@sha256:[a-f0-9]{64}(?: AS [a-z0-9]+)?$", line) for line in from_lines
    ):
        findings.append("every FROM must be pinned to an image digest")
    if "apt-get upgrade" in dockerfile or "apk upgrade" in dockerfile:
        findings.append("the Dockerfile must not perform a blanket package upgrade")
    for required in (
        f'AF3_COMMIT={UPSTREAM_COMMIT}',
        f'AF3_TREE={UPSTREAM_TREE}',
        "SETUPTOOLS_SCM_PRETEND_VERSION",
        "USER 1001:1001",
        "licensed payload present in image",
        "reference database present in image",
    ):
        if required not in dockerfile:
            findings.append(f"the Dockerfile is missing the required element {required!r}")
    if "useradd" in dockerfile or "groupadd" in dockerfile:
        findings.append("house style forbids useradd/groupadd; use a numeric USER")

    entrypoint = entrypoint_source()
    if "def verify_parameter_artifact" not in entrypoint:
        findings.append("the entrypoint must verify the parameter artifact")
    if "must never be equated" not in entrypoint:
        findings.append("the entrypoint must reject equated reference identities")

    _validate_schemas(findings)

    if findings:
        raise BuildError("; ".join(findings))

    return {
        "schema": "fs2-serve.nebius.ai/alphafold3-image-check/v1",
        "status": "PASS",
        "runtime_id": RUNTIME_ID,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_version": UPSTREAM_VERSION,
        "contracts": sorted(path.name for path in CONTRACTS.glob("*.json")),
        "schemas": sorted(path.name for path in SCHEMAS.glob("*.json")),
    }


def _validate_schemas(findings: list[str]) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        findings.append("jsonschema is required to validate the committed schemas")
        return
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        try:
            Draft202012Validator.check_schema(_load(path))
        except Exception as error:  # noqa: BLE001 - report any schema defect
            findings.append(f"{path.name} is not a valid Draft 2020-12 schema: {error}")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build(
    *,
    tag: str,
    oci_file: Path,
    builder: str | None,
    metadata_file: Path,
    source_revision: str | None = None,
) -> dict[str, Any]:
    require_tool("docker")
    if oci_file.exists():
        raise BuildError(f"refusing to overwrite an existing OCI archive at {oci_file}")

    state = source_state()
    if state["missing_context_paths"]:
        raise BuildError(
            f"build context files are missing: {state['missing_context_paths']}"
        )
    if state["dirty_context_paths"]:
        raise BuildError(
            "the build context has uncommitted changes in "
            f"{state['dirty_context_paths']}. Commit them first: the provenance records the "
            "HEAD revision, so an image built from a dirty tree would point at source that "
            "does not match it."
        )
    if source_revision and source_revision != state["commit"]:
        raise BuildError(
            f"requested source revision {source_revision} is not HEAD ({state['commit']}). "
            "Build from the exact commit the image is meant to carry."
        )
    context = context_digest()
    created = utcnow()
    command = ["docker", "buildx", "build"]
    if builder:
        command += ["--builder", builder]
    command += [
        "--platform",
        "linux/amd64",
        "--provenance=mode=max",
        f"--attest=type=sbom,generator={SBOM_GENERATOR}",
        f"--output=type=oci,dest={oci_file}",
        f"--metadata-file={metadata_file}",
        f"--label=org.opencontainers.image.created={created}",
        "--tag",
        tag,
        "--file",
        str(ROOT / "Dockerfile"),
        str(ROOT),
    ]
    result = run(command, capture=False, check=False)
    if result.returncode != 0:
        raise BuildError(f"docker buildx build failed with exit code {result.returncode}")

    attestation = verify_attestations(oci_file)

    # The defect this guards against: an image whose provenance names a commit
    # that is not the commit its source came from.
    recorded = attestation_vcs_revision(oci_file)
    if recorded is None:
        raise BuildError("the SLSA provenance records no source revision")
    if recorded != state["commit"]:
        raise BuildError(
            f"the SLSA provenance records source revision {recorded}, but the build context "
            f"is at {state['commit']}. Refusing to hand back an image whose provenance points "
            "at different source."
        )
    after = source_state()
    if after["commit"] != state["commit"] or after["dirty_context_paths"]:
        raise BuildError(
            "the build context changed while the image was being built; the provenance can no "
            "longer be trusted to describe it"
        )

    metadata = _load(metadata_file) if metadata_file.exists() else {}

    return {
        "schema": "fs2-serve.nebius.ai/alphafold3-image-build/v1",
        "status": "PASS",
        "runtime_id": RUNTIME_ID,
        "tag": tag,
        "platform": "linux/amd64",
        "created": created,
        "builder": builder,
        "base_image_digest": BASE_IMAGE_DIGEST,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_tree": UPSTREAM_TREE,
        "source_revision": state["commit"],
        "source_tree": state["tree"],
        "attestation_vcs_revision": recorded,
        "context_sha256": context["context_sha256"],
        "context_files": context["files"],
        "oci_archive_sha256": sha256_of(oci_file),
        "oci_archive_bytes": oci_file.stat().st_size,
        **attestation,
        "buildx_metadata_digest": metadata.get("containerimage.digest"),
    }


def _oci_blob(archive: tarfile.TarFile, digest: str) -> bytes:
    member = f"blobs/{digest.replace(':', '/')}"
    handle = archive.extractfile(member)
    if handle is None:
        raise BuildError(f"OCI archive is missing blob {digest}")
    return handle.read()


def verify_attestations(oci_file: Path) -> dict[str, Any]:
    """Prove the archive carries exactly the expected SBOM and provenance.

    Every in-toto statement must be about this image, and the set of predicate
    types must equal the expected set, so a missing SBOM cannot pass unnoticed.
    """
    with tarfile.open(oci_file) as archive:
        index = json.loads(_oci_blob_index(archive))
        manifest_digest = index["manifests"][0]["digest"]
        top = json.loads(_oci_blob(archive, manifest_digest))
        if top.get("mediaType") not in (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        ):
            raise BuildError("expected an OCI image index at the top of the archive")

        image_manifests = []
        attestation_manifests = []
        for entry in top.get("manifests", []):
            annotations = entry.get("annotations") or {}
            if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
                attestation_manifests.append(entry)
            else:
                image_manifests.append(entry)

        if len(image_manifests) != 1:
            raise BuildError(
                f"expected exactly one image manifest, found {len(image_manifests)}"
            )
        image_digest = image_manifests[0]["digest"]
        if len(attestation_manifests) != 1:
            raise BuildError(
                f"expected exactly one attestation manifest, found {len(attestation_manifests)}"
            )
        referenced = (attestation_manifests[0].get("annotations") or {}).get(
            "vnd.docker.reference.digest"
        )
        if referenced != image_digest:
            raise BuildError("the attestation manifest does not reference this image")

        attestation = json.loads(_oci_blob(archive, attestation_manifests[0]["digest"]))
        predicates: set[str] = set()
        statement_types: set[str] = set()
        for layer in attestation.get("layers", []):
            statement = json.loads(_oci_blob(archive, layer["digest"]))
            statement_types.add(str(statement.get("_type")))
            predicates.add(str(statement.get("predicateType")))
            for subject in statement.get("subject", []):
                subject_digest = (subject.get("digest") or {}).get("sha256")
                if subject_digest and f"sha256:{subject_digest}" != image_digest:
                    raise BuildError("an attestation subject is not this image")

        if predicates != EXPECTED_ATTESTATIONS:
            raise BuildError(
                "attestation predicate types are "
                f"{sorted(predicates)}, expected {sorted(EXPECTED_ATTESTATIONS)}"
            )
        if not statement_types <= SUPPORTED_STATEMENT_TYPES:
            raise BuildError(f"unsupported in-toto statement types: {sorted(statement_types)}")

        return {
            "image_manifest_digest": image_digest,
            "attestation_manifest_digest": attestation_manifests[0]["digest"],
            "attestation_predicates": sorted(predicates),
            "attestation_statement_types": sorted(statement_types),
        }


def _oci_blob_index(archive: tarfile.TarFile) -> bytes:
    handle = archive.extractfile("index.json")
    if handle is None:
        raise BuildError("OCI archive has no index.json")
    return handle.read()


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def inspect(oci_file: Path) -> dict[str, Any]:
    """Walk every layer and prove no licensed or database payload is embedded.

    This reads the actual layer tars rather than trusting a build-time check, so
    the guarantee holds for the artefact that would be published.
    """
    offenders: list[str] = []
    entries = 0
    layers = 0
    largest: list[tuple[int, str]] = []

    with tarfile.open(oci_file) as archive:
        index = json.loads(_oci_blob_index(archive))
        top = json.loads(_oci_blob(archive, index["manifests"][0]["digest"]))
        image_entry = next(
            entry
            for entry in top["manifests"]
            if (entry.get("annotations") or {}).get("vnd.docker.reference.type")
            != "attestation-manifest"
        )
        manifest = json.loads(_oci_blob(archive, image_entry["digest"]))

        for layer in manifest.get("layers", []):
            media = layer.get("mediaType", "")
            if "tar" not in media:
                continue
            layers += 1
            blob = _oci_blob(archive, layer["digest"])
            raw = gzip.decompress(blob) if media.endswith("gzip") else blob
            with tarfile.open(fileobj=io.BytesIO(raw)) as layer_tar:
                for member in layer_tar:
                    entries += 1
                    name = member.name.lstrip("./")
                    base = Path(name).name
                    if member.isfile():
                        if FORBIDDEN_NAME_RE.search(name) or base in REFERENCE_DB_NAMES:
                            offenders.append(name)
                        if member.size >= LARGE_ENTRY_BYTES:
                            largest.append((member.size, name))
                    elif member.isdir() and base in REFERENCE_DB_DIRS:
                        offenders.append(name)

    if layers == 0:
        raise BuildError("no filesystem layers were found in the archive")
    if offenders:
        raise BuildError(
            "image layers contain payload that must never be embedded: "
            + ", ".join(sorted(set(offenders)))
        )

    largest.sort(reverse=True)
    return {
        "schema": "fs2-serve.nebius.ai/alphafold3-image-hygiene/v1",
        "status": "PASS",
        "layer_inspection": {
            "method": "OCI archive layer tar walk over every filesystem layer",
            "layers_inspected": layers,
            "entries_inspected": entries,
            "offenders": [],
            "largest_entries": [
                {"path": name, "bytes": size} for size, name in largest[:10]
            ],
        },
        "parameters_embedded": False,
        "reference_databases_embedded": False,
    }


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------


def smoke(*, oci_file: Path, tag: str) -> dict[str, Any]:
    """Load the archive locally and run the entrypoint's offline probes.

    Only checks that need no GPU and no licensed bytes run here. The parameter
    load and inference checks require a real GPU and the mounted academic
    artifact, and are recorded as live H100 evidence instead.
    """
    require_tool("skopeo")
    require_tool("docker")
    run(
        [
            "skopeo",
            "copy",
            "--override-os",
            "linux",
            "--override-arch",
            "amd64",
            f"oci-archive:{oci_file}",
            f"docker-daemon:{tag}",
        ],
        capture=False,
    )

    inspected = json.loads(run(["docker", "image", "inspect", tag]).stdout)[0]
    labels = inspected.get("Config", {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != UPSTREAM_COMMIT:
        raise BuildError("built image revision label does not match the pinned upstream commit")
    if inspected.get("Config", {}).get("User") != "1001:1001":
        raise BuildError("built image does not default to the nonroot uid 1001:1001")

    verify = run(["docker", "run", "--rm", tag, "verify"], check=False)
    if verify.returncode != 0:
        raise BuildError(f"entrypoint verify failed: {verify.stdout}\n{verify.stderr}")
    verify_receipt = json.loads(verify.stdout)

    version = run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/alphafold3_venv/bin/python3",
            tag,
            "-c",
            "import importlib.metadata as m; print(m.version('alphafold3'))",
        ]
    ).stdout.strip()
    if version != UPSTREAM_VERSION:
        raise BuildError(f"installed distribution is {version}, expected {UPSTREAM_VERSION}")

    cli = run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/alphafold3_venv/bin/python3",
            tag,
            "/app/alphafold/run_alphafold.py",
            "--help",
        ],
        check=False,
    )
    help_ok = "--model_dir" in cli.stdout and "--db_dir" in cli.stdout

    apt_versions = _apt_versions(tag)

    return {
        "schema": "fs2-serve.nebius.ai/alphafold3-image-smoke/v1",
        "status": "PASS",
        "tag": tag,
        "image_id": inspected.get("Id"),
        "distribution_version": version,
        "entrypoint_exit_code": verify.returncode,
        "cli_help_ok": help_ok,
        "gpu_present": bool(verify_receipt.get("devices", {}).get("gpu_present", False)),
        "verify_receipt": verify_receipt,
        "apt_packages": apt_versions,
        "note": (
            "Offline probes only. No GPU and no licensed parameters were present, so the "
            "parameter load and inference checks are recorded separately as live H100 evidence."
        ),
    }


def _apt_versions(tag: str) -> dict[str, str]:
    """Record exact apt versions so package drift between builds is detectable."""
    packages = ("python3.12", "zlib1g", "libgomp1", "zstd", "ca-certificates")
    query = run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "dpkg-query",
            tag,
            "-W",
            "-f=${Package} ${Version}\n",
            *packages,
        ],
        check=False,
    )
    versions: dict[str, str] = {}
    for line in query.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            versions[parts[0]] = parts[1]
    return versions


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


def lock(
    *,
    build_receipt: Path,
    hygiene_receipt: Path,
    smoke_receipt: Path | None,
    publication_receipt: Path | None,
    destination: Path,
) -> dict[str, Any]:
    built = _load(build_receipt)
    hygiene = _load(hygiene_receipt)
    smoked = _load(smoke_receipt) if smoke_receipt else None
    published = _load(publication_receipt) if publication_receipt else None

    document: dict[str, Any] = {
        "schema": IMAGE_LOCK_SCHEMA,
        "runtime_id": RUNTIME_ID,
        "source": {
            "upstream_commit": UPSTREAM_COMMIT,
            "upstream_tree": UPSTREAM_TREE,
            "upstream_version": UPSTREAM_VERSION,
            "source_lock_sha256": sha256_of(CONTRACTS / "af3-runtime-source-lock.json"),
            "dockerfile_sha256": sha256_of(ROOT / "Dockerfile"),
            "entrypoint_sha256": sha256_of(ROOT / "runtime" / "af3_runtime.py"),
            "parameter_binding_sha256": sha256_of(CONTRACTS / "af3-parameter-binding.json"),
            "source_revision": built["source_revision"],
            "source_tree": built.get("source_tree"),
            "attestation_vcs_revision": built["attestation_vcs_revision"],
            "context_sha256": built["context_sha256"],
            "context_files": built["context_files"],
        },
        "build": {
            "platform": "linux/amd64",
            "base_image_digest": BASE_IMAGE_DIGEST,
            "image_manifest_digest": built["image_manifest_digest"],
            "attestation_manifest_digest": built.get("attestation_manifest_digest"),
            "attestation_predicates": built["attestation_predicates"],
            "oci_archive_sha256": built["oci_archive_sha256"],
            "created": built["created"],
            "builder": built.get("builder"),
        },
        "publication": {
            "state": "unpublished",
            "repository": "withheld",
            "region": None,
            "tag": built.get("tag"),
            "digest": None,
            "published_at": None,
            "overwrote_existing_tag": False,
        },
        "hygiene": {
            "layer_inspection": hygiene["layer_inspection"],
            "parameters_embedded": False,
            "reference_databases_embedded": False,
        },
    }

    if smoked:
        document["build"]["apt_packages"] = smoked.get("apt_packages", {})
        document["smoke"] = {
            "distribution_version": smoked["distribution_version"],
            "entrypoint_exit_code": smoked["entrypoint_exit_code"],
            "cli_help_ok": bool(smoked.get("cli_help_ok")),
            "gpu_present": bool(smoked.get("gpu_present")),
            "note": smoked.get("note"),
        }
    if published:
        document["publication"].update(
            {
                "state": "published",
                "region": published.get("region"),
                "tag": published.get("tag"),
                "digest": published["digest"],
                "published_at": published["published_at"],
                "overwrote_existing_tag": False,
            }
        )

    if document["source"]["attestation_vcs_revision"] != document["source"]["source_revision"]:
        raise BuildError(
            "the build receipt records a provenance revision that differs from its source "
            "revision; the image cannot be locked"
        )
    _validate_against(document, SCHEMAS / "af3-image-lock.schema.json")
    emit(document, destination)
    return document


def _validate_against(document: dict[str, Any], schema_path: Path) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as error:
        raise BuildError("jsonschema is required to validate the image lock") from error
    validator = Draft202012Validator(_load(schema_path))
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.path))
    if errors:
        joined = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise BuildError(f"document does not satisfy {schema_path.name}: {joined}")


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def publish(*, oci_file: Path, repository: str, tag: str, region: str) -> dict[str, Any]:
    """Push to the project registry, refusing to overwrite an existing tag."""
    require_tool("crane")
    require_tool("skopeo")

    destination = f"{repository}:{tag}"
    existing = run(["crane", "digest", destination], check=False)
    if existing.returncode == 0 and existing.stdout.strip():
        raise BuildError(
            f"destination tag {destination} already exists at {existing.stdout.strip()}; "
            "refusing to overwrite a published tag. Publish a new tag instead."
        )

    result = run(
        [
            "skopeo",
            "copy",
            "--all",
            f"oci-archive:{oci_file}",
            f"docker://{destination}",
        ],
        capture=False,
        check=False,
    )
    if result.returncode != 0:
        raise BuildError(
            f"publication to {destination} failed with exit code {result.returncode}. "
            "If this is an authentication or authorization failure, stop and ask the "
            "operator to grant registry access rather than switching credentials."
        )

    digest = run(["crane", "digest", destination]).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise BuildError(f"registry returned an unusable digest for {destination}: {digest!r}")

    return {
        "schema": "fs2-serve.nebius.ai/alphafold3-image-publication/v1",
        "status": "PASS",
        "region": region,
        "tag": tag,
        "digest": digest,
        "published_at": utcnow(),
        "overwrote_existing_tag": False,
        "repository_note": (
            "The concrete registry account path is a deploy-time binding and is not recorded."
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build.py", description=__doc__)
    parser.add_argument(
        "command",
        choices=("check", "build", "inspect", "smoke", "lock", "publish", "contract"),
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="contract: verify the committed contract matches the implementation",
    )
    parser.add_argument("--tag", default=DEFAULT_TAG, help="Local image tag")
    parser.add_argument("--builder", help="Attestation-capable buildx builder")
    parser.add_argument("--oci-file", type=Path, help="OCI archive path")
    parser.add_argument("--metadata-file", type=Path, help="buildx metadata output path")
    parser.add_argument("--receipt", type=Path, help="Write this verb's receipt here")
    parser.add_argument("--build-receipt", type=Path, help="lock: the build receipt")
    parser.add_argument("--hygiene-receipt", type=Path, help="lock: the inspect receipt")
    parser.add_argument("--smoke-receipt", type=Path, help="lock: the smoke receipt")
    parser.add_argument("--publication-receipt", type=Path, help="lock: the publication receipt")
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=CONTRACTS / "af3-image-lock.json",
        help="lock: destination for the committed image lock",
    )
    parser.add_argument(
        "--source-revision",
        help=(
            "build: the commit this image must carry. The build fails unless it equals HEAD "
            "and the provenance records it."
        ),
    )
    parser.add_argument("--repository", help="publish: full destination repository path")
    parser.add_argument("--region", default="eu-north1", help="publish: registry region")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            emit(check(), args.receipt)
            return 0
        if args.command == "build":
            if not args.oci_file:
                raise BuildError("build requires --oci-file")
            metadata = args.metadata_file or args.oci_file.with_suffix(".metadata.json")
            emit(
                build(
                    tag=args.tag,
                    oci_file=args.oci_file,
                    builder=args.builder,
                    metadata_file=metadata,
                    source_revision=args.source_revision,
                ),
                args.receipt,
            )
            return 0
        if args.command == "inspect":
            if not args.oci_file:
                raise BuildError("inspect requires --oci-file")
            emit(inspect(args.oci_file), args.receipt)
            return 0
        if args.command == "smoke":
            if not args.oci_file:
                raise BuildError("smoke requires --oci-file")
            emit(smoke(oci_file=args.oci_file, tag=args.tag), args.receipt)
            return 0
        if args.command == "lock":
            if not args.build_receipt or not args.hygiene_receipt:
                raise BuildError("lock requires --build-receipt and --hygiene-receipt")
            lock(
                build_receipt=args.build_receipt,
                hygiene_receipt=args.hygiene_receipt,
                smoke_receipt=args.smoke_receipt,
                publication_receipt=args.publication_receipt,
                destination=args.lock_file,
            )
            return 0
        if args.command == "contract":
            generated = command_io_contract()
            if args.check_only:
                if not COMMAND_IO_PATH.is_file():
                    raise BuildError(f"{COMMAND_IO_PATH.name} has not been generated yet")
                committed = _load(COMMAND_IO_PATH)
                if committed != generated:
                    raise BuildError(
                        f"{COMMAND_IO_PATH.name} is stale; regenerate it with "
                        "'python3 build.py contract'"
                    )
                emit(
                    {
                        "schema": "fs2-serve.nebius.ai/alphafold3-command-io-check/v1",
                        "status": "PASS",
                        "contract": COMMAND_IO_PATH.name,
                    },
                    args.receipt,
                )
                return 0
            emit(generated, COMMAND_IO_PATH)
            return 0
        if args.command == "publish":
            if not args.oci_file or not args.repository:
                raise BuildError("publish requires --oci-file and --repository")
            emit(
                publish(
                    oci_file=args.oci_file,
                    repository=args.repository,
                    tag=args.tag,
                    region=args.region,
                ),
                args.receipt,
            )
            return 0
        raise BuildError(f"unsupported command {args.command!r}")
    except BuildError as error:
        emit(
            {
                "schema": "fs2-serve.nebius.ai/alphafold3-image-error/v1",
                "status": "FAIL",
                "command": args.command,
                "error": str(error),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
