"""The S2 jobs 0.1.14 file bridge; no Kubernetes credential in the model.

The uploader authenticates and fences every pointer in S2's durable database.
Its acknowledgement supplies the actual attempt identity, not caller fields.
Finalization publishes the completed result as a recovery checkpoint first; S2
still owns the terminal success decision after main exits and uploader records it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

from fs2_gromacs.files import atomic_json
from pydantic import TypeAdapter

from .contracts import Attempt, Digest, Owner, RuntimeProfile


class FileLifecycle:
    def __init__(self, work: Path, *, timeout: float = 120, poll_seconds: float = 0.5):
        self.work, self.timeout, self.poll_seconds = work, timeout, poll_seconds
        self.generation = 0

    def _record(self) -> dict:
        # curl replaces this file while writing; incomplete JSON is not an ack.
        try:
            value = json.loads((self.work / "attempts/.ack.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _wait(self, predicate):
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            record = self._record()
            if "detail" in record:
                # Never include upstream error bodies or private inputs in logs.
                raise PermissionError("Serverless refused this attempt's publication")
            if predicate(record):
                return record
            time.sleep(self.poll_seconds)
        raise TimeoutError("Serverless did not acknowledge the attempt before the deadline")

    def discover(self, *, pod_name: str, owner: Owner, profile: RuntimeProfile) -> Attempt:
        record = self._wait(lambda r: r.get("pod") == pod_name and r.get("epoch") is not None)
        if record.get("tenant") != owner.tenant_id:
            raise PermissionError("the private binding and execution tenant differ")
        attempt = Attempt(
            owner=owner,
            root_operation_id=record["root"],
            attempt_id=pod_name,
            epoch=record["epoch"],
            cluster=record["region"],
            gpu_class=record.get("gpu_class") or None,
            image=record["image"],
            attempt_role=record.get("role", record.get("attempt_role", "run")),
            gpus=record.get("gpus", 1),
        )
        profile.authorize(attempt)
        return attempt

    @staticmethod
    def _matches(record: dict, attempt: Attempt) -> bool:
        return (record.get("pod"), record.get("root"), record.get("epoch")) == (
            attempt.attempt_id,
            attempt.root_operation_id,
            attempt.epoch,
        )

    def require_current(self, attempt: Attempt) -> None:
        # This is a local consistency check, NOT the publication fence. An old
        # worker can upload orphan immutable objects; only S2 can promote a head.
        record = self._wait(lambda r: bool(r))
        if not self._matches(record, attempt) or record.get("status") != "started":
            raise PermissionError("attempt acknowledgement is absent, replaced, or terminal")

    def checkpoint_reference(self, attempt: Attempt) -> str | None:
        self.require_current(attempt)
        reference = self._record().get("checkpoint_ref")
        if reference is not None:
            TypeAdapter(Digest).validate_python(reference)
        return reference

    def _publish(self, attempt: Attempt, reference: str, generation: int, *, result: bool) -> None:
        TypeAdapter(Digest).validate_python(reference)
        self.require_current(attempt)
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            # The nonce re-notifies the 0.1.14 uploader after a lost POST/ack. It
            # never changes the reference/generation or admits another run.
            atomic_json(
                self.work / "CHECKPOINT.json",
                {
                    "checkpoint_ref": reference,
                    "generation": generation,
                    "publication_nonce": uuid4().hex,
                },
            )
            retry_at = min(end, time.monotonic() + 25)
            while time.monotonic() < retry_at:
                record = self._record()
                if "detail" in record:
                    raise PermissionError("Serverless refused this attempt's publication")
                if self._matches(record, attempt) and (
                    record.get("checkpoint_ref") == reference
                    and record.get("checkpoint_generation") == generation
                    and (not result or record.get("result_ref") == reference)
                ):
                    self.generation = generation
                    return
                time.sleep(self.poll_seconds)
        raise TimeoutError("checkpoint was uploaded but its durable acknowledgement is pending")

    def checkpoint(self, attempt: Attempt, reference: str, generation: int) -> None:
        self._publish(attempt, reference, generation, result=False)

    def finalize(self, attempt: Attempt, reference: str) -> None:
        TypeAdapter(Digest).validate_python(reference)
        # No checkpoint_ref here: the uploader reads CHECKPOINT.json and keeps
        # its generation. A result manifest is itself a portable recovery point,
        # so recovery after export/exit loss will not run mdrun a second time.
        atomic_json(self.work / "RESULT.json", {"result_ref": reference})
        self._publish(attempt, reference, self.generation + 1, result=True)
