#!/usr/bin/env python3
"""Exact-revision public artifact acquisition and atomic SFS publication."""

from __future__ import annotations

import hashlib
import fcntl
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Callable

from .artifacts import (
    build_artifact_manifest,
    canonical_bytes,
    sha256_file,
    verify_artifact_tree,
)
from .loader import AcquisitionPlan, CatalogError, ModelRecord, canonical_content_uri
from .staging import LOCALIZER_OWNER


ACQUISITION_WORKER_RESULT_SCHEMA = (
    "fs2-serve.nebius.ai/artifact-acquisition-worker-result/v1"
)
ACQUISITION_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/artifact-acquisition-receipt/v4"
ACQUISITION_RUN_AS_UID = 10001
ACQUISITION_RUN_AS_GID = 10001
ACQUISITION_FS_GROUP = 10001
FRESH_WRITE_PROOF_OPERATION = "exclusive-create-write-fsync-read-unlink"
K8S_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def _process_identity() -> tuple[int, int, tuple[int, ...]]:
    return os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups())))


def _workload_identity() -> dict[str, str]:
    """Read the non-overridable renderer/downward-API execution identity."""

    names = (
        "FS2_ACQUISITION_OPERATION_ID",
        "FS2_ACQUISITION_JOB_NAMESPACE",
        "FS2_ACQUISITION_JOB_NAME",
        "FS2_ACQUISITION_JOB_UID",
        "FS2_ACQUISITION_POD_NAME",
        "FS2_ACQUISITION_POD_UID",
        "FS2_ACQUISITION_HELPER_IMAGE",
        "FS2_ACQUISITION_HELPER_IMAGE_DIGEST",
        "FS2_ACQUISITION_HELPER_ADMISSION_DIGEST",
        "FS2_ACQUISITION_HELPER_REGISTRY_IDENTITY_SHA256",
        "FS2_ACQUISITION_HELPER_BUILD_IDENTITY_SHA256",
        "FS2_ACQUISITION_PLAN_SHA256",
        "FS2_ACQUISITION_HELPER_CONTRACT_SHA256",
    )
    value = {name: os.environ.get(name, "") for name in names}
    if any(not item for item in value.values()):
        raise CatalogError("acquisition helper lacks its renderer/downward-API identity")
    return value


def _strong_digest(value: Any, label: str, *, image: bool = False) -> str:
    prefix = "sha256:" if image else ""
    text = value.removeprefix(prefix) if isinstance(value, str) else ""
    if (
        not isinstance(value, str)
        or (image and not value.startswith(prefix))
        or re.fullmatch(r"[0-9a-f]{64}", text) is None
        or len(set(text)) < 8
    ):
        raise CatalogError(f"{label} is not a strong immutable digest")
    return value


