from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import threading
import unittest
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

MODULE_PATH = Path(__file__).resolve().parents[1] / "run_acceptance.py"
SPEC = importlib.util.spec_from_file_location(
    "fs2_scientific_fleet_acceptance", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

MODEL_ID = "test-model"
VARIANT_ID = "test-model-h100"
MODEL_REVISION = "a" * 40
RUNTIME_IMAGE = "sha256:" + "b" * 64
RUNTIME_RECIPE = "c" * 64
WORKLOAD_RECIPE = "d" * 64
MODEL_ARTIFACTS = "e" * 64
EXECUTION_IDENTITY = "f" * 64
SEMANTIC_RECEIPT = "1" * 64
NOW = "2026-09-04T12:00:00Z"
LATER = "2026-09-04T12:00:03Z"
OPERATION_ID = "00000000-0000-0000-0000-000000000900"
BATCH_ID = "00000000-0000-0000-0000-000000000901"
WORKLOAD_ID = "00000000-0000-0000-0000-000000000902"
K8S_ROOT = Path(__file__).resolve().parents[3]
PROTEINA_FRAGMENT = Path(
    "models/cancer-immunotherapy/runtime-images/proteina-complexa/activation/fragment.json"
)


def canonical(value: object, *, newline: bool = False) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"))
        + ("\n" if newline else "")
    ).encode()


def pointer(data: bytes, media_type: str, artifact_id: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": media_type,
        "compression": "none",
    }


