from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("cosmos_customer_acceptance", HERE / "run_acceptance.py")
builder = _load("cosmos_customer_manifest", HERE / "build_manifest.py")

DIGEST = "sha256:" + "a" * 64
EVALUATED_AT = datetime(2026, 9, 15, 18, 0, 1, tzinfo=UTC)
IDENTITY = {
    "source_revision": "b" * 40,
    "runtime_images": {"control-plane": DIGEST, "cosmos": "sha256:" + "c" * 64},
    "configuration_sha256": "sha256:" + "d" * 64,
    "model_revision": "nvidia/Cosmos3-Nano@immutable",
    "client_build_sha256": "sha256:" + "e" * 64,
    "tenant_policy_sha256": "sha256:" + "f" * 64,
    "public_endpoint": "https://inference.example.test/mcp",
    "public_tool_catalog_sha256": "sha256:" + "1" * 64,
}


def test_manifests_keep_apps_and_cosmos_modes_independent_and_request_exact_customer_scope():
    manifest = builder.manifest(IDENTITY)
    capabilities = {row["id"]: row for row in manifest["capabilities"]}
    assert set(capabilities) == {
        "text-to-image",
        "text-to-video",
        "image-to-video",
        "video-to-video",
        "transfer-video",
        "forward-dynamics",
        "inverse-dynamics",
    }
    assert [row["id"] for row in capabilities["video-to-video"]["scenarios"]] == [
        "v2v-url",
        "v2v-upload",
    ]
    assert not capabilities["forward-dynamics"]["advertised"]
    assert not capabilities["inverse-dynamics"]["advertised"]
    assert manifest["requested_capability_ids"] == ["video-to-video"]
    lerobot = builder.lerobot_manifest(IDENTITY)
    assert lerobot["app_id"] == "cosmos3-lerobot-augmentation"
    assert [row["id"] for row in lerobot["capabilities"]] == ["lerobot-augmentation"]
    assert lerobot["capabilities"][0]["scenarios"][0]["workload_states"] == [
        "scale-from-zero"
    ]


