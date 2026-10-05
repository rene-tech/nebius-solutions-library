"""Coherent, incrementally published native molecular-dynamics checkpoints.

Only this opt-in collector uses the journal. Files are immutable artifacts;
publishing the small manifest last is the durable commit boundary. A subsequent
attempt restores the latest committed generation of the same operation/shard.
Unfinished uploads are not generations. Old segment files are reused by digest.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx
from fs2_gromacs.contracts import relative_path
from fs2_gromacs.files import FileDigestCache, atomic_json, digest_file, media_type

from .gromacs_storage import GromacsCustomerStorage
from .models import StageInvocation
from .native_workflows import NativeWorkflow, workflow_for_collector

if TYPE_CHECKING:
    from .companion import WorkloadArtifactHttpClient

CHECKPOINT_MEDIA = "application/vnd.fs2.gromacs-checkpoint+json"
COLLECTOR = "gromacs-workflow-v1"
MAX_MANIFEST_BYTES = 32 * 1024**2


class GromacsCheckpointTransport:
    """Shared transport, retaining the original class name for GROMACS callers."""

    def __init__(
        self,
        client: WorkloadArtifactHttpClient,
        invocation: StageInvocation,
        workspace: Path,
        *,
        workflow: NativeWorkflow | None = None,
    ) -> None:
        selected = workflow or workflow_for_collector(COLLECTOR)
        if selected is None:
            raise ValueError("native checkpoint workflow is not registered")
        self.workflow: NativeWorkflow = selected
        self.client, self.invocation, self.workspace = client, invocation, workspace
        self.data = workspace / "data"
        self.meta = workspace / ".fs2"
        self.generation = 0
        self.files: dict[str, dict[str, Any]] = {}
        self.final_files: dict[str, dict[str, Any]] = {}
        self.final_references_prepared = False
        self.diagnostic_files: dict[str, dict[str, Any]] = {}
        self.file_digests = FileDigestCache()
        self.operation = invocation.argv[invocation.argv.index("--operation-id") + 1]
        self.attempt = os.environ.get("FS2_ATTEMPT_ID", "local-companion")
        self.customer = GromacsCustomerStorage(
            client, workspace, self.operation, invocation.shard_id, workflow=self.workflow
        )
        self.transfer_progress: dict[str, Any] = {}
        self.transfer_started = self.phase_started = 0.0
        self.progress_lock = threading.RLock()

    def _begin_progress(self, action: str, generation: int, total_files: int) -> None:
        self.transfer_started = self.phase_started = time.monotonic()
        self.transfer_progress = {
            "action": action,
            "generation": generation,
            "total_files": total_files,
            "completed_files": 0,
            "phase": "validation",
            "phase_seconds": {},
        }
        self._progress("validation")

    def _progress(self, phase: str, completed_files: int | None = None) -> None:
        """Sanitized local timing evidence: no paths, payloads, handles or keys."""
        with self.progress_lock:
            now = time.monotonic()
            durations = self.transfer_progress["phase_seconds"]
            previous = self.transfer_progress["phase"]
            durations[previous] = durations.get(previous, 0.0) + now - self.phase_started
            self.phase_started = now
            self.transfer_progress.update(phase=phase, elapsed_seconds=now - self.transfer_started)
            if completed_files is not None:
                self.transfer_progress["completed_files"] = completed_files
            atomic_json(self.meta / "transfer-progress.json", self.transfer_progress)
            if phase in {"committed", "restored", "finalized"}:
                print(json.dumps({"event": "native_checkpoint_transfer", **self.transfer_progress}), flush=True)

    def _cohort_progress(self) -> Callable[[str | None], None]:
        """Separate summed cohort work from the non-overlapping wall timeline."""
        previous: str | None = None
        started = time.monotonic()

        def report(phase: str | None) -> None:
            nonlocal previous, started
            with self.progress_lock:
                now = time.monotonic()
                durations = self.transfer_progress.setdefault("cohort_phase_seconds", {})
                active = self.transfer_progress.setdefault("active_cohort_phases", {})
                if previous is not None:
                    durations[previous] = durations.get(previous, 0.0) + now - started
                    active[previous] -= 1
                    if active[previous] == 0:
                        del active[previous]
                if phase is not None:
                    active[phase] = active.get(phase, 0) + 1
                previous, started = phase, now
                self.transfer_progress["elapsed_seconds"] = now - self.transfer_started
                atomic_json(self.meta / "transfer-progress.json", self.transfer_progress)

        return report

    @staticmethod
    def _native_groups(items: list[dict[str, Any]]) -> Iterator[tuple[list[dict[str, Any]], int]]:
        group: list[dict[str, Any]] = []
        size = 0
        for item in items:
            if group and (len(group) == 64 or size + item["size_bytes"] > 1024**3):
                yield group, size
                group, size = [], 0
            group.append(item)
            size += item["size_bytes"]
        if group:
            yield group, size

    def _upload_native_groups(
        self, items: list[dict[str, Any]],
    ) -> Iterator[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        """At most two cohorts/128 handles and one GiB in-flight file bytes.

        One oversized file runs alone. HTTP PUTs retain the client's shared
        eight-stream bound. Results are consumed in input order; failures stop
        new work and drain already-running transfers before the caller can
        publish a customer manifest, checkpoint acknowledgement or final result.
        """
        if not items:
            return
        self.transfer_progress["max_parallel_cohorts"] = 2
        self._progress("platform-pipeline")

        def upload(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
            report = self._cohort_progress()
            try:
                return self.client.upload_files(
                    identity=f"{self.invocation.produces}:{self.attempt}:native-file",
                    paths=tuple(self.data / item["path"] for item in group),
                    media_type="application/octet-stream",
                    compression=None,
                    on_phase=report,
                )
            finally:
                report(None)

        groups = self._native_groups(items)
        waiting = next(groups, None)
        pending: deque[tuple[Future[list[dict[str, Any]]], list[dict[str, Any]], int]] = deque()
        in_flight_bytes, completed = 0, 0
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="native-artifact-cohort")
        try:
            while waiting is not None or pending:
                # Do not enqueue more work after a known failure, even if an
                # earlier cohort is the next one consumed in input order.
                for future, _, _ in pending:
                    if future.done():
                        future.result()
                while waiting is not None and len(pending) < 2:
                    group, size = waiting
                    if pending and in_flight_bytes + size > 1024**3:
                        break
                    pending.append((executor.submit(upload, group), group, size))
                    in_flight_bytes += size
                    waiting = next(groups, None)
                future, group, size = pending.popleft()
                refs = future.result()
                if len(refs) != len(group) or any(
                    (ref.get("sha256"), ref.get("size_bytes")) != (item["sha256"], item["size_bytes"])
                    for item, ref in zip(group, refs, strict=True)
                ):
                    raise ValueError("native upload differs from the stopped checkpoint bytes")
                in_flight_bytes -= size
                yield group, refs
                completed += len(group)
                self._progress("platform-pipeline", completed)
        finally:
            for future, _, _ in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def _state(self, value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        state = value.get("state", {})
        files = value.get("files", [])
        if (
            (state.get("schema"), state.get("operation_id"), state.get("job_id"))
            != (
                self.workflow.checkpoint_schema,
                self.operation,
                self.invocation.shard_id,
            )
            or type(state.get("generation")) is not int
            or state["generation"] < 1
        ):
            raise ValueError("checkpoint belongs to another workflow or shard")
        maximum_files = 32766 if self.workflow.model_id in {"gromacs", "gromacs-mpi"} else 9998
        if not isinstance(files, list) or len(files) > maximum_files:
            raise ValueError("checkpoint file count exceeds the invocation")
        names = [relative_path(item["path"]) for item in files]
        if (
            len(set(names)) != len(names)
            or sum(item["size_bytes"] for item in files) > self.invocation.max_output_bytes
        ):
            raise ValueError("checkpoint has duplicate paths or exceeds the byte budget")
        return state, files

    def restore(self) -> None:
        try:
            self._restore()
        except Exception:
            self._notify_failure("restore")
            raise

    def _notify_failure(self, phase: str) -> None:
        # The engine must not hold a GPU for its full handoff timeout after
        # the companion exits. Never put provider messages/credentials here.
        self.meta.mkdir(parents=True, exist_ok=True)
        atomic_json(self.meta / "transport-error.json", {"status": "failed", "phase": phase})

    def _restore(self) -> None:
        self.meta.mkdir(parents=True, exist_ok=True)
        self.customer.initialize()

        def read(response: httpx.Response) -> dict[str, Any]:
            response.read()
            return cast(dict[str, Any], response.json())

        latest = self.client._download_get(
            self.client.base_url + "/internal/scientific-workloads/checkpoints/latest",
            headers=self.client.headers,
            read=read,
        ).get("checkpoint")
        if latest is not None:
            if latest["size_bytes"] > MAX_MANIFEST_BYTES or latest["media_type"] != self.workflow.checkpoint_media_type:
                raise ValueError("checkpoint manifest size/type is invalid")
            raw = self.client.download(
                UUID(latest["artifact_id"]),
                expected_digest="sha256:" + latest["sha256"],
                expected_size_bytes=latest["size_bytes"],
                expected_media_type=self.workflow.checkpoint_media_type,
            )
            value = json.loads(raw)
            state, files = self._state(value)
            self._begin_progress("restore", state["generation"], len(files))

            def restore_file(item: dict[str, Any]) -> None:
                ref = item["artifact"]
                if (ref["sha256"], ref["size_bytes"], ref["media_type"]) != (
                    item["sha256"],
                    item["size_bytes"],
                    media_type(item["path"]),
                ):
                    raise ValueError("checkpoint file metadata differs from its artifact")
                self.client.download_file(
                    UUID(ref["artifact_id"]),
                    destination=self.data / item["path"],
                    expected_digest="sha256:" + ref["sha256"],
                    expected_size_bytes=ref["size_bytes"],
                    expected_media_type=ref["media_type"],
                )
                self.files[item["path"]] = item

            if self.workflow.engine == "gromacs":
                with ThreadPoolExecutor(max_workers=8) as executor:
                    for offset in range(0, len(files), 128):
                        group = files[offset : offset + 128]
                        self._progress("restore-authorize", offset)
                        self.client.prepare_downloads(
                            tuple(dict.fromkeys(UUID(item["artifact"]["artifact_id"]) for item in group))
                        )
                        self._progress("restore-transfer", offset)
                        tuple(executor.map(restore_file, group))
            else:
                for item in files:
                    restore_file(item)
            atomic_json(self.meta / self.workflow.state_filename, state)
            self.generation = state["generation"]
            self._progress("restored", len(files))
        atomic_json(self.meta / "restore-complete.json", {"status": "ready", "generation": self.generation})

    def publish_ready(self) -> None:
        try:
            self._publish_ready()
        except Exception:
            self._notify_failure("publish")
            raise

    def _publish_ready(self) -> None:
        path = self.meta / "checkpoint-ready.json"
        if not path.is_file():
            return
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("checkpoint publication marker exceeds its bound")
        value = json.loads(path.read_text())
        state, files = self._state(value)
        if state["generation"] <= self.generation:
            return
        self._begin_progress("publish", state["generation"], len(files))
        published = []
        known = {item["sha256"]: item for item in self.files.values()}
        missing: dict[str, dict[str, Any]] = {}
        for item in files:
            source = self.data / item["path"]
            root = self.data.resolve(strict=True)
            if source.is_symlink() or root not in source.resolve(strict=True).parents or not source.is_file():
                raise ValueError("checkpoint file escaped its workspace")
            if source.stat().st_size != item["size_bytes"] or self.file_digests.digest(source) != item["sha256"]:
                raise ValueError("checkpoint file changed after the engine stopped")
            # Native GROMACS routinely emits byte-identical aliases, e.g.
            # md.gro and md.part0001.gro, or shared lambda-window topologies.
            # The platform reserves content addresses, not filenames.
            prior = known.get(item["sha256"])
            if not prior or (prior["sha256"], prior["size_bytes"]) != (item["sha256"], item["size_bytes"]):
                missing.setdefault(item["sha256"], item)

        def upload(item: dict[str, Any]) -> dict[str, Any]:
            return {
                **item,
                "artifact": self.client.upload_file(
                    identity=f"{self.invocation.produces}:{self.attempt}:native-file",
                    path=self.data / item["path"],
                    media_type=media_type(item["path"]),
                    compression=None,
                ),
                "uploaded_attempt": self.attempt,
            }

        pending = list(missing.values())
        if self.workflow.model_id in {"gromacs", "gromacs-mpi"}:
            for group, refs in self._upload_native_groups(pending):
                for item, ref in zip(group, refs, strict=True):
                    known[item["sha256"]] = {**item, "artifact": ref, "uploaded_attempt": self.attempt}
        else:
            for item in pending:
                entry = upload(item)
                known[entry["sha256"]] = entry
        for item in files:
            prior = known[item["sha256"]]
            entry = {**item, "artifact": prior["artifact"], "uploaded_attempt": prior.get("uploaded_attempt")}
            published.append(entry)
        self._progress("customer-export", len(files))
        customer_storage = self.customer.publish(state, files)
        self._progress("manifest")
        manifest = {"state": state, "files": published, "customer_storage": customer_storage}
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("checkpoint manifest exceeds its bound")
        ref = self.client.upload(
            identity=f"{self.invocation.produces}:{self.attempt}:checkpoint:{state['generation']}",
            content=raw,
            media_type=self.workflow.checkpoint_media_type,
            compression=None,
        )
        self.generation = state["generation"]
        self.files = {item["path"]: item for item in published}
        self._progress("acknowledgement")
        atomic_json(
            self.meta / "checkpoint-ack.json",
            {
                "status": "committed",
                "generation": self.generation,
                "artifact": ref,
                "customer_storage": customer_storage,
            },
        )
        self._progress("committed")

    def diagnostic_file_reference(self, path: Path) -> dict[str, Any] | None:
        """Retain failed native bytes without declaring recoverable progress.

        A closed failure log may already be an immutable checkpoint artifact.
        Reuse that artifact only in this attempt, preserving its MIME type.
        New/changed bytes are uploaded under the native content identity, never
        committed as a new checkpoint generation or certified as science.
        """
        if self.data.resolve() not in path.resolve().parents:
            return None
        digest, size = digest_file(path), path.stat().st_size
        if digest in self.diagnostic_files:
            return self.diagnostic_files[digest]
        prior = next(
            (
                item
                for item in self.files.values()
                if item["sha256"] == digest
                and item["size_bytes"] == size
                and item.get("uploaded_attempt") == self.attempt
            ),
            None,
        )
        if prior is not None:
            ref = cast(dict[str, Any], prior["artifact"])
        else:
            ref = self.client.upload_file(
                identity=f"{self.invocation.produces}:{self.attempt}:native-file",
                path=path,
                media_type=media_type(str(path)),
                compression=None,
            )
        self.diagnostic_files[digest] = ref
        return ref

    def _prepare_final_references(self) -> None:
        """Re-home recovered bytes into this successful attempt in bounded batches.

        Stage-result ownership still requires the current attempt. In particular,
        this must not turn a twenty-thousand-file preemption recovery into twenty
        thousand sequential upload lifecycles at the final publication boundary.
        """
        self._begin_progress("finalize", self.generation, len(self.files))
        pending: dict[str, dict[str, Any]] = {}
        root = self.data.resolve(strict=True)
        for item in self.files.values():
            path = self.data / item["path"]
            if (
                path.is_symlink()
                or root not in path.resolve(strict=True).parents
                or not path.is_file()
                or (path.stat().st_size, self.file_digests.digest(path)) != (item["size_bytes"], item["sha256"])
            ):
                raise ValueError("final native file differs from its committed checkpoint")
            if item.get("uploaded_attempt") == self.attempt:
                self.final_files[item["sha256"]] = item["artifact"]
            else:
                pending.setdefault(item["sha256"], item)
        remaining = [item for digest, item in pending.items() if digest not in self.final_files]
        for group, refs in self._upload_native_groups(remaining):
            for item, ref in zip(group, refs, strict=True):
                self.final_files[item["sha256"]] = ref
        self.final_references_prepared = True
        self._progress("finalized", len(self.files))

    def final_file_reference(self, path: Path) -> dict[str, Any] | None:
        """Publish native final bytes once per digest in the current attempt.

        A recovered checkpoint can reference prior attempts. Final stage results
        must belong to this attempt, including all byte-identical filename aliases.
        Non-native files (the result JSON) follow the collector's ordinary path.
        """
        if self.data.resolve() not in path.resolve().parents:
            return None
        prior = self.files.get(str(path.relative_to(self.data)))
        if prior is None or (path.stat().st_size, self.file_digests.digest(path)) != (
            prior["size_bytes"],
            prior["sha256"],
        ):
            raise ValueError("final native file differs from its committed checkpoint")
        digest = prior["sha256"]
        if digest in self.final_files:
            return self.final_files[digest]
        if (
            prior.get("uploaded_attempt") != self.attempt
            and self.workflow.model_id in {"gromacs", "gromacs-mpi"}
            and not self.final_references_prepared
        ):
            self._prepare_final_references()
            return self.final_files[digest]
        if prior.get("uploaded_attempt") == self.attempt:
            ref = cast(dict[str, Any], prior["artifact"])
        else:
            ref = self.client.upload_file(
                identity=f"{self.invocation.produces}:{self.attempt}:native-file",
                path=path,
                media_type=media_type(str(path)),
                compression=None,
            )
        self.final_files[digest] = ref
        return ref


# New engines use the domain-neutral name; existing imports remain compatible.
NativeCheckpointTransport = GromacsCheckpointTransport
