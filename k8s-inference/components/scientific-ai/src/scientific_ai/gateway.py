"""Thin Scientific AI compatibility service over S2 and customer object storage.

No Kubernetes client, scheduler, worker queue, in-memory operation registry or
second user database. S2 authenticates keys and owns all execution lifecycle.
The bucket holds artifact manifests and the UUID-to-S2 compatibility mapping.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import boto3
import httpx
import jwt
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fs2_gromacs.contracts import normalize, request_schema
from pydantic import Field, RootModel, SecretStr, model_validator

from .broker import https_origin
from .contracts import Contract, FileRecord, GromacsRun, Identifier, Owner, RuntimeProfile
from .gateway_store import GatewayStore, canonical, identifier, uuid
from .runner import PrivateRun
from .serverless import ServerlessClient, ServerlessError, _idempotency, invoke_payload
from .workload_identity import PodVerifier, WorkloadIdentity

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "EXPORT_FAILED", "PREEMPTED"}
RUN_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"


class TenantStorage(Contract):
    endpoint: str
    region: str
    bucket: str
    prefix: str
    access_key: SecretStr
    secret_key: SecretStr
    # Non-admin S2 service key, used only to authorize an active job's object URL.
    execution_key: SecretStr
    workload_identity_ref: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class Settings(Contract):
    serverless_url: str
    public_url: str
    model_id: str = "scientific-gromacs-pilot"
    profile_ref: str = "confidential-job-v1"
    runtime: RuntimeProfile
    signing_key: SecretStr
    tenants: dict[str, TenantStorage]
    workload_identity: WorkloadIdentity


class ArtifactPointer(Contract):
    artifact_id: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0, le=50 * 1024**3)
    media_type: str = "application/octet-stream"
    compression: str = "none"


class WorkflowParameters(RootModel[dict]):
    @model_validator(mode="after")
    def validate_native(self):
        try:
            normalize(self.root)
        except Exception:
            raise ValueError("invalid native GROMACS workflow") from None
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return request_schema()


class Submit(Contract):
    schema_: str = Field(alias="schema", default=RUN_SCHEMA, pattern="^" + re.escape(RUN_SCHEMA) + "$")
    operation: str = Field(default="run-workflow", pattern=r"^run-workflow$")
    service_class: str = "customer-batch"
    input_manifest: ArtifactPointer
    parameters: WorkflowParameters
    client_context: dict = Field(default_factory=dict)


class Resume(Contract):
    """Only implemented continuation options; never silently ignore legacy fields."""

    gpu_class: Identifier | None = None


class Gateway:
    def __init__(self, settings: Settings, *, http_factory=httpx.Client, storage_factory=None):
        self.settings, self.http_factory = settings, http_factory
        self.pod_verifier = PodVerifier(settings.workload_identity)
        https_origin(settings.serverless_url)
        https_origin(settings.public_url)
        if len(settings.signing_key.get_secret_value()) < 32:
            raise ValueError("the private job-grant signing key must have at least 32 characters")
        self.clients = {}
        for tenant, config in settings.tenants.items():
            https_origin(config.endpoint)
            self.clients[tenant] = (
                storage_factory(tenant)
                if storage_factory
                else boto3.client(
                    "s3",
                    region_name=config.region,
                    endpoint_url=config.endpoint,
                    aws_access_key_id=config.access_key.get_secret_value(),
                    aws_secret_access_key=config.secret_key.get_secret_value(),
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "path"},
                        retries={"mode": "standard", "max_attempts": 4},
                        request_checksum_calculation="when_required",
                        response_checksum_validation="when_required",
                    ),
                )
            )

    @contextmanager
    def client(self, authorization: str):
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise HTTPException(401, "an inference bearer key is required")
        with self.http_factory(
            base_url=self.settings.serverless_url,
            headers={"Authorization": authorization},
            timeout=60,
            follow_redirects=False,
        ) as http:
            client = ServerlessClient(http)
            info = client._request("GET", "/v1/keys/me")
            if info.get("tenant") not in self.settings.tenants:
                raise HTTPException(403, "no customer storage binding is configured for this tenant")
            allowed = info.get("models") or []
            if allowed and self.settings.model_id not in allowed and "all-proxy-models" not in allowed:
                raise HTTPException(403, "this key is not authorized for GROMACS")
            owner = Owner(tenant_id=info["tenant"], principal_id=info["id"])
            yield client, self.store(owner), info

    def store(self, owner: Owner):
        config = self.settings.tenants[owner.tenant_id]
        return GatewayStore(
            self.clients[owner.tenant_id], bucket=config.bucket, prefix=config.prefix, owner=owner
        )

    def model(self):
        return {
            "model_id": "gromacs",
            "display_name": "GROMACS",
            "execution_mode": "scientific-batch",
            "tool_name": "submit_gromacs_workflow",
            "operations": ["run-workflow"],
            "runtime_revision": self.settings.runtime.revision,
            "gpu_preference": self.settings.runtime.gpu_preference,
            "qualification": "isolated-pilot",
            "request_schema": request_schema(),
            "checkpoint_type": "native-gromacs",
            "gpu_snapshot": False,
            "capabilities": {
                "jobs_per_operation": 1,
                "gpus_per_job": 1,
                "native_checkpoint_resume": True,
                "automatic_cross_gpu_recovery": False,
                "resume_options": Resume.model_json_schema(),
                "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v2",
            },
        }

    def reserve(self, store: GatewayStore, body: dict):
        digest, size = body.get("sha256"), body.get("size_bytes")
        FileRecord(path="input", sha256=digest, size_bytes=size)
        if body.get("model_id", "gromacs") != "gromacs" or size > 50 * 1024**3:
            raise HTTPException(422, "unsupported App or upload size")
        aid = identifier(store.owner, "artifact", digest)
        record = store.save(
            "artifacts",
            aid,
            {
                "artifact_id": aid,
                "sha256": digest,
                "size_bytes": size,
                "media_type": body.get("media_type", "application/octet-stream"),
                "compression": body.get("compression", "none"),
                "object_key": store.prefix + "uploads/" + digest,
            },
            create=True,
        )
        if (record["sha256"], record["size_bytes"]) != (digest, size):
            raise HTTPException(409, "artifact identity conflicts with the existing reservation")
        return {
            "upload_id": aid,
            "operation_id": aid,
            "artifact": self.pointer(record),
            "upload_url": self.settings.public_url + f"/v1/scientific-artifacts/uploads/{aid}/content",
            "direct_upload_url": store.url("put_object", record["object_key"]),
            "direct_upload_expires_in": 60,
        }

    @staticmethod
    def pointer(record):
        return {k: record[k] for k in ("artifact_id", "sha256", "size_bytes", "media_type", "compression")}

    def content(self, store, pointer: ArtifactPointer, *, limit: int):
        record = store.get("artifacts", pointer.artifact_id)
        if (
            any(record[k] != getattr(pointer, k) for k in ("sha256", "size_bytes"))
            or pointer.size_bytes > limit
        ):
            raise HTTPException(422, "artifact reference differs from its immutable reservation")
        response = store.client.get_object(Bucket=store.bucket, Key=record["object_key"])
        with response["Body"] as source:
            data = source.read(limit + 1)
        if len(data) != pointer.size_bytes or hashlib.sha256(data).hexdigest() != pointer.sha256:
            raise HTTPException(422, "artifact failed checksum verification")
        return data

    def finalize_upload(self, store, aid):
        record = store.get("artifacts", aid)
        response = store.client.get_object(Bucket=store.bucket, Key=record["object_key"])
        digest, size = hashlib.sha256(), 0
        with response["Body"] as source:
            while data := source.read(4 * 1024**2):
                size += len(data)
                if size > record["size_bytes"]:
                    raise HTTPException(422, "uploaded artifact exceeds its declared size")
                digest.update(data)
        if (digest.hexdigest(), size) != (record["sha256"], record["size_bytes"]):
            raise HTTPException(422, "uploaded artifact failed checksum verification")
        return {"artifact": self.pointer(record), "state": "finalized"}

    def submit(self, client, store, body: Submit, key: str):
        _idempotency(key)
        op = identifier(store.owner, "operation", key)
        request_hash = hashlib.sha256(canonical(body.model_dump(by_alias=True))).hexdigest()
        # The native scientific bundle contract stays intact. Neither the input
        # filenames nor the scientific protocol enter ordinary S2 Job metadata.
        manifest = json.loads(self.content(store, body.input_manifest, limit=1024**2))
        entries = manifest.get("artifacts", manifest.get("entries", []))
        inputs = [entry for entry in entries if entry.get("name", entry.get("role")) == "gromacs-inputs"]
        if manifest.get("schema") != MANIFEST_SCHEMA or len(inputs) != 1:
            raise HTTPException(422, "input manifest requires exactly one gromacs-inputs bundle")
        entry = inputs[0].get("artifact", inputs[0].get("ref", inputs[0]))
        pointer = ArtifactPointer.model_validate(
            {k: entry[k] for k in ArtifactPointer.model_fields if k in entry}
        )
        uploaded = store.get("artifacts", pointer.artifact_id)
        if (uploaded["sha256"], uploaded["size_bytes"]) != (pointer.sha256, pointer.size_bytes):
            raise HTTPException(422, "input bundle differs from its reserved identity")
        jobs = body.parameters.root.get("jobs", [])
        if len(jobs) != 1:
            raise HTTPException(422, "this first single-GPU migration pilot accepts one job per operation")
        run = GromacsRun(
            job_id=jobs[0]["id"],
            parameters=body.parameters.root,
            input_bundle=FileRecord(
                path="input.tar.gz", sha256=pointer.sha256, size_bytes=pointer.size_bytes
            ),
        )
        record = store.save(
            "operations",
            op,
            {
                "operation_id": op,
                "root_id": op,
                "request_sha256": request_hash,
                "runtime": self.settings.runtime.model_dump(mode="json"),
                "input_manifest": body.input_manifest.model_dump(),
                "s2_id": None,
                "latest_s2_id": None,
            },
            create=True,
        )
        if record["request_sha256"] != request_hash:
            raise HTTPException(409, "Idempotency-Key already identifies a different request")
        if record.get("s2_id"):
            return self.status(client, store, op)
        profile = RuntimeProfile.model_validate(record["runtime"])
        objects = store.objects(op)
        # S3's copy helper uses multipart for large inputs. It never reads the
        # customer's trajectory through API memory or another tenant's prefix.
        store.client.copy(
            {"Bucket": store.bucket, "Key": uploaded["object_key"]},
            store.bucket,
            objects.prefix + "blobs/" + pointer.sha256,
        )
        grant = jwt.encode(
            {"iss": "scientific-ai", "aud": "job-storage", "owner": store.owner.model_dump(), "op": op},
            self.settings.signing_key.get_secret_value(),
            algorithm="HS256",
        )
        binding = PrivateRun(
            owner=store.owner,
            run=run,
            profile=profile,
            broker_url=self.settings.public_url,
            storage_token=grant,
        )
        private_ref = "sai-" + op
        client.put_binding(private_ref, {"run.json": binding.model_dump_json()})
        response = client.submit(
            self.settings.model_id,
            invoke_payload(
                profile,
                profile_ref=self.settings.profile_ref,
                identity_ref=self.settings.tenants[store.owner.tenant_id].workload_identity_ref,
                private_input_ref=private_ref,
            ),
            idempotency_key="sai-" + op,
        )
        store.save("operations", op, {**record, "s2_id": response["id"], "latest_s2_id": response["id"]})
        return self.status(client, store, op)

    def status(self, client, store, op):
        record = store.get("operations", op)
        if not record.get("s2_id"):
            return {
                "operation_id": op,
                "state": "admission-unknown",
                "status": "ADMISSION_PENDING",
                "retry_same_idempotency_key": True,
            }
        value = client.status(record["s2_id"])
        result = {
            **value,
            "operation_id": op,
            "id": op,
            "serverless_operation_id": value["id"],
            "model_id": "gromacs",
            "state": value["status"].lower(),
            "batch_id": op,
        }
        result["operation"] = {
            "operation_id": op,
            "status": value["status"].lower(),
            "result_available": value["status"] == "SUCCEEDED" and bool(value.get("result_ref")),
        }
        result["batch"] = {
            "batch_id": op,
            "status": value["status"].lower(),
            "stages": [
                {
                    "stage_id": "gromacs",
                    "status": value["status"].lower(),
                    "attempts": value.get("attempts", []),
                }
            ],
        }
        return result

    def result(self, client, store, op):
        operation = self.status(client, store, op)
        if operation["status"] != "SUCCEEDED" or not operation.get("result_ref"):
            raise HTTPException(409, "the scientific result is not durably complete")
        record = store.get("operations", op)
        snapshot = store.artifacts(record["root_id"]).load(operation["result_ref"])
        if snapshot.owner != store.owner or snapshot.root_operation_id != operation["root_operation_id"]:
            raise PermissionError("result belongs to another owner or execution")
        outputs = []
        for file in snapshot.files:
            aid = identifier(store.owner, "output", record["root_id"] + ":" + file.sha256)
            output = store.save(
                "artifacts",
                aid,
                {
                    "artifact_id": aid,
                    "sha256": file.sha256,
                    "size_bytes": file.size_bytes,
                    "media_type": "application/octet-stream",
                    "compression": "none",
                    "object_key": store.objects(record["root_id"]).prefix + "blobs/" + file.sha256,
                },
                create=True,
            )
            outputs.append(
                {
                    "name": "file-" + str(len(outputs) + 1),
                    "semantic_type": "gromacs-output/v1",
                    "artifact": self.pointer(output),
                }
            )
        manifest = {"schema": MANIFEST_SCHEMA, "manifest_id": op, "entries": outputs}
        encoded = canonical(manifest)
        digest = hashlib.sha256(encoded).hexdigest()
        objects = store.objects(record["root_id"])
        objects.put("blobs/" + digest, io.BytesIO(encoded))
        manifest_id = identifier(store.owner, "output-manifest", record["root_id"] + ":" + digest)
        published = store.save(
            "artifacts",
            manifest_id,
            {
                "artifact_id": manifest_id,
                "sha256": digest,
                "size_bytes": len(encoded),
                "media_type": "application/vnd.fs2.scientific-manifest+json",
                "compression": "none",
                "object_key": objects.prefix + "blobs/" + digest,
            },
            create=True,
        )
        root = store.get("operations", record["root_id"])
        return {
            # v1 requires legacy-controller-specific admission UIDs which S2
            # does not publish. Keep useful names/pointers, version the changed
            # execution record instead of fabricating those fields.
            "schema": "fs2-serve.nebius.ai/scientific-run-result/v2",
            "operation_id": op,
            "model_id": "gromacs",
            "status": "succeeded",
            "terminal_status": "succeeded",
            "submitted_at": operation.get("created_at"),
            "completed_at": operation.get("ended_at"),
            "input_manifest": root["input_manifest"],
            "semantic_validation": {
                "validator_id": "gromacs-native-workflow",
                "status": "passed",
                "receipt_digest": hashlib.sha256(canonical(snapshot.result)).hexdigest(),
            },
            "native_result": snapshot.result,
            "output_manifest": self.pointer(published),
            "attempts": operation.get("attempts", []),
            "execution": {k: operation.get(k) for k in ("attempt_id", "epoch", "cluster", "image_digest")},
        }

    def resume(self, client, store, op, key: str, *, gpu_class: str | None = None):
        _idempotency(key)
        Resume(gpu_class=gpu_class)
        original = store.get("operations", op)
        root = store.get("operations", original["root_id"])
        profile = RuntimeProfile.model_validate(root["runtime"])
        if gpu_class is not None and gpu_class not in profile.gpu_preference:
            raise HTTPException(422, "GPU class is not qualified for the original runtime")
        resumed = identifier(store.owner, "resume", op + ":" + key)
        intent = hashlib.sha256(canonical([op, gpu_class])).hexdigest()
        previous = store.save(
            "operations",
            resumed,
            {
                "operation_id": resumed,
                "root_id": original["root_id"],
                "request_sha256": intent,
                "s2_id": None,
            },
            create=True,
        )
        if previous["request_sha256"] != intent:
            raise HTTPException(409, "resume key already identifies different continuation options")
        if not previous.get("s2_id"):
            response = client.resume(original["s2_id"], idempotency_key="sai-" + resumed, gpu_class=gpu_class)
            store.save("operations", resumed, {**previous, "s2_id": response["id"]})
            root = store.get("operations", original["root_id"])
            store.save("operations", original["root_id"], {**root, "latest_s2_id": response["id"]})
        return self.status(client, store, resumed)

    def sign(self, token: str, body: dict, identity_token: str):
        try:
            claims = jwt.decode(
                token,
                self.settings.signing_key.get_secret_value(),
                algorithms=["HS256"],
                issuer="scientific-ai",
                audience="job-storage",
            )
            owner = Owner.model_validate(claims["owner"])
            store = self.store(owner)
            operation = store.get("operations", claims["op"])
        except (jwt.PyJWTError, KeyError, ValueError):
            raise HTTPException(403, "invalid job storage grant") from None
        config = self.settings.tenants[owner.tenant_id]
        with self.http_factory(
            base_url=self.settings.serverless_url,
            headers={"Authorization": "Bearer " + config.execution_key.get_secret_value()},
            timeout=30,
            follow_redirects=False,
        ) as http:
            current = ServerlessClient(http).status(operation["latest_s2_id"])
        if current.get("attempt_id") != body.get("attempt_id") or not current.get("attempt_id"):
            raise HTTPException(403, "job storage grant is not active for this execution attempt")
        self.pod_verifier.require_current(
            identity_token,
            current=current,
            namespace=config.namespace,
            identity_ref=config.workload_identity_ref,
        )
        if current["status"] == "QUEUED":
            # The uploader's authenticated start record can precede Kubernetes' Running observation.
            # A verified current pod retries immutable I/O; storage stays denied until the phase catches up.
            raise ConnectionError("the current attempt's startup observation is pending")
        if current["status"] not in {"RUNNING", "FINALIZING"}:
            raise HTTPException(403, "job storage grant is not active for this execution attempt")
        relative = body.get("key", "")
        if not re.fullmatch(r"(?:blobs|manifests)/[a-f0-9]{64}", relative):
            raise HTTPException(403, "object is outside this job's artifact namespace")
        key = store.objects(claims["op"]).prefix + relative
        action = body.get("action")
        if action in ("get", "put"):
            return {"url": store.url(action + "_object", key), "expires_in": 60}
        params = {"Bucket": store.bucket, "Key": key}
        if action == "multipart-create":
            return {"upload_id": store.client.create_multipart_upload(**params)["UploadId"]}
        upload_id = body.get("upload_id")
        if not isinstance(upload_id, str) or not upload_id or len(upload_id) > 2048:
            raise HTTPException(422, "a valid multipart upload ID is required")
        if action == "multipart-part":
            part = body.get("part_number")
            if type(part) is not int or not 1 <= part <= 10000:
                raise HTTPException(422, "part_number must be between 1 and 10000")
            return {"url": store.url("upload_part", key, UploadId=upload_id, PartNumber=part)}
        if action == "multipart-complete":
            store.client.complete_multipart_upload(
                **params, UploadId=upload_id, MultipartUpload={"Parts": body["parts"]}
            )
        elif action == "multipart-abort":
            store.client.abort_multipart_upload(**params, UploadId=upload_id)
        else:
            raise HTTPException(422, "unknown storage action")
        return {"acknowledged": True}


def create_app(settings: Settings | None = None, *, gateway: Gateway | None = None):
    if gateway is None:
        settings = settings or Settings.model_validate_json(
            Path(os.environ["SCIENTIFIC_AI_CONFIG"]).read_bytes()
        )
        gateway = Gateway(settings)
    app = FastAPI(title="Nebius Scientific AI", version="0.1.0", docs_url=None, redoc_url=None)

    def authorization(request):
        return request.headers.get("Authorization", "")

    @app.exception_handler(ServerlessError)
    async def upstream_error(request, error):
        return JSONResponse(
            {
                "error": {
                    "code": "SERVERLESS_REQUEST_FAILED",
                    "retryable": error.retryable,
                    "admission": "unknown" if error.retryable else "not-confirmed",
                }
            },
            status_code=error.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "retryable": False,
                    # No rejected values or exception context: these may be private.
                    "fields": [{"path": list(e["loc"]), "code": e["type"]} for e in error.errors()],
                }
            },
            status_code=422,
        )

    @app.exception_handler(ConnectionError)
    async def temporarily_unavailable(request, error):
        return JSONResponse({"error": {"code": "DEPENDENCY_UNAVAILABLE", "retryable": True}}, status_code=503)

    @app.exception_handler(ValueError)
    async def invalid_value(request, error):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "retryable": False}}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"error": {"code": "NOT_FOUND", "retryable": False}}, status_code=404)

    @app.exception_handler(PermissionError)
    async def denied(request, error):
        return JSONResponse({"error": {"code": "ACCESS_DENIED", "retryable": False}}, status_code=403)

    @app.get("/health")
    def health():
        return {"status": "ok", "scope": "isolated-serverless2-pilot"}

    @app.get("/v1/me")
    def me(request: Request):
        with gateway.client(authorization(request)) as (_, store, info):
            return {
                "tenant_id": store.owner.tenant_id,
                "principal_id": store.owner.principal_id,
                "models": ["gromacs"],
                "role": info["role"],
            }

    @app.get("/v1/scientific-models")
    def models(request: Request):
        with gateway.client(authorization(request)):
            return {"models": [gateway.model()]}

    @app.get("/v1/models/gromacs/schema")
    def schema(request: Request):
        with gateway.client(authorization(request)):
            return gateway.model()

    @app.post("/v1/scientific-artifacts/uploads")
    def reserve(request: Request, body: dict):
        with gateway.client(authorization(request)) as (_, store, _):
            return gateway.reserve(store, body)

    @app.put("/v1/scientific-artifacts/uploads/{aid}/content")
    async def upload(request: Request, aid: str):
        with gateway.client(authorization(request)) as (_, store, _):
            record = store.get("artifacts", aid)
            size, digest = 0, hashlib.sha256()
            with tempfile.TemporaryFile() as temporary:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > record["size_bytes"]:
                        raise HTTPException(413, "upload exceeds its reservation")
                    temporary.write(chunk)
                    digest.update(chunk)
                if (digest.hexdigest(), size) != (record["sha256"], record["size_bytes"]):
                    raise HTTPException(422, "upload failed checksum verification")
                temporary.seek(0)
                store.client.upload_fileobj(temporary, store.bucket, record["object_key"])
            return {"upload_id": aid, "size_bytes": size}

    @app.post("/v1/scientific-artifacts/uploads/{aid}:finalize")
    def finalize_upload(request: Request, aid: str):
        with gateway.client(authorization(request)) as (_, store, _):
            return gateway.finalize_upload(store, aid)

    @app.post("/v1/models/gromacs:submit", status_code=202)
    def submit(request: Request, body: Submit):
        with gateway.client(authorization(request)) as (client, store, _):
            result = gateway.submit(client, store, body, request.headers.get("Idempotency-Key", ""))
            return JSONResponse(
                result,
                status_code=202,
                headers={
                    "x-fs2-operation-id": result["operation_id"],
                    "Location": "/v1/operations/" + result["operation_id"],
                },
            )

    @app.get("/v1/operations/{op}")
    def status(request: Request, op: str):
        with gateway.client(authorization(request)) as (client, store, _):
            return gateway.status(client, store, uuid(op))

    @app.get("/v1/operations/{op}/checkpoints")
    def checkpoints(request: Request, op: str):
        with gateway.client(authorization(request)) as (client, store, _):
            value = gateway.status(client, store, uuid(op))
            return {
                "operation_id": op,
                "checkpoint_ref": value.get("checkpoint_ref"),
                "generation": value.get("checkpoint_generation"),
                "attempt_id": value.get("attempt_id"),
            }

    @app.get("/v1/operations/{op}/result")
    def result(request: Request, op: str):
        with gateway.client(authorization(request)) as (client, store, _):
            return gateway.result(client, store, uuid(op))

    @app.delete("/v1/operations/{op}")
    @app.post("/v1/operations/{op}:cancel")
    def cancel(request: Request, op: str):
        with gateway.client(authorization(request)) as (client, store, _):
            operation = store.get("operations", uuid(op))
            client.cancel(operation["s2_id"], idempotency_key="cancel-" + op)
            return gateway.status(client, store, op)

    @app.post("/v1/operations/{op}:resume", status_code=202)
    def resume(request: Request, op: str, body: Resume):
        with gateway.client(authorization(request)) as (client, store, _):
            return gateway.resume(
                client,
                store,
                uuid(op),
                request.headers.get("Idempotency-Key", ""),
                gpu_class=body.gpu_class,
            )

    @app.get("/v1/artifacts/{aid}/content")
    @app.get("/v1/scientific-artifacts/{aid}/content")
    def download(request: Request, aid: str):
        with gateway.client(authorization(request)) as (_, store, _):
            artifact = store.get("artifacts", aid)
            body = store.client.get_object(Bucket=store.bucket, Key=artifact["object_key"])["Body"]

            def chunks():
                try:
                    while block := body.read(1024**2):
                        yield block
                finally:
                    body.close()

            return StreamingResponse(
                chunks(),
                media_type=artifact["media_type"],
                headers={
                    "Content-Length": str(artifact["size_bytes"]),
                    "x-fs2-artifact-sha256": artifact["sha256"],
                },
            )

    @app.get("/v1/artifacts/{aid}/download")
    def download_handle(request: Request, aid: str):
        with gateway.client(authorization(request)) as (_, store, _):
            artifact = store.get("artifacts", aid)
            return {
                "artifact": gateway.pointer(artifact),
                "url": store.url("get_object", artifact["object_key"]),
                "expires_in": 60,
            }

    @app.post("/internal/storage")
    def storage(request: Request, body: dict):
        value = authorization(request)
        if not value.startswith("Bearer "):
            raise HTTPException(401, "a job storage grant is required")
        return gateway.sign(value[7:], body, request.headers.get("X-Scientific-Workload-Token", ""))

    # Imported here so native workers do not initialize an MCP server.
    from .mcp_server import mount_mcp

    mount_mcp(app, gateway)
    return app