@dataclass
class ApiState:
    mode: str = "success"
    model_id: str = MODEL_ID
    variant_id: str = VARIANT_ID
    operation: str = "design"
    model_revision: str = MODEL_REVISION
    runtime_image: str = RUNTIME_IMAGE
    runtime_recipe: str = RUNTIME_RECIPE
    workload_recipe: str = WORKLOAD_RECIPE
    model_artifacts: str = MODEL_ARTIFACTS
    execution_identity: str = EXECUTION_IDENTITY
    reservations: dict[str, dict[str, Any]] = field(default_factory=dict)
    uploaded: list[dict[str, Any]] = field(default_factory=list)
    submitted: dict[str, Any] | None = None
    authorized_requests: int = 0

    def status(self, terminal: bool) -> dict[str, Any]:
        state = "succeeded" if terminal else "queued"
        return {
            "operation": {
                "id": OPERATION_ID,
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "operation": self.operation,
                "status": state,
                "semantic_outcome": "passed" if terminal else None,
                "accepted_at": NOW,
                "available_at": NOW,
                "activation_started_at": NOW if terminal else None,
                "ready_at": LATER if terminal else None,
                "started_at": NOW if terminal else None,
                "completed_at": LATER if terminal else None,
                "cold_start_seconds": 3.0 if terminal else None,
                "runtime": {
                    "pod_uid": "pod-uid",
                    "node_uid": "node-uid",
                    "gpu_uuids": ["GPU-test"],
                    "gpu_count": 1,
                    "preemptible": False,
                },
            },
            "batch": {
                "batch_id": BATCH_ID,
                "workload_id": WORKLOAD_ID,
                "model_id": self.model_id,
                "variant_id": self.variant_id,
                "input_artifact_id": None
                if self.submitted is None
                else self.submitted["input_manifest"]["artifact_id"],
                "status": state,
                "result_published": terminal,
                "scheduling_snapshot_digest": "2" * 64,
                "stages": [
                    {
                        "stage_id": "design",
                        "status": state,
                        "failure_code": None,
                        "attempts": []
                        if not terminal
                        else [
                            {
                                "attempt_id": "00000000-0000-0000-0000-000000000903",
                                "shard_id": "main",
                                "attempt_number": 1,
                                "workload_kind": "Job",
                                "workload_name": "test-model-design",
                                "workload_uid": "kueue-workload-uid",
                                "workload_namespace": "fs2-models",
                                "route_namespace": "fs2-models",
                                "outcome": "succeeded",
                                "last_phase": "teardown",
                                "resource_released": True,
                                "failure_kind": None,
                                "failure_code": None,
                                "scheduling_admission": {
                                    "resolved_pool_id": "h100-reserved-8x",
                                    "admitted_resource_flavor": "inference-h100",
                                    "accelerator_resource_name": "nvidia.com/gpu",
                                    "accelerator_count": 1,
                                    "admitted_at": NOW,
                                },
                            }
                        ],
                    }
                ],
            },
        }

    def result(self) -> dict[str, Any]:
        assert self.submitted is not None
        semantic_status = "failed" if self.mode == "semantic" else "passed"
        return {
            "schema": MODULE.RESULT_SCHEMA,
            "operation_id": OPERATION_ID,
            "batch_id": BATCH_ID,
            "workload_id": WORKLOAD_ID,
            "terminal_status": "succeeded",
            "submitted_at": NOW,
            "completed_at": LATER,
            "execution_identity": {
                "model_id": self.model_id,
                "variant_id": self.variant_id,
                "model_revision": self.model_revision,
                "runtime_image_digest": self.runtime_image,
                "runtime_recipe_sha256": self.runtime_recipe,
                "workload_recipe_sha256": self.workload_recipe,
                "model_artifact_manifest_digest": self.model_artifacts,
                "execution_identity_sha256": self.execution_identity,
            },
            "scheduling_snapshot": {
                "policy_revision": "policy-1",
                "captured_at": NOW,
                "service_class": "customer-batch",
                "tenant_queue": "scientific",
                "model_lane": self.model_id,
                "stages": [
                    {
                        "stage_id": "design",
                        "resource_class": "gpu",
                        "resolved_cluster_queue": "inference",
                        "resolved_local_queue": "scientific",
                        "workload_priority_class": "customer-batch",
                        "workload_priority_value": 500,
                        "resolved_pool_preference": ["h100-reserved-8x"],
                        "accelerator_resource_name": "nvidia.com/gpu",
                        "accelerator_count": 1,
                        "max_queue_seconds": 600,
                        "max_execution_seconds": 3600,
                        "checkpoint_mode": "restart",
                        "preemption_mode": "restartable",
                    }
                ],
            },
            "input_manifest": self.submitted["input_manifest"],
            "output_manifest": {
                "artifact_id": "00000000-0000-0000-0000-000000000904",
                "sha256": "3" * 64,
                "size_bytes": 123,
                "media_type": MODULE.MANIFEST_MEDIA_TYPE,
                "compression": "none",
            },
            "attempts": [
                {
                    "attempt_id": "00000000-0000-0000-0000-000000000903",
                    "stage_id": "design",
                    "shard_id": "main",
                    "attempt_number": 1,
                    "status": "succeeded",
                    "started_at": NOW,
                    "completed_at": LATER,
                    "scheduling_admission": {
                        "resolved_pool_id": "h100-reserved-8x",
                        "admitted_resource_flavor": "inference-h100",
                        "accelerator_resource_name": "nvidia.com/gpu",
                        "accelerator_count": 1,
                        "admitted_at": NOW,
                    },
                    "kueue_workload_uid": "kueue-workload-uid",
                    "k8s_job_uid": "job-uid",
                    "pod_uids": ["pod-uid"],
                    "node_uids": ["node-uid"],
                    "gpu_uuids": ["GPU-test"],
                    "checkpoint_input": None,
                    "checkpoint_output": None,
                }
            ],
            "semantic_validation": {
                "validator_id": "test-validator",
                "status": semantic_status,
                "receipt_digest": SEMANTIC_RECEIPT,
            },
            "error": None,
        }


