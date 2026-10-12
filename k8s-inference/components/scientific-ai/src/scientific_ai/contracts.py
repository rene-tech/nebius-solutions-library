"""Private application contracts, not a second public API or an IAM service."""

from __future__ import annotations

from typing import Annotated, Literal

from fs2_gromacs.contracts import normalize, relative_path
from jsonschema import ValidationError as NativeValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Image = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Owner(Contract):
    """From authenticated Scientific AI state, NEVER a request's tenant field."""

    tenant_id: Identifier
    principal_id: Identifier


class Attempt(Contract):
    """From Serverless's authenticated current-attempt binding."""

    owner: Owner
    root_operation_id: Identifier
    attempt_id: Identifier
    epoch: int = Field(strict=True, ge=1)
    cluster: Identifier
    gpu_class: Identifier | None
    image: Image
    attempt_role: Literal["run", "finalize"] = "run"
    gpus: int = Field(strict=True, ge=0, default=1)

    @model_validator(mode="after")
    def execution_shape(self) -> Attempt:
        if self.attempt_role == "finalize":
            if self.gpus != 0 or self.gpu_class is not None:
                raise ValueError("delivery finalization must allocate no GPU")
        elif self.gpus != 1 or self.gpu_class is None:
            raise ValueError("this native execution requires exactly one qualified GPU")
        return self


class StorageGrant(Contract):
    """Already authorized by Scientific AI and enforced by provider IAM.

    These checks prevent accidental grant mixing; they are not a substitute for
    provider authorization/revocation. No cloud credentials are stored here.
    """

    owner: Owner
    root_operation_id: Identifier
    grant_ref: Identifier

    def authorize(self, attempt: Attempt) -> None:
        if (self.owner, self.root_operation_id) != (attempt.owner, attempt.root_operation_id):
            raise PermissionError("storage grant does not belong to this execution")


class RecoveryPolicy(Contract):
    """Opt-in S2 policy; this application never schedules a replacement worker."""

    on_infrastructure_loss: Literal["retry", "fail", "recover"] = "retry"
    max_recoveries: int = Field(strict=True, ge=1, le=3, default=1)
    wait_s: int = Field(strict=True, ge=0, le=86400, default=300)
    deadline_s: int | None = Field(strict=True, ge=60, le=604800, default=None)

    def wire(self, checkpoint_ref: str | None) -> dict:
        values = {"on_infrastructure_loss": self.on_infrastructure_loss, "checkpoint_ref": checkpoint_ref}
        if self.on_infrastructure_loss == "recover":
            values.update(max_recoveries=self.max_recoveries, wait_s=self.wait_s, deadline_s=self.deadline_s)
        return values


class RuntimeProfile(Contract):
    """Operator-owned immutable revision, not caller-selected images or placement."""

    revision: Identifier
    checkpoint_compatibility_id: Identifier
    images: dict[Identifier, Image]
    allowed_clusters: tuple[Identifier, ...] = Field(min_length=1)
    gpu_preference: tuple[Identifier, ...] = ("rtx-pro-6000", "l40s")
    fallback_wait_s: int = Field(strict=True, ge=0, le=86400, default=60)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)

    @model_validator(mode="after")
    def qualified_choices(self) -> RuntimeProfile:
        if not self.gpu_preference or len(set(self.gpu_preference)) != len(self.gpu_preference):
            raise ValueError("GPU preference must contain distinct qualified classes")
        if set(self.gpu_preference) - self.images.keys():
            raise ValueError("each permitted GPU needs a digest-pinned image")
        return self

    def authorize(self, attempt: Attempt) -> None:
        if attempt.attempt_role == "finalize":
            if attempt.cluster not in self.allowed_clusters or attempt.image not in self.images.values():
                raise PermissionError("finalizer differs from its pinned runtime revision")
            return
        if (
            attempt.cluster not in self.allowed_clusters
            or attempt.gpu_class not in self.gpu_preference
            or self.images.get(attempt.gpu_class) != attempt.image
        ):
            raise PermissionError("attempt placement or image differs from its pinned runtime revision")


class FileRecord(Contract):
    path: str = Field(repr=False, min_length=1, max_length=512)
    sha256: Digest
    size_bytes: int = Field(strict=True, ge=0)

    @field_validator("path")
    @classmethod
    def contained(cls, value: str) -> str:
        return relative_path(value)


class GromacsRun(Contract):
    """Protected manifest. Native protocol and filenames stay out of S2 metadata."""

    schema_version: Literal[1] = 1
    job_id: Identifier
    input_bundle: FileRecord
    parameters: dict = Field(repr=False)

    @model_validator(mode="after")
    def native_contract(self) -> GromacsRun:
        try:
            normalized = normalize(self.parameters)
        except (ValueError, NativeValidationError):
            # jsonschema's default exception text can include the whole private
            # request. Return a schema error, not a payload in shared logs.
            raise ValueError("invalid GROMACS workflow; check the native request schema") from None
        if self.input_bundle.path != "input.tar.gz":
            raise ValueError("the pilot input is the existing gzip-tar bundle contract")
        if self.job_id not in {job["id"] for job in normalized["jobs"]}:
            raise ValueError("job_id must identify a native workflow shard")
        if "continuation_files" in normalized:
            raise ValueError(
                "the S2 pilot restores committed checkpoints, not caller-built continuation maps"
            )
        if normalized["output_destination"] != "customer-bucket":
            raise ValueError("the external-I/O pilot must not mirror into platform storage")
        return self


class Snapshot(Contract):
    schema_version: Literal[1] = 1
    kind: Literal["checkpoint", "result"]
    owner: Owner
    root_operation_id: Identifier
    attempt_id: Identifier
    epoch: int = Field(strict=True, ge=1)
    runtime_revision: Identifier
    compatibility_id: Identifier
    image: Image
    job_id: Identifier
    input_sha256: Digest
    state: dict = Field(repr=False)
    files: tuple[FileRecord, ...] = Field(max_length=32766)
    result: dict | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def unique_files(self) -> Snapshot:
        if len({file.path for file in self.files}) != len(self.files):
            raise ValueError("snapshot contains duplicate paths")
        if self.kind == "result" and (not self.result or self.result.get("status") != "succeeded"):
            raise ValueError("a successful result manifest requires successful scientific computation")
        return self