def test_mp4_validation_decodes_frames_and_rejects_an_unchanged_copy(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("requires ffmpeg and ffprobe")
    source = tmp_path / "source.mp4"
    builder_module = _load("cosmos_mp4_fixture", HERE / "build_mp4_fixture.py")
    built = builder_module.build(source)
    assert built["frames"] == 16
    changed = tmp_path / "changed.mp4"
    subprocess.run(  # noqa: S603 - resolved test dependency and fixed arguments
        [
            str(shutil.which("ffmpeg")),
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            "hue=s=0.35",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(changed),
        ],
        check=True,
    )
    declared = runner.file_identity(changed) | {"media_type": "video/mp4"}
    assert runner.validate_mp4(source, changed, declared)["decoded_pixels_distinct"]
    with pytest.raises(runner.AcceptanceError, match="output_decoded_pixels_unchanged"):
        runner.validate_mp4(source, source, runner.file_identity(source))


def _run_receipt() -> dict[str, object]:
    case_rows = [
        {
            "scenario_id": scenario,
            "outcome": "passed",
            "operation_id": f"op-{scenario}-{cohort}",
            "workload_states": (
                ["scale-from-zero"]
                if scenario == "lerobot-lighting" or cohort == 1
                else ["hot"]
            ),
        }
        for cohort in (1, 2)
        for scenario in runner.SCENARIOS
    ]
    return {
        "completed_at": "2026-09-15T18:00:00+00:00",
        "tenant_policy_verified": True,
        "workload_states": ["scale-from-zero", "hot"],
        "observed_surfaces": ["containers", "logs", "metrics", "runs", "usage"],
        "fixtures": {
            "url_mp4": {"sha256": "2" * 64},
            "local_mp4": {"sha256": "3" * 64},
            "lerobot": {"sha256": "4" * 64},
        },
        "cohorts": [
            {"cases": case_rows[:3]},
            {"cases": case_rows[3:]},
        ],
    }


def test_raw_mcp_evidence_cannot_qualify_librechat_or_the_whole_app():
    manifest = builder.manifest(IDENTITY)
    receipts = runner.evidence(manifest, _run_receipt(), "mcp-sdk")
    verdict = runner.gate.evaluate(manifest, receipts, evaluated_at=EVALUATED_AT)
    states = {row["capability_id"]: row["state"] for row in verdict["capabilities"]}
    assert states["video-to-video"] == "partial"
    assert not verdict["ready"]


def test_exact_requested_capabilities_qualify_but_uncovered_sibling_modes_keep_app_not_ready():
    manifest = builder.manifest(IDENTITY)
    receipts = runner.evidence(manifest, _run_receipt(), "librechat+mcp")
    verdict = runner.gate.evaluate(manifest, receipts, evaluated_at=EVALUATED_AT)
    states = {row["capability_id"]: row["state"] for row in verdict["capabilities"]}
    assert states["video-to-video"] == "qualified"
    assert states["text-to-video"] == "untested"
    assert verdict["verdict"] == "not-ready"
    report = runner.human_verdict([verdict], requested_scope_qualified=True)
    assert "forward-dynamics | withdrawn / unsupported | untested" in report


def test_lerobot_evidence_qualifies_only_the_independent_lerobot_app():
    manifest = builder.lerobot_manifest(IDENTITY)
    receipts = runner.evidence(manifest, _run_receipt(), "librechat+mcp")
    verdict = runner.gate.evaluate(manifest, receipts, evaluated_at=EVALUATED_AT)
    assert verdict["ready"]
    assert verdict["capabilities"][0]["state"] == "qualified"


def test_evidence_never_borrows_another_apps_workload_state():
    receipt = _run_receipt()
    receipt["workload_states"] = ["hot", "scale-from-zero"]
    for cohort in receipt["cohorts"]:
        for case in cohort["cases"]:
            if case["scenario_id"] == "lerobot-lighting":
                case["workload_states"] = ["hot"]
    manifest = builder.lerobot_manifest(IDENTITY)
    receipts = runner.evidence(manifest, receipt, "librechat+mcp")
    verdict = runner.gate.evaluate(manifest, receipts, evaluated_at=EVALUATED_AT)
    assert verdict["capabilities"][0]["state"] == "partial"
    assert "scale-from-zero" in str(
        verdict["capabilities"][0]["scenarios"][0]["reasons"]
    )


def test_librechat_receipt_must_name_the_exact_operations_and_build():
    operations = {"00000000-0000-4000-8000-000000000001": "v2v-url"}
    receipt = {
        "schema": "fs2-serve.nebius.ai/librechat-mcp-acceptance/v1",
        "client_build_sha256": IDENTITY["client_build_sha256"],
        "public_endpoint": IDENTITY["public_endpoint"],
        "operations": [
            {
                "scenario_id": "v2v-url",
                "operation_id": next(iter(operations)),
                "outcome": "passed",
            }
        ],
    }
    assert runner.client_path(IDENTITY, receipt, operations) == "librechat+mcp"
    receipt["client_build_sha256"] = DIGEST
    with pytest.raises(runner.AcceptanceError, match="librechat_build_mismatch"):
        runner.client_path(IDENTITY, receipt, operations)
    receipt["client_build_sha256"] = IDENTITY["client_build_sha256"]
    receipt["operations"][0]["scenario_id"] = "v2v-upload"
    with pytest.raises(
        runner.AcceptanceError, match="librechat_did_not_exercise_exact_operations"
    ):
        runner.client_path(IDENTITY, receipt, operations)


def test_tool_error_result_requires_an_exact_jsonrpc_code():
    result = SimpleNamespace(
        is_error=True,
        structured_content={
            "error": {"code": -32602, "data": {"type": "model_input_validation"}}
        },
        content=[],
    )
    assert runner._tool_failure(result) == (-32602, "model_input_validation")
    with pytest.raises(
        runner.AcceptanceError, match="invalid_request_missing_jsonrpc_code"
    ):
        runner._tool_failure(
            SimpleNamespace(is_error=True, structured_content={}, content=[])
        )
    with pytest.raises(runner.AcceptanceError, match="invalid_local_path_was_accepted"):
        runner._tool_failure(
            SimpleNamespace(is_error=False, structured_content={}, content=[])
        )


def test_admin_envelope_rejects_partial_or_unavailable_sources():
    class Response:
        status_code = 200

        def __init__(self, meta):
            self.meta = meta

        def json(self):
            return {"meta": self.meta, "data": {"items": []}}

    with pytest.raises(runner.AcceptanceError, match="admin_reported_warning"):
        runner._data(Response({"warnings": ["partial"], "sources": []}))
    with pytest.raises(runner.AcceptanceError, match="admin_source_not_available"):
        runner._data(
            Response(
                {
                    "warnings": [],
                    "sources": [{"source": "loki", "state": "unavailable"}],
                }
            )
        )


def test_workload_state_comes_from_actual_container_inventory():
    assert (
        runner.workload_state({"state": "available", "total": 0, "items": []})
        == "scale-from-zero"
    )
    assert (
        runner.workload_state(
            {
                "state": "available",
                "total": 1,
                "items": [{"id": "pod-a", "ready": True, "restarts": 0}],
            }
        )
        == "hot"
    )
    with pytest.raises(
        runner.AcceptanceError, match="workload_neither_hot_nor_scale_from_zero"
    ):
        runner.workload_state(
            {
                "state": "available",
                "total": 1,
                "items": [{"id": "pod-a", "ready": False, "restarts": 0}],
            }
        )


@pytest.mark.asyncio
async def test_live_release_identity_joins_configuration_and_both_apps():
    observed_identity = {
        **IDENTITY,
        "runtime_images": {
            "control-plane": DIGEST,
            "cosmos3-nano": "sha256:" + "c" * 64,
            "cosmos3-lerobot-augmentation": "sha256:" + "9" * 64,
        },
    }

    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return {"meta": {"warnings": [], "sources": []}, "data": self.data}

    class Admin:
        async def get(self, path, params=None):
            del params
            if path.endswith("/configuration"):
                return Response(
                    {
                        "etag": observed_identity["configuration_sha256"].removeprefix(
                            "sha256:"
                        )
                    }
                )
            if path.endswith("/models"):
                return Response(
                    {
                        "items": [
                            {
                                "identity": {
                                    "id": "cosmos3-nano",
                                    "model_revision": observed_identity[
                                        "model_revision"
                                    ],
                                    "runtime_image_digest": "registry.example/cosmos@"
                                    + observed_identity["runtime_images"][
                                        "cosmos3-nano"
                                    ],
                                }
                            }
                        ]
                    }
                )
            if path.endswith("/scientific-models"):
                return Response(
                    {
                        "items": [
                            {
                                "model_id": "cosmos3-lerobot-augmentation",
                                "readiness": "qualified",
                                "qualification": {"state": "qualified"},
                                "backend": {
                                    "source_revision": observed_identity[
                                        "model_revision"
                                    ],
                                    "model_revision": observed_identity[
                                        "model_revision"
                                    ],
                                    "runtime_image_digest": observed_identity[
                                        "runtime_images"
                                    ]["cosmos3-lerobot-augmentation"],
                                },
                            }
                        ]
                    }
                )
            raise AssertionError(path)

    observed = await runner.verify_live_release(Admin(), observed_identity)
    assert observed["cosmos3_lerobot_augmentation"]["qualification"] == "qualified"

    claimed_identity = {
        **observed_identity,
        "configuration_sha256": "sha256:" + "0" * 64,
    }
    with pytest.raises(
        runner.AcceptanceError, match="live_configuration_identity_mismatch"
    ):
        await runner.verify_live_release(Admin(), claimed_identity)


@pytest.mark.asyncio
async def test_invalid_request_debug_correlation_requires_the_exact_payload_marker_and_semantics():
    marker = "unique-negative-idempotency-marker"
    summary = {
        "id": "exchange-one",
        "mcp_tool": "cosmos3_nano_video_to_video",
        "semantic_outcome": "failed",
        "jsonrpc_error_code": -32602,
        "admission_stage": "pre_admission",
    }
    detail = {
        **summary,
        "request_id": "request-one",
        "operation_id": None,
        "model_id": "cosmos3-nano",
        "http_status": 200,
        "semantic_error_type": "model_input_validation",
        "request_body": {
            "encoding": "utf-8",
            "data": json.dumps({"params": {"arguments": {"idempotency_key": marker}}}),
            "complete": True,
        },
        "response_body": {
            "encoding": "utf-8",
            "data": json.dumps({"jsonrpc": "2.0", "error": {"code": -32602}}),
            "complete": True,
        },
    }

    class Response:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def json(self):
            return {"meta": {"warnings": [], "sources": []}, "data": self.value}

    class Admin:
        async def get(self, path, params=None):
            del params
            return Response(
                detail if path.endswith("/exchange-one") else {"items": [summary]}
            )

    observed = await runner.invalid_request_observability(
        Admin(), "app-cosmos", started_at="2026-09-15T18:00:00+00:00", marker=marker
    )
    assert observed["semantic_outcome"] == "failed" and observed["http_status"] == 200
    assert observed["operation_id"] is None and marker not in json.dumps(observed)


@pytest.mark.asyncio
async def test_lerobot_submission_uses_scientific_envelope_and_validates_published_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = tmp_path / "tiny-lrobot.tar.zst"
    bundle.write_bytes(b"tiny-bundle")
    output = tmp_path / "output"
    output.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    refs = [
        {
            "artifact_id": "00000000-0000-4000-8000-000000000001",
            "sha256": "1" * 64,
            "size_bytes": len(bundle.read_bytes()),
            "media_type": "application/x-tar",
            "compression": "zstd",
        },
        {
            "artifact_id": "00000000-0000-4000-8000-000000000002",
            "sha256": "2" * 64,
            "size_bytes": 512,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
    ]
    result_artifact = {
        "artifact_id": "00000000-0000-4000-8000-000000000003",
        "sha256": "3" * 64,
        "size_bytes": 600,
        "media_type": "application/json",
        "compression": "none",
    }
    variant_artifact = {
        "artifact_id": "00000000-0000-4000-8000-000000000004",
        "sha256": "4" * 64,
        "size_bytes": 700,
        "media_type": "application/x-tar",
        "compression": "zstd",
    }
    output_manifest = {
        "artifact_id": "00000000-0000-4000-8000-000000000005",
        "sha256": "5" * 64,
        "size_bytes": 800,
        "media_type": "application/vnd.fs2.scientific-manifest+json",
        "compression": "none",
    }

    class FakePublic:
        def __init__(self):
            self.uploads = []
            self.submission = None
            self.downloads = []

        async def upload(
            self, model_id, path, media_type, compression, idempotency_key
        ):
            self.uploads.append(
                {
                    "model_id": model_id,
                    "path": path,
                    "document": json.loads(path.read_text())
                    if media_type.endswith("+json")
                    else None,
                    "media_type": media_type,
                    "compression": compression,
                    "idempotency_key": idempotency_key,
                }
            )
            return refs[len(self.uploads) - 1]

        async def call(self, name, arguments):
            if name == "submit_cosmos3_lerobot_augmentation":
                self.submission = arguments
                return {"operation": {"id": "00000000-0000-4000-8000-000000000006"}}
            if name == "get_scientific_result":
                return {
                    "schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
                    "terminal_status": "succeeded",
                    "output_manifest": output_manifest,
                }
            if name == "inspect_scientific_artifact_manifest":
                assert arguments == {
                    "artifact_id": output_manifest["artifact_id"],
                    "limit": 16,
                }
                return {
                    "artifact": output_manifest,
                    "entry_count": 2,
                    "truncated": False,
                    "entries": [
                        {
                            "name": "result",
                            "semantic_type": "lerobot-augmentation-result/v1",
                            "artifact": result_artifact,
                        },
                        {
                            "name": "variant-00",
                            "semantic_type": "lerobot-v3-augmented-bundle/v1",
                            "artifact": variant_artifact,
                        },
                    ],
                }
            raise AssertionError(name)

        async def wait(self, op_id, timeout_seconds):
            return {
                "operation": {"id": op_id, "status": "succeeded"},
                "timeout": timeout_seconds,
            }

        async def download(self, tool, artifact, destination):
            self.downloads.append((tool, artifact, destination))
            if artifact == result_artifact:
                runner.write_private(
                    destination,
                    {
                        "schema": "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-result/v1",
                        "operation_id": "00000000-0000-4000-8000-000000000006",
                        "status": "succeeded",
                        "progress": {"completed_units": 1, "total_units": 1},
                        "variants": [
                            {
                                "variant_index": 0,
                                "seed": 20260916,
                                "artifact": variant_artifact,
                                "validation": {
                                    "reader": "lerobot==0.6.1",
                                    "episodes": 1,
                                    "frames": 16,
                                    "decoded_video_frames": 16,
                                    "status": "passed",
                                },
                                "provenance_sha256": "6" * 64,
                            }
                        ],
                        "failures": [],
                    },
                    (),
                )
            else:
                descriptor = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(b"variant")

    fake = FakePublic()
    monkeypatch.setattr(
        runner, "validate_lerobot", lambda *args: {"reader": "lerobot==0.6.1"}
    )
    template = json.loads(
        (
            HERE.parents[1]
            / "models/general-media/lerobot-augmentation/fixtures/scientific-run-request.json"
        ).read_text()
    )
    accepted = await runner.submit_lerobot(
        fake, bundle, source, template, 1, output, 60, ("secret",)
    )

    assert fake.uploads[1]["document"] == {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "cosmos-lerobot-input-c1-20260915",
        "entries": [
            {
                "name": "lerobot-dataset",
                "semantic_type": "lerobot-v3-bundle/v1",
                "artifact": refs[0],
            }
        ],
    }
    assert fake.submission["input_manifest"] == refs[1]
    assert fake.submission["parameters"]["source"] == {
        "kind": "uploaded-bundle",
        **refs[0],
    }
    assert "source" not in {key for key in fake.submission if key != "parameters"}
    assert [item[1] for item in fake.downloads] == [result_artifact, variant_artifact]
    assert accepted["validation"]["reader"] == "lerobot==0.6.1"


def test_contract_examples_are_valid_json() -> None:
    for name in ("deployment-receipt.example.json", "librechat-receipt.example.json"):
        assert isinstance(json.loads((HERE / name).read_text(encoding="utf-8")), dict)