class FakeApi:
    def __init__(self, mode: str = "success", **identity: str) -> None:
        self.state = ApiState(mode=mode, **identity)
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("content-length", "0")))

            def _json(self) -> dict[str, Any]:
                return json.loads(self._body())

            def _send(
                self, status: int, value: object, headers: dict[str, str] | None = None
            ) -> None:
                body = canonical(value)
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                for key, item in (headers or {}).items():
                    self.send_header(key, item)
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if self.headers.get("authorization") != "Bearer test-token":
                    self._send(401, {"error": {"type": "unauthorized"}})
                    return False
                state.authorized_requests += 1
                return True

            def do_POST(self) -> None:
                if not self._authorized():
                    return
                if self.path == "/v1/scientific-artifacts/uploads":
                    request = self._json()
                    ordinal = len(state.reservations) + 1
                    upload_id = str(UUID(int=ordinal))
                    operation_id = str(UUID(int=100 + ordinal))
                    state.reservations[upload_id] = {
                        **request,
                        "operation_id": operation_id,
                    }
                    self._send(
                        201,
                        {
                            "operation_id": operation_id,
                            "upload_id": upload_id,
                            "content_path": (
                                f"/v1/scientific-artifacts/uploads/{upload_id}/content?operation_id={operation_id}"
                            ),
                            "max_content_bytes": 1024 * 1024,
                            "handle": {
                                "method": "PUT",
                                "url": "https://object.invalid/input?X-Amz-Signature=never-record-this",
                                "headers": {
                                    "x-amz-security-token": "never-record-this"
                                },
                                "write_once": True,
                                "expires_at": LATER,
                            },
                        },
                    )
                    return
                if self.path.endswith(":finalize"):
                    upload_id = self.path.rsplit("/", 1)[1].removesuffix(":finalize")
                    request = self._json()
                    reservation = state.reservations[upload_id]
                    if request["operation_id"] != reservation["operation_id"]:
                        self._send(404, {"error": {"type": "not_found"}})
                        return
                    artifact_id = str(UUID(int=200 + int(UUID(upload_id))))
                    self._send(
                        200,
                        {
                            "artifact_id": artifact_id,
                            "sha256": reservation["sha256"],
                            "size_bytes": reservation["size_bytes"],
                            "media_type": reservation["media_type"],
                            "compression": reservation.get("compression", "none"),
                        },
                    )
                    return
                if self.path == f"/v1/models/{state.model_id}:submit":
                    if state.mode == "route":
                        self._body()
                        self._send(
                            503, {"error": {"type": "scientific_route_unavailable"}}
                        )
                        return
                    state.submitted = self._json()
                    self._send(
                        202,
                        state.status(False),
                        {
                            "x-fs2-operation-id": OPERATION_ID,
                            "location": f"/v1/operations/{OPERATION_ID}",
                        },
                    )
                    return
                self._send(404, {"error": {"type": "not_found"}})

            def do_PUT(self) -> None:
                if not self._authorized():
                    return
                parsed = urlsplit(self.path)
                upload_id = parsed.path.split("/")[-2]
                reservation = state.reservations[upload_id]
                if parse_qs(parsed.query) != {
                    "operation_id": [reservation["operation_id"]]
                }:
                    self._send(404, {"error": {"type": "not_found"}})
                    return
                body = self._body()
                state.uploaded.append(
                    {"upload_id": upload_id, "body": body, **reservation}
                )
                receipt = {
                    "operation_id": reservation["operation_id"],
                    "upload_id": upload_id,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size_bytes": len(body),
                    "media_type": self.headers["content-type"],
                    "finalized": False,
                }
                if state.mode == "digest":
                    receipt["sha256"] = "0" * 64
                elif state.mode == "size":
                    receipt["size_bytes"] = len(body) + 1
                elif state.mode == "media":
                    receipt["media_type"] = "application/octet-stream"
                self._send(200, receipt)

            def do_GET(self) -> None:
                if not self._authorized():
                    return
                if self.path == f"/v1/operations/{OPERATION_ID}":
                    self._send(200, state.status(state.mode != "timeout"))
                    return
                if self.path == f"/v1/operations/{OPERATION_ID}/result":
                    self._send(200, state.result())
                    return
                self._send(404, {"error": {"type": "not_found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeApi:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


def activation_fragment(
    request_path: str, supporting_inputs: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "profile_projection": {
            "profile": {
                "execution_identity": {
                    "model_revision": MODEL_REVISION,
                    "runtime_image_digest": RUNTIME_IMAGE,
                    "runtime_recipe_sha256": RUNTIME_RECIPE,
                    "workload_recipe_sha256": WORKLOAD_RECIPE,
                    "artifact_manifest_digest": MODEL_ARTIFACTS,
                    "execution_identity_sha256": EXECUTION_IDENTITY,
                }
            }
        },
        "execution_projection": {"variant_id": VARIANT_ID},
        "public_fixtures": {
            "request": request_path,
            "supporting_inputs": supporting_inputs,
        },
    }


class AcceptanceRunnerTest(unittest.TestCase):
    def _direct_fixture(self, root: Path) -> Path:
        data = b">target\nMKT\n"
        (root / "inputs").mkdir()
        (root / "inputs/target.fasta").write_bytes(data)
        request = {
            "schema": MODULE.REQUEST_SCHEMA,
            "operation": "design",
            "service_class": "customer-batch",
            "input_manifest": pointer(data, "text/x-fasta", "target-placeholder"),
            "parameters": {},
        }
        (root / "request.json").write_bytes(canonical(request, newline=True))
        fragment = activation_fragment(
            "request.json",
            [
                {
                    "role": "request-input-manifest",
                    "path": "inputs/target.fasta",
                    "encoding": "raw",
                }
            ],
        )
        path = root / "fragment.json"
        path.write_bytes(canonical(fragment, newline=True))
        return path

    def _manifest_fixture(self, root: Path) -> Path:
        first = b">target-a\nMKT\n"
        second = b">target-b\nAAA\n"
        (root / "inputs").mkdir()
        (root / "inputs/a.fasta").write_bytes(first)
        (root / "inputs/b.fasta").write_bytes(second)
        manifest = {
            "schema": MODULE.MANIFEST_SCHEMA,
            "manifest_id": "two-inputs",
            "entries": [
                {
                    "name": "target_a",
                    "semantic_type": "protein-sequence-fasta/v1",
                    "artifact": pointer(first, "text/x-fasta", "target-a-placeholder"),
                },
                {
                    "name": "target_b",
                    "semantic_type": "protein-sequence-fasta/v1",
                    "artifact": pointer(second, "text/x-fasta", "target-b-placeholder"),
                },
            ],
        }
        manifest_bytes = canonical(manifest, newline=True)
        (root / "inputs/manifest.json").write_bytes(manifest_bytes)
        request = {
            "schema": MODULE.REQUEST_SCHEMA,
            "operation": "design",
            "service_class": "customer-batch",
            "input_manifest": pointer(
                manifest_bytes, MODULE.MANIFEST_MEDIA_TYPE, "manifest-placeholder"
            ),
            "parameters": {},
        }
        (root / "request.json").write_bytes(canonical(request, newline=True))
        fragment = activation_fragment(
            "request.json",
            [
                {
                    "role": "request-input-manifest",
                    "path": "inputs/manifest.json",
                    "encoding": "canonical-json-newline",
                },
                {
                    "role": "manifest-artifact",
                    "name": "target_a",
                    "path": "inputs/a.fasta",
                    "encoding": "raw",
                },
                {
                    "role": "manifest-artifact",
                    "name": "target_b",
                    "path": "inputs/b.fasta",
                    "encoding": "raw",
                },
            ],
        )
        path = root / "fragment.json"
        path.write_bytes(canonical(fragment, newline=True))
        return path

    @staticmethod
    def _config(root: Path, fragment: Path, api: FakeApi) -> Any:
        return MODULE.RunConfig(
            endpoint=api.endpoint,
            repository_root=root,
            activation_fragment=fragment,
            receipt_path=root / "receipt.json",
            run_id="offline-test-run",
            timeout_seconds=0.05,
            poll_seconds=0.005,
            request_timeout_seconds=2,
        )

    def test_direct_artifact_success_writes_redacted_receipt(self) -> None:
        with TemporaryDirectory() as directory, FakeApi() as api:
            root = Path(directory)
            fragment = self._direct_fixture(root)
            config = self._config(root, fragment, api)
            receipt = MODULE.run_acceptance(
                config,
                MODULE.PublicApiClient(api.endpoint, "test-token", timeout_seconds=2),
            )

            self.assertEqual(len(api.state.uploaded), 1)
            self.assertEqual(receipt["terminal_state"]["semantic_validation"], "passed")
            self.assertEqual(receipt["cold_start"]["cold_start_seconds"], 3.0)
            self.assertEqual(receipt["queue"]["tenant_queue"], "scientific")
            self.assertEqual(
                receipt["execution_identity"]["execution_identity_sha256"],
                EXECUTION_IDENTITY,
            )
            body = config.receipt_path.read_text()
            self.assertNotIn("never-record-this", body)
            self.assertNotIn("test-token", body)
            self.assertNotIn("principal_id", body)
            self.assertGreater(api.state.authorized_requests, 0)

    def test_manifest_with_multiple_logical_artifacts_is_rebuilt_and_uploaded(
        self,
    ) -> None:
        with TemporaryDirectory() as directory, FakeApi() as api:
            root = Path(directory)
            fragment = self._manifest_fixture(root)
            config = self._config(root, fragment, api)
            receipt = MODULE.run_acceptance(
                config,
                MODULE.PublicApiClient(api.endpoint, "test-token", timeout_seconds=2),
            )

            self.assertEqual(len(api.state.uploaded), 3)
            rebuilt = json.loads(api.state.uploaded[-1]["body"])
            self.assertEqual(
                [entry["name"] for entry in rebuilt["entries"]],
                ["target_a", "target_b"],
            )
            for entry in rebuilt["entries"]:
                UUID(entry["artifact"]["artifact_id"])
            roles = [item["role"] for item in receipt["artifact_digests"]["uploads"]]
            self.assertEqual(
                roles,
                ["manifest-artifact", "manifest-artifact", "request-input-manifest"],
            )
            self.assertEqual(
                api.state.submitted["input_manifest"]["artifact_id"],
                rebuilt_manifest_id(api.state),
            )

    def test_proteina_public_fixture_runs_the_complete_manifest_upload_flow(
        self,
    ) -> None:
        fragment_value = json.loads(
            (K8S_ROOT / PROTEINA_FRAGMENT).read_text(encoding="utf-8")
        )
        expected = fragment_value["profile_projection"]["profile"]["execution_identity"]
        variant_id = fragment_value["execution_projection"]["variant_id"]
        with TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "proteina-receipt.json"
            config = MODULE.RunConfig(
                endpoint="http://127.0.0.1",
                repository_root=K8S_ROOT,
                activation_fragment=PROTEINA_FRAGMENT,
                receipt_path=receipt_path,
                run_id="proteina-public-fixture-offline",
                timeout_seconds=0.05,
                poll_seconds=0.005,
                request_timeout_seconds=2,
            )
            model_id, request, declarations, _fragment = MODULE._activation(config)
            self.assertEqual(model_id, "proteina-complexa")
            self.assertEqual(request["operation"], "design-binders")
            self.assertEqual(
                [item.role for item in declarations],
                ["request-input-manifest", "manifest-artifact"],
            )
            target_bundle = declarations[1]
            self.assertEqual(target_bundle.name, "target-bundle")
            self.assertEqual(
                hashlib.sha256(target_bundle.data).hexdigest(),
                "a9582cbc7583eaf9280cc7395f6a4af8b11efda803fe791e89c76fedc871d72a",
            )
            self.assertEqual(len(target_bundle.data), 81948)
            self.assertEqual(
                target_bundle.data,
                MODULE.deterministic_tar_gzip(
                    target_bundle.path.read_bytes(),
                    "assets/target_data/bindcraft_targets/PD-L1.pdb",
                ),
            )
            with tarfile.open(
                fileobj=io.BytesIO(target_bundle.data), mode="r:gz"
            ) as archive:
                members = archive.getmembers()
                self.assertEqual(len(members), 1)
                member = members[0]
                self.assertEqual(
                    member.name, "assets/target_data/bindcraft_targets/PD-L1.pdb"
                )
                self.assertEqual(
                    (member.mode, member.uid, member.gid, member.mtime),
                    (0o444, 0, 0, 0),
                )
                source = archive.extractfile(member)
                self.assertIsNotNone(source)
                assert source is not None
                self.assertEqual(source.read(), target_bundle.path.read_bytes())

            with FakeApi(
                model_id=model_id,
                variant_id=variant_id,
                operation=str(request["operation"]),
                model_revision=expected["model_revision"],
                runtime_image=expected["runtime_image_digest"],
                runtime_recipe=expected["runtime_recipe_sha256"],
                workload_recipe=expected["workload_recipe_sha256"],
            ) as api:
                config = MODULE.RunConfig(
                    endpoint=api.endpoint,
                    repository_root=K8S_ROOT,
                    activation_fragment=PROTEINA_FRAGMENT,
                    receipt_path=receipt_path,
                    run_id="proteina-public-fixture-offline",
                    timeout_seconds=0.05,
                    poll_seconds=0.005,
                    request_timeout_seconds=2,
                )
                receipt = MODULE.run_acceptance(
                    config,
                    MODULE.PublicApiClient(
                        api.endpoint, "test-token", timeout_seconds=2
                    ),
                )

                self.assertEqual(len(api.state.uploaded), 2)
                self.assertEqual(api.state.uploaded[0]["body"], target_bundle.data)
                rebuilt = json.loads(api.state.uploaded[1]["body"])
                UUID(rebuilt["entries"][0]["artifact"]["artifact_id"])
                self.assertEqual(rebuilt["entries"][0]["name"], "target-bundle")
                self.assertEqual(
                    api.state.submitted["input_manifest"]["artifact_id"],
                    rebuilt_manifest_id(api.state),
                )
                self.assertEqual(receipt["model"]["model_id"], "proteina-complexa")
                self.assertEqual(
                    [item["role"] for item in receipt["artifact_digests"]["uploads"]],
                    ["manifest-artifact", "request-input-manifest"],
                )
                self.assertTrue(receipt_path.is_file())

    def test_deterministic_archive_materializer_rejects_unsafe_paths_and_roles(
        self,
    ) -> None:
        for archive_path in (
            "",
            "/absolute",
            "../escape",
            "a/../escape",
            "a\\b",
            "x" * 101,
        ):
            with (
                self.subTest(archive_path=archive_path),
                self.assertRaisesRegex(
                    MODULE.AcceptanceError, "supporting_input_archive_path_invalid"
                ),
            ):
                MODULE.deterministic_tar_gzip(b"fixture", archive_path)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.pdb").write_bytes(b"fixture")
            declaration = {
                "role": "request-input-manifest",
                "name": "target-bundle",
                "path": "source.pdb",
                "encoding": "deterministic-tar-gzip-v1",
                "archive_path": "inputs/target.pdb",
            }
            with self.assertRaisesRegex(
                MODULE.AcceptanceError, "supporting_input_materializer_role_invalid"
            ):
                MODULE._read_declared_input(root, declaration)

    def test_digest_size_and_media_mismatches_fail_closed(self) -> None:
        for mode in ("digest", "size", "media"):
            with (
                self.subTest(mode=mode),
                TemporaryDirectory() as directory,
                FakeApi(mode) as api,
            ):
                root = Path(directory)
                fragment = self._direct_fixture(root)
                config = self._config(root, fragment, api)
                with self.assertRaisesRegex(
                    MODULE.AcceptanceError, "upload_receipt_mismatch"
                ):
                    MODULE.run_acceptance(
                        config,
                        MODULE.PublicApiClient(
                            api.endpoint, "test-token", timeout_seconds=2
                        ),
                    )
                self.assertFalse(config.receipt_path.exists())

    def test_route_rejection_fails_closed(self) -> None:
        with TemporaryDirectory() as directory, FakeApi("route") as api:
            root = Path(directory)
            fragment = self._direct_fixture(root)
            config = self._config(root, fragment, api)
            with self.assertRaisesRegex(MODULE.AcceptanceError, "http_submit_503"):
                MODULE.run_acceptance(
                    config,
                    MODULE.PublicApiClient(
                        api.endpoint, "test-token", timeout_seconds=2
                    ),
                )
            self.assertFalse(config.receipt_path.exists())

    def test_nonterminal_timeout_fails_closed(self) -> None:
        with TemporaryDirectory() as directory, FakeApi("timeout") as api:
            root = Path(directory)
            fragment = self._direct_fixture(root)
            config = self._config(root, fragment, api)
            with self.assertRaisesRegex(MODULE.AcceptanceError, "operation_timeout"):
                MODULE.run_acceptance(
                    config,
                    MODULE.PublicApiClient(
                        api.endpoint, "test-token", timeout_seconds=2
                    ),
                )
            self.assertFalse(config.receipt_path.exists())

    def test_semantic_validation_failure_fails_closed(self) -> None:
        with TemporaryDirectory() as directory, FakeApi("semantic") as api:
            root = Path(directory)
            fragment = self._direct_fixture(root)
            config = self._config(root, fragment, api)
            with self.assertRaisesRegex(
                MODULE.AcceptanceError, "semantic_validation_failed"
            ):
                MODULE.run_acceptance(
                    config,
                    MODULE.PublicApiClient(
                        api.endpoint, "test-token", timeout_seconds=2
                    ),
                )
            self.assertFalse(config.receipt_path.exists())


def rebuilt_manifest_id(state: ApiState) -> str:
    return str(UUID(int=200 + int(UUID(state.uploaded[-1]["upload_id"]))))


if __name__ == "__main__":
    unittest.main()
