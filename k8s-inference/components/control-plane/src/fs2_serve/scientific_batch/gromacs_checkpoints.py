"""Coherent, incrementally published native GROMACS checkpoints.

Only this opt-in collector uses the journal. Files are immutable artifacts;
publishing the small manifest last is the durable commit boundary. A subsequent
attempt restores the latest committed generation of the same operation/shard.
Unfinished uploads are not generations. Old segment files are reused by digest.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx
from fs2_gromacs.contracts import relative_path
from fs2_gromacs.files import atomic_json, digest_file, media_type

from .gromacs_storage import GromacsCustomerStorage
from .models import StageInvocation

if TYPE_CHECKING:
    from .companion import WorkloadArtifactHttpClient

CHECKPOINT_MEDIA = "application/vnd.fs2.gromacs-checkpoint+json"
COLLECTOR = "gromacs-workflow-v1"
MAX_MANIFEST_BYTES = 16 * 1024**2


class GromacsCheckpointTransport:
    def __init__(self, client: WorkloadArtifactHttpClient, invocation: StageInvocation, workspace: Path) -> None:
        self.client, self.invocation, self.workspace = client, invocation, workspace
        self.data = workspace / "data"
        self.meta = workspace / ".fs2"
        self.generation = 0
        self.files: dict[str, dict[str, Any]] = {}
        self.operation = invocation.argv[invocation.argv.index("--operation-id") + 1]
        self.attempt = os.environ.get("FS2_ATTEMPT_ID", "local-companion")
        self.customer = GromacsCustomerStorage(client, workspace, self.operation, invocation.shard_id)

    def _state(self, value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        state = value.get("state", {})
        files = value.get("files", [])
        if (
            (state.get("schema"), state.get("operation_id"), state.get("job_id"))
            != (
                "fs2-serve.nebius.ai/gromacs-checkpoint/v1",
                self.operation,
                self.invocation.shard_id,
            )
            or type(state.get("generation")) is not int
            or state["generation"] < 1
        ):
            raise ValueError("checkpoint belongs to another workflow or shard")
        if not isinstance(files, list) or len(files) > 9998:
            raise ValueError("checkpoint file count exceeds the invocation")
        names = [relative_path(item["path"]) for item in files]
        if (
            len(set(names)) != len(names)
            or sum(item["size_bytes"] for item in files) > self.invocation.max_output_bytes
        ):
            raise ValueError("checkpoint has duplicate paths or exceeds the byte budget")
        return state, files

    def restore(self) -> None:
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
            if latest["size_bytes"] > MAX_MANIFEST_BYTES or latest["media_type"] != CHECKPOINT_MEDIA:
                raise ValueError("checkpoint manifest size/type is invalid")
            raw = self.client.download(
                UUID(latest["artifact_id"]),
                expected_digest="sha256:" + latest["sha256"],
                expected_size_bytes=latest["size_bytes"],
                expected_media_type=CHECKPOINT_MEDIA,
            )
            value = json.loads(raw)
            state, files = self._state(value)
            for item in files:
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
            atomic_json(self.meta / "gromacs-state.json", state)
            self.generation = state["generation"]
        atomic_json(self.meta / "restore-complete.json", {"status": "ready", "generation": self.generation})

    def publish_ready(self) -> None:
        path = self.meta / "checkpoint-ready.json"
        if not path.is_file():
            return
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("checkpoint publication marker exceeds its bound")
        value = json.loads(path.read_text())
        state, files = self._state(value)
        if state["generation"] <= self.generation:
            return
        published = []
        for item in files:
            source = self.data / item["path"]
            root = self.data.resolve(strict=True)
            if source.is_symlink() or root not in source.resolve(strict=True).parents or not source.is_file():
                raise ValueError("checkpoint file escaped its workspace")
            if source.stat().st_size != item["size_bytes"] or digest_file(source) != item["sha256"]:
                raise ValueError("checkpoint file changed after the engine stopped")
            prior = self.files.get(item["path"])
            if prior and (prior["sha256"], prior["size_bytes"]) == (item["sha256"], item["size_bytes"]):
                ref = prior["artifact"]
                uploaded_attempt = prior.get("uploaded_attempt")
            else:
                ref = self.client.upload_file(
                    identity=f"{self.invocation.produces}:{self.attempt}:checkpoint:{item['path']}",
                    path=source,
                    media_type=media_type(item["path"]),
                    compression=None,
                )
                uploaded_attempt = self.attempt
            published.append({**item, "artifact": ref, "uploaded_attempt": uploaded_attempt})
        customer_storage = self.customer.publish(state, files)
        manifest = {"state": state, "files": published, "customer_storage": customer_storage}
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("checkpoint manifest exceeds its bound")
        ref = self.client.upload(
            identity=f"{self.invocation.produces}:{self.attempt}:checkpoint:{state['generation']}",
            content=raw,
            media_type=CHECKPOINT_MEDIA,
            compression=None,
        )
        self.generation = state["generation"]
        self.files = {item["path"]: item for item in published}
        atomic_json(
            self.meta / "checkpoint-ack.json",
            {
                "status": "committed",
                "generation": self.generation,
                "artifact": ref,
                "customer_storage": customer_storage,
            },
        )

    def current_file_reference(self, path: Path) -> dict[str, Any] | None:
        """Reuse committed bytes in this attempt, never a failed attempt's result."""
        if self.data.resolve() not in path.resolve().parents:
            return None
        prior = self.files.get(str(path.relative_to(self.data)))
        if (
            prior is not None
            and prior.get("uploaded_attempt") == self.attempt
            and path.stat().st_size == prior["size_bytes"]
            and digest_file(path) == prior["sha256"]
        ):
            return cast(dict[str, Any], prior["artifact"])
        return None