def _validate_workload_identity(
    record: ModelRecord, plan: AcquisitionPlan, value: dict[str, str]
) -> dict[str, Any]:
    acquisition = plan.to_dict()
    helper = acquisition.get("helper_image")
    if not isinstance(helper, dict):
        raise CatalogError("public acquisition plan lacks its helper image contract")
    expected_plan = hashlib.sha256(canonical_bytes(acquisition)).hexdigest()
    expected_helper = hashlib.sha256(canonical_bytes(helper)).hexdigest()
    operation_id = value.get("FS2_ACQUISITION_OPERATION_ID")
    job_name = value.get("FS2_ACQUISITION_JOB_NAME")
    pod_name = value.get("FS2_ACQUISITION_POD_NAME")
    job_uid = value.get("FS2_ACQUISITION_JOB_UID")
    pod_uid = value.get("FS2_ACQUISITION_POD_UID")
    image_digest = _strong_digest(
        value.get("FS2_ACQUISITION_HELPER_IMAGE_DIGEST"),
        "acquisition helper image digest",
        image=True,
    )
    image_reference = value.get("FS2_ACQUISITION_HELPER_IMAGE")
    if (
        not isinstance(operation_id, str)
        or DNS_LABEL.fullmatch(operation_id) is None
        or value.get("FS2_ACQUISITION_JOB_NAMESPACE") != "fs2-models"
        or job_name != f"{record.model_id}-cache-{operation_id}"
        or not isinstance(pod_name, str)
        or not pod_name.startswith(job_name + "-")
        or not isinstance(job_uid, str)
        or K8S_UID.fullmatch(job_uid) is None
        or not isinstance(pod_uid, str)
        or K8S_UID.fullmatch(pod_uid) is None
        or not isinstance(image_reference, str)
        or not image_reference.endswith(helper["repository_suffix"] + "@" + image_digest)
        or value.get("FS2_ACQUISITION_PLAN_SHA256") != expected_plan
        or value.get("FS2_ACQUISITION_HELPER_CONTRACT_SHA256") != expected_helper
    ):
        raise CatalogError("acquisition workload identity differs from its rendered model plan")
    admission_digest = _strong_digest(
        value.get("FS2_ACQUISITION_HELPER_ADMISSION_DIGEST"),
        "acquisition helper admission digest",
    )
    registry_identity = _strong_digest(
        value.get("FS2_ACQUISITION_HELPER_REGISTRY_IDENTITY_SHA256"),
        "acquisition helper registry identity",
    )
    build_identity = _strong_digest(
        value.get("FS2_ACQUISITION_HELPER_BUILD_IDENTITY_SHA256"),
        "acquisition helper build identity",
    )
    return {
        "operation_id": operation_id,
        "helper_image": {
            "id": "fs2-acquisition-helper",
            "reference": image_reference,
            "digest": image_digest,
            "admission_receipt_digest": admission_digest,
            "registry_identity_sha256": registry_identity,
            "build_identity_sha256": build_identity,
            "helper_contract_sha256": expected_helper,
        },
        "job": {
            "api_version": "batch/v1",
            "kind": "Job",
            "namespace": "fs2-models",
            "name": job_name,
            "uid": job_uid,
        },
        "pod": {
            "api_version": "v1",
            "kind": "Pod",
            "namespace": "fs2-models",
            "name": pod_name,
            "uid": pod_uid,
            "owner_job_uid": job_uid,
        },
        "acquisition_plan_sha256": expected_plan,
    }


def _mountinfo_filesystem_type(path: Path) -> str:
    """Return the filesystem type for the longest matching Linux mount point."""

    resolved = path.resolve()
    matches: list[tuple[int, str]] = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError as exc:
        raise CatalogError("cannot inspect the acquisition filesystem mount") from exc
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 >= len(fields) or len(fields) < 5:
            continue
        mount_text = (
            fields[4]
            .replace(r"\040", " ")
            .replace(r"\011", "\t")
            .replace(r"\012", "\n")
            .replace(r"\134", "\\")
        )
        mount = Path(mount_text)
        if resolved == mount or mount in resolved.parents:
            matches.append((len(mount.parts), fields[separator + 1]))
    if not matches:
        raise CatalogError("cannot resolve the acquisition filesystem type")
    return max(matches)[1]


