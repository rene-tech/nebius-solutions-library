import gzip
import hashlib
import io
import tarfile
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from scientific_ai.contracts import Attempt, FileRecord, GromacsRun, Owner, RuntimeProfile, StorageGrant
from scientific_ai.gromacs import GromacsApplication
from scientific_ai.storage import Artifacts


@pytest.fixture(scope="session")
def pod_tokens():
    """Real RSA signatures, with no connection to a real issuer or credentials."""
    import json

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="test-only", use="sig", alg="RS256")

    def claims(pod="pod-1"):
        now = int(datetime.now(UTC).timestamp())
        return {
            "iss": "https://issuer.example/test-a",
            "aud": "https://testserver/internal/storage",
            "sub": "system:serviceaccount:tenant-system:identity-scientific-ai-storage",
            "iat": now - 1,
            "nbf": now - 1,
            "exp": now + 3600,
            "kubernetes.io": {
                "namespace": "tenant-system",
                "pod": {"name": pod, "uid": "uid-" + pod},
                "serviceaccount": {"name": "identity-scientific-ai-storage", "uid": "sa-uid"},
            },
        }

    def sign(value):
        return jwt.encode(value, key, algorithm="RS256", headers={"kid": "test-only"})

    return claims, sign, {"keys": [public]}


class MemoryObjects:
    """Only a test double; production has no permissive storage fallback."""

    def __init__(self):
        self.data, self.puts = {}, []
        self.fail_put = False
        self.corrupt_get = False

    def put(self, key, source):
        if self.fail_put:
            raise OSError("synthetic storage denial")
        self.data[key] = source.read()
        self.puts.append(key)

    def get(self, key):
        value = self.data[key]
        return io.BytesIO(value + b"corrupt" if self.corrupt_get else value)


class MemoryLifecycle:
    """Implements the future S2 boundary in tests, never shipped as a backend."""

    def __init__(self, attempt):
        self.current, self.head, self.done = attempt, None, None
        self.fail_ack = False
        self.checkpoints = []

    def require_current(self, attempt):
        if self.current != attempt:
            raise PermissionError("cancelled or replaced attempt")

    def checkpoint(self, attempt, reference, generation):
        self.require_current(attempt)
        self.head = reference
        self.checkpoints.append((attempt.attempt_id, generation, reference))

    def finalize(self, attempt, reference):
        self.require_current(attempt)
        if self.fail_ack:
            raise OSError("synthetic callback outage")
        self.done = reference


def bundle(files):
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def setup(tmp_path):
    owner = Owner(tenant_id="system", principal_id="qa")
    profile = RuntimeProfile(
        revision="gromacs-test-v1",
        checkpoint_compatibility_id="gromacs-test-compat-v1",
        images={
            "rtx-pro-6000": "example.invalid/gromacs@sha256:" + "a" * 64,
            "l40s": "example.invalid/gromacs@sha256:" + "b" * 64,
        },
        allowed_clusters=("test-a", "test-b"),
    )
    attempt = Attempt(
        owner=owner,
        root_operation_id="run-1",
        attempt_id="attempt-1",
        epoch=1,
        cluster="test-a",
        gpu_class="rtx-pro-6000",
        image=profile.images["rtx-pro-6000"],
    )
    grant = StorageGrant(owner=owner, root_operation_id="run-1", grant_ref="grant-1")
    objects = MemoryObjects()
    data = bundle({"run.tpr": b"synthetic-only"})
    record = FileRecord(path="input.tar.gz", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    objects.data["blobs/" + record.sha256] = data
    run = GromacsRun(
        job_id="replica",
        input_bundle=record,
        parameters={
            "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
            "jobs": [
                {"id": "replica", "steps": [{"id": "md", "command": "mdrun", "args": ["-s", "run.tpr"]}]}
            ],
        },
    )
    lifecycle = MemoryLifecycle(attempt)
    defaults = dict(
        run=run,
        attempt=attempt,
        grant=grant,
        profile=profile,
        lifecycle=lifecycle,
        artifacts=Artifacts(objects),
        workspace=tmp_path / "attempt1",
        gmx="not-executed",
    )
    return defaults, objects, lifecycle


def initialized_application(setup):
    defaults, objects, lifecycle = setup
    app = GromacsApplication(**defaults)
    app.prepare()
    app.worker.data.mkdir()
    (app.worker.data / "run.tpr").write_bytes(b"synthetic protocol")
    return app, objects, lifecycle


def successful_result(app):
    from fs2_gromacs.files import inventory

    return {
        "status": "succeeded",
        "recipe_sha256": app.worker.recipe,
        "operation_id": app.attempt.root_operation_id,
        "job_id": app.run_spec.job_id,
        "inventory_complete": True,
        "engine_id": app.attempt.image,
        "files": inventory(app.worker.data, max_bytes=100000),
    }