def _fresh_filesystem_write_proof(
    destination: Path,
    operation_id: str,
    *,
    filesystem_type: str,
) -> dict[str, Any]:
    """Prove an exclusive non-root write, durability, readback, and cleanup."""

    payload = b"fs2-provider-block-fresh-write-proof/v1\n"
    marker = destination / f".fs2-write-proof-{operation_id}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    info: os.stat_result | None = None
    try:
        descriptor = os.open(marker, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        info = marker.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or marker.is_symlink()
            or marker.read_bytes() != payload
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise CatalogError(
                "fresh filesystem write proof failed readback or mode validation"
            )
    finally:
        if os.path.lexists(marker):
            marker.unlink()
        directory_descriptor = os.open(
            destination,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    if info is None:
        raise CatalogError("fresh filesystem write proof did not observe its marker")
    return {
        "filesystem_type": filesystem_type,
        "probe_path": str(destination),
        "operation": FRESH_WRITE_PROOF_OPERATION,
        "bytes_written": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "file_uid": info.st_uid,
        "file_gid": info.st_gid,
        "file_mode": "0600",
        "marker_removed": not marker.exists(),
        "directory_fsync": True,
    }


def _write_manifest(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o440)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _materialize_exact_payload(
    scratch: Path,
    payload: Path,
    expected_identity: dict[str, Any],
) -> None:
    """Copy the closed expected tree into a clean payload without metadata or links."""

    allowed = {item["path"]: item for item in expected_identity["files"]}
    if len(allowed) != expected_identity["file_count"]:
        raise CatalogError("expected artifact allowlist has duplicate paths")
    observed: set[str] = set()
    for candidate in sorted(scratch.rglob("*")):
        relative = candidate.relative_to(scratch).as_posix()
        info = candidate.lstat()
        if relative == ".cache" or relative.startswith(".cache/huggingface"):
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise CatalogError("Hugging Face scratch metadata contains an unsafe entry")
            continue
        if stat.S_ISDIR(info.st_mode):
            if not any(path.startswith(relative + "/") for path in allowed):
                raise CatalogError(f"download contains an extra directory: {relative}")
            continue
        if relative not in allowed:
            raise CatalogError(f"download contains an extra artifact entry: {relative}")
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            raise CatalogError(f"download artifact entry is not a regular file: {relative}")
        expected = allowed[relative]
        if info.st_size != expected["bytes"] or sha256_file(candidate) != expected["sha256"]:
            raise CatalogError(f"download artifact entry differs from expected identity: {relative}")
        target = payload / relative
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        target_descriptor = os.open(target, flags, 0o440)
        source_descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            with (
                os.fdopen(source_descriptor, "rb", closefd=False) as source_stream,
                os.fdopen(target_descriptor, "wb", closefd=False) as target_stream,
            ):
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
        finally:
            os.close(source_descriptor)
            os.close(target_descriptor)
        if target.stat().st_size != expected["bytes"] or sha256_file(target) != expected["sha256"]:
            raise CatalogError(f"clean payload verification failed: {relative}")
        observed.add(relative)
    if observed != set(allowed):
        raise CatalogError("download is missing one or more exact allowlisted artifact files")
    if (payload / ".cache" / "huggingface").exists():
        raise CatalogError("clean payload contains forbidden Hugging Face cache metadata")


def acquire_huggingface_artifact(
    record: ModelRecord,
    plan: AcquisitionPlan,
    destination_prefix: Path | str,
    *,
    snapshot_download: Callable[..., str] | None = None,
    reserve_bytes: int = 8 * 1024**3,
    process_identity_probe: Callable[[], tuple[int, int, tuple[int, ...]]] = _process_identity,
    workload_identity_probe: Callable[[], dict[str, str]] = _workload_identity,
    filesystem_type_probe: Callable[[Path], str] = _mountinfo_filesystem_type,
    fresh_write_proof: Callable[..., dict[str, Any]] = _fresh_filesystem_write_proof,
) -> dict[str, Any]:
    """Download one public exact HF revision and atomically publish its inventory."""

    value = record.to_dict()
    acquisition = plan.to_dict()
    if plan.model_id != record.model_id or plan.method != "huggingface-public-snapshot":
        raise CatalogError("public Hugging Face acquisition requires its exact model plan")
    if value["model"]["source"]["entitlement"]["required"] is not False:
        raise CatalogError("public Hugging Face acquisition cannot consume an entitlement")
    if isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0:
        raise CatalogError("acquisition reserve bytes must be a nonnegative integer")
    workload = _validate_workload_identity(record, plan, workload_identity_probe())
    run_as_uid, run_as_gid, supplementary_groups = process_identity_probe()
    if (
        run_as_uid != ACQUISITION_RUN_AS_UID
        or run_as_gid != ACQUISITION_RUN_AS_GID
        or ACQUISITION_FS_GROUP
        not in set(supplementary_groups) | {run_as_gid}
    ):
        raise CatalogError("artifact acquisition must run as the deterministic non-root identity")
    destination = Path(destination_prefix)
    if destination.name != record.model_id or destination.parent.name != "models":
        raise CatalogError("artifact destination is not the model-scoped storage prefix")
    if destination.exists():
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CatalogError("artifact destination prefix must be a non-symlink directory")
    else:
        destination.mkdir(mode=0o750, parents=True)
    if destination.is_symlink():
        raise CatalogError("artifact destination prefix must not be a symlink")

    if snapshot_download is None:
        try:
            from huggingface_hub import snapshot_download as hf_snapshot_download
        except ImportError as exc:
            raise CatalogError("acquisition image lacks the pinned huggingface_hub client") from exc
        snapshot_download = hf_snapshot_download

    operation_id = workload["operation_id"]
    temporary = destination / f".acquire-{operation_id}"
    download_root = temporary / "download"
    payload = temporary / "payload"
    lock_root = destination.parent / ".locks"
    lock_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    lock_path = lock_root / f"{record.model_id}.acquire.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o640,
    )
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogError("another artifact acquirer owns this model path") from exc
        capacity_bound = value["cache"]["artifact"]["capacity_bound_bytes"]
        if isinstance(capacity_bound, bool) or not isinstance(capacity_bound, int):
            raise CatalogError("public artifact acquisition requires a capacity bound")
        free_before = shutil.disk_usage(destination).free
        if free_before < capacity_bound + reserve_bytes:
            raise CatalogError(
                "insufficient destination free space for the model capacity bound and reserve"
            )
        provider_block = (
            acquisition["publication"]
            == "atomic-content-addressed-provider-block-pvc"
        )
        filesystem_write_proof = None
        if provider_block:
            filesystem_type = filesystem_type_probe(destination)
            if filesystem_type != "ext4":
                raise CatalogError("provider block acquisition requires a live ext4 filesystem")
            filesystem_write_proof = fresh_write_proof(
                destination,
                operation_id,
                filesystem_type=filesystem_type,
            )
            if (
                filesystem_write_proof["file_uid"] != ACQUISITION_RUN_AS_UID
                or filesystem_write_proof["file_gid"] != ACQUISITION_RUN_AS_GID
                or filesystem_write_proof["marker_removed"] is not True
            ):
                raise CatalogError("provider block fresh ext4 write proof has wrong ownership")
        temporary.mkdir(mode=0o750)
        try:
            returned = snapshot_download(
                repo_id=acquisition["repository"],
                revision=acquisition["revision"],
                local_dir=str(download_root),
                token=False,
            )
            if Path(returned).resolve() != download_root.resolve():
                raise CatalogError("Hugging Face client wrote outside the owned acquisition path")
            expected_identity = value["cache"]["artifact"].get("expected_identity")
            if expected_identity is not None:
                payload.mkdir(mode=0o750)
                _materialize_exact_payload(download_root, payload, expected_identity)
            else:
                payload = download_root
            manifest = build_artifact_manifest(
                payload,
                model_id=record.model_id,
                kind="weights",
                source_uri=f"hf://{acquisition['repository']}",
                source_revision=acquisition["revision"],
                license_id=value["model"]["source"]["license"]["id"],
                license_state=value["model"]["source"]["license"]["state"],
                entitlement_state=value["model"]["source"]["entitlement"]["state"],
                owner=LOCALIZER_OWNER,
                retention="retained-platform",
            )
            if manifest.expanded_bytes > capacity_bound:
                raise CatalogError("acquired artifact exceeds its reviewed capacity bound")
            if expected_identity is not None and (
                manifest.content_digest != expected_identity["content_digest"]
                or manifest.digest != expected_identity["manifest_digest"]
                or manifest.expanded_bytes != expected_identity["expanded_bytes"]
                or len(manifest.files) != expected_identity["file_count"]
            ):
                raise CatalogError("acquired payload differs from the expected exact-revision identity")
            content_root = destination / "sha256"
            content_root.mkdir(mode=0o750, exist_ok=True)
            published = content_root / manifest.content_digest
            if published.exists():
                if published.is_symlink() or not published.is_dir():
                    raise CatalogError("existing acquired content address is not a directory")
                verify_artifact_tree(manifest, published)
                shutil.rmtree(payload)
                outcome = "already-present"
            else:
                os.replace(payload, published)
                verify_artifact_tree(manifest, published)
                outcome = "acquired"
            manifest_bytes = canonical_bytes(manifest.to_dict())
            manifest_path = destination.parent / ".manifests" / f"{manifest.digest}.json"
            if manifest_path.exists():
                if manifest_path.is_symlink() or manifest_path.read_bytes() != manifest_bytes:
                    raise CatalogError("existing artifact manifest differs at the same digest")
            else:
                _write_manifest(manifest_path, manifest_bytes)
            free_after = shutil.disk_usage(destination).free
            receipt = {
                "schema": ACQUISITION_WORKER_RESULT_SCHEMA,
                "operation_id": operation_id,
                "model_id": record.model_id,
                "model_digest": record.digest,
                "method": plan.method,
                "source": {
                    "repository": acquisition["repository"],
                    "revision": acquisition["revision"],
                },
                "artifact_manifest_digest": manifest.digest,
                "artifact_content_digest": manifest.content_digest,
                "content_uri": canonical_content_uri(
                    (
                        f"pvc://fs2-models/qwen3-8b-weights/models/{record.model_id}"
                        if acquisition["publication"]
                        == "atomic-content-addressed-provider-block-pvc"
                        else f"sfs://fs2-cache/mnt/fs2-serve-cache/models/{record.model_id}"
                    )
                    + f"/sha256/{manifest.content_digest}",
                    model_id=record.model_id,
                    content_digest=manifest.content_digest,
                    scheme=(
                        "pvc"
                        if acquisition["publication"]
                        == "atomic-content-addressed-provider-block-pvc"
                        else "sfs"
                    ),
                ),
                "prerequisite_ids": list(plan.required_prerequisite_ids),
                "storage": (
                    {
                        "mode": "provider-block-pvc",
                        "contract": acquisition["storage_contract"],
                        "pvc_namespace": "fs2-models",
                        "pvc_name": "qwen3-8b-weights",
                    }
                    if acquisition["publication"]
                    == "atomic-content-addressed-provider-block-pvc"
                    else {
                        "mode": "sfs-pvc",
                        "contract": "fs2-models/shared-cache-pvc",
                        "pvc_namespace": "fs2-models",
                        "pvc_name": "fs2-cache",
                    }
                ),
                "credential_source": "none-public-revision",
                "token_used": False,
                "publication": acquisition["publication"],
                "controller_owner": "fs2-serve-acquirer",
                "acquisition_plan_sha256": workload["acquisition_plan_sha256"],
                "helper_image": workload["helper_image"],
                "execution": {
                    "run_as_non_root": True,
                    "run_as_uid": run_as_uid,
                    "run_as_gid": run_as_gid,
                    "fs_group": ACQUISITION_FS_GROUP,
                    "supplemental_groups_policy": "Strict",
                    "seccomp_profile": "RuntimeDefault",
                    "job": workload["job"],
                    "pod": workload["pod"],
                },
                "filesystem_write_proof": filesystem_write_proof,
                "lock_path": str(lock_path),
                "capacity_bound_bytes": capacity_bound,
                "reserve_bytes": reserve_bytes,
                "free_bytes_before": free_before,
                "free_bytes_after": free_after,
                "outcome": outcome,
                "cleanup": {"temporary_path_absent": False},
            }
            shutil.rmtree(temporary)
            receipt["cleanup"]["temporary_path_absent"] = not temporary.exists()
            receipt["receipt_digest"] = hashlib.sha256(
                canonical_bytes(receipt)
            ).hexdigest()
            return receipt
        except Exception:
            if temporary.exists() and temporary.parent == destination:
                shutil.rmtree(temporary)
            raise
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def finalize_acquisition_receipt(
    record: ModelRecord,
    plan: AcquisitionPlan,
    worker_result: dict[str, Any],
    cleanup_observation: dict[str, Any],
) -> dict[str, Any]:
    """Create promotion evidence only after API-observed UID-fenced Job cleanup."""

    if worker_result.get("schema") != ACQUISITION_WORKER_RESULT_SCHEMA:
        raise CatalogError("acquisition finalizer requires an exact worker result")
    unsigned_worker = dict(worker_result)
    worker_digest = unsigned_worker.pop("receipt_digest", None)
    if (
        _strong_digest(worker_digest, "acquisition worker result digest")
        != hashlib.sha256(canonical_bytes(unsigned_worker)).hexdigest()
    ):
        raise CatalogError("acquisition worker result digest differs")
    if (
        worker_result.get("model_id") != record.model_id
        or worker_result.get("model_digest") != record.digest
        or worker_result.get("acquisition_plan_sha256")
        != hashlib.sha256(canonical_bytes(plan.to_dict())).hexdigest()
    ):
        raise CatalogError("acquisition worker result differs from its model plan")
    execution = worker_result.get("execution")
    if not isinstance(execution, dict):
        raise CatalogError("acquisition worker result lacks execution identity")
    job = execution.get("job")
    pod = execution.get("pod")
    if not isinstance(job, dict) or not isinstance(pod, dict):
        raise CatalogError("acquisition worker result lacks Job/Pod identity")
    expected_resources = [
        {key: job[key] for key in ("api_version", "kind", "namespace", "name", "uid")},
        {
            key: pod[key]
            for key in (
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "owner_job_uid",
            )
        },
    ]
    required_cleanup = {
        "completed_at",
        "observer_identity_sha256",
        "controller_identity_sha256",
        "api_server_observed",
        "expected_resources",
        "resources",
        "temporary_path_absent",
        "write_marker_absent",
        "foreign_uids_touched",
    }
    if not isinstance(cleanup_observation, dict) or set(cleanup_observation) != required_cleanup:
        raise CatalogError("acquisition cleanup observation has unexpected or missing fields")
    _strong_digest(
        cleanup_observation["observer_identity_sha256"],
        "acquisition cleanup observer identity",
    )
    _strong_digest(
        cleanup_observation["controller_identity_sha256"],
        "acquisition cleanup controller identity",
    )
    if (
        cleanup_observation["api_server_observed"] is not True
        or cleanup_observation["expected_resources"] != expected_resources
        or cleanup_observation["temporary_path_absent"] is not True
        or cleanup_observation["write_marker_absent"] is not True
        or cleanup_observation["foreign_uids_touched"] is not False
        or not isinstance(cleanup_observation["completed_at"], str)
        or not cleanup_observation["completed_at"].endswith("Z")
    ):
        raise CatalogError("acquisition cleanup did not close the exact Job/Pod subjects")
    resources = cleanup_observation["resources"]
    if not isinstance(resources, list) or len(resources) != 2:
        raise CatalogError("acquisition cleanup must observe exactly its Job and Pod")
    for expected, observed in zip(expected_resources, resources, strict=True):
        if not isinstance(observed, dict) or set(observed) != {
            *expected,
            "delete_precondition_uid",
            "final_state",
            "replacement_uid",
            "replacement_touched",
        }:
            raise CatalogError("acquisition cleanup resource fields differ")
        replacement_uid = observed["replacement_uid"]
        if (
            any(observed[key] != expected[key] for key in expected)
            or observed["delete_precondition_uid"] != expected["uid"]
            or observed["final_state"] != "absent"
            or observed["replacement_touched"] is not False
            or (
                replacement_uid is not None
                and (
                    not isinstance(replacement_uid, str)
                    or K8S_UID.fullmatch(replacement_uid) is None
                    or replacement_uid == expected["uid"]
                )
            )
        ):
            raise CatalogError("acquisition cleanup is not UID-fenced or replacement-safe")
    final = {
        key: value
        for key, value in worker_result.items()
        if key not in {"schema", "receipt_digest", "cleanup"}
    }
    final.update(
        {
            "schema": ACQUISITION_RECEIPT_SCHEMA,
            "worker_result_digest": worker_digest,
            "cleanup": cleanup_observation,
        }
    )
    final["receipt_digest"] = hashlib.sha256(canonical_bytes(final)).hexdigest()
    return final
