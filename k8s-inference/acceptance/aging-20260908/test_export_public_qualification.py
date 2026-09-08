"""Synthetic receipt tests; these fixtures are not live qualification evidence."""

import copy
import json
import sys
from types import MappingProxyType

import export_public_qualification as exporter
import pytest

# Match control-plane tests: validate the checked-out archival loader, not a
# potentially older installed catalog wheel in the acceptance environment.
sys.path.insert(0, str(exporter.ROOT / "catalog/runtime"))

from fs2_serve_catalog.consumer import ServingBindings, bind_gateway_catalog  # noqa: E402
from fs2_serve_catalog.loader import load_catalog  # noqa: E402

from fs2_serve.deployment_runtimes import (  # noqa: E402
    SET_SCHEMA,
    DeploymentRuntimeError,
    bind_deployment_runtimes,
)
from fs2_serve.native_catalog import augment_native_catalog  # noqa: E402


SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64
SYNTHETIC_SECRET = "synthetic-secret-must-never-be-exported"  # noqa: S105


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def alter(path, mutate):
    value = exporter.read(path)
    mutate(value)
    write(path, value)


@pytest.fixture
def campaign(tmp_path):
    """Use actual immutable identities/results but synthetic clocks and IDs."""
    root = tmp_path / "synthetic-not-public-acceptance"
    witness_root = tmp_path / "synthetic-witness"
    publication = tmp_path / "publication.json"
    write(
        publication,
        {
            "source_commit": SOURCE,
            "source_tree": "c" * 40,
            "digest": DIGEST,
            "repository": "example.invalid/test",
            "secret": SYNTHETIC_SECRET,
        },
    )
    models = []
    for model, device in exporter.MODELS.items():
        app_id = f"synthetic-{model}-app"
        operations = []
        traces = []
        declaration = exporter.read(
            exporter.ROOT / "catalog/runtime/native" / f"{model}.json"
        )
        record = declaration["record"]
        image = record["runtime"]["image"]["reference"]
        native = exporter.read(exporter.HERE / f"{model}-r01.json")
        spec = {
            "modelRef": model,
            "lifecycle": {"desiredState": "Enabled"},
            "runtime": {"image": image},
            "artifact": {
                "revision": record["model"]["source"]["revision"],
                "manifestDigest": record["cache"]["artifact"]["manifest_digest"],
            },
            "availability": {"minReplicas": 0, "maxReplicas": 1, "warmWindows": []},
        }
        container = {
            "pod_uid": f"synthetic-{model}-pod-uid",
            "pod_name": f"synthetic-{model}-pod",
            "node_name": "synthetic-node",
            "ready": True,
            "image": image,
            "private_data": SYNTHETIC_SECRET,
        }

        def observation(at, containers, status):
            return {
                "at": at,
                "summary": {"app_id": app_id, "status": status},
                "containers": {
                    "state": "available",
                    "truncated": False,
                    "items": copy.deepcopy(containers),
                    "total": len(containers),
                },
            }

        def http(method, path, response, status=200, **extra):
            traces.append(
                {
                    "method": method,
                    "path": path,
                    "status": status,
                    "response": copy.deepcopy(response),
                    "response_sha256": exporter.value_sha(response),
                    **extra,
                }
            )

        def mcp(tool, response):
            traces.append(
                {
                    "tool": tool,
                    "response": {
                        "isError": False,
                        "structuredContent": copy.deepcopy(response),
                    },
                }
            )

        http("GET", "/v1/models", {"data": [{"id": model}]})
        mcp("list_models", {"data": [{"id": model}]})
        for index in (0, 1):
            operation_id = f"synthetic-{model}-op-{index}"
            result = native["native_http_predictions"][index]["body"]
            operation = {
                "operation_id": operation_id,
                "submission_index": index,
                "request_sha256": declaration["semantic_requests"]["requests"][index][
                    "payload_sha256"
                ],
                "terminal": {
                    "id": operation_id,
                    "status": "succeeded",
                    "accepted_at": f"2026-09-08T00:0{index + 1}:00+00:00",
                    "completed_at": f"2026-09-08T00:0{index + 1}:10+00:00",
                    "principal_id": SYNTHETIC_SECRET,
                },
                "result": result,
                "client_seconds": 11.0,
                "app_observations": [
                    observation(
                        f"2026-09-08T00:0{index + 1}:09+00:00",
                        [container],
                        "Ready",
                    )
                ],
            }
            operations.append(operation)
            if index == 0:
                for reused in ("false", "true"):
                    http(
                        "POST",
                        f"/v1/models/{model}:invoke",
                        {"id": operation_id},
                        status=202,
                        operation_id=operation_id,
                        replay=reused,
                    )
            else:
                for reused in (False, True):
                    mcp("invoke_model", {"id": operation_id, "reused": reused})
            http("GET", f"/v1/operations/{operation_id}/result", result)
            mcp(
                "get_operation_result",
                {"operation": {"id": operation_id}, "result": result},
            )
        http("GET", "/v1/models", {"error": "unauthorized"}, status=401)
        outcome = {
            "outcome": "passed",
            "model_id": model,
            "app_id": app_id,
            "release": SOURCE,
            "started_at": "2026-09-08T00:00:00+00:00",
            "completed_at": "2026-09-08T00:04:00+00:00",
            "test_key_revoked": True,
            "key_id": SYNTHETIC_SECRET,
            "original_settings": {"serving": {"spec": {"private": SYNTHETIC_SECRET}}},
            "test_settings": {"serving": {"spec": spec}},
            "final_settings": {"serving": {"spec": copy.deepcopy(spec)}},
            "zero_before": observation("2026-09-08T00:00:10+00:00", [], "Cold"),
            "zero_after": observation("2026-09-08T00:03:00+00:00", [], "Cold"),
            "operations": operations,
            "usage": {"logical_runs": 2},
            "runs": {
                "items": [
                    {"operation": {"id": row["operation_id"]}} for row in operations
                ]
            },
        }
        write(root / model / "outcome.json", outcome)
        for index, trace in enumerate(traces):
            write(root / model / f"{index:03d}-synthetic.json", trace)
        write(
            witness_root / f"{model}.json",
            {
                "model_id": model,
                "captured_at": "2026-09-08T00:01:09+00:00",
                "pod": {
                    "metadata": {
                        "uid": container["pod_uid"],
                        "name": container["pod_name"],
                        "namespace": "synthetic",
                        "creationTimestamp": "2026-09-08T00:01:01+00:00",
                        "annotations": {"private": SYNTHETIC_SECRET},
                    },
                    "spec": {
                        "nodeName": container["node_name"],
                        "containers": [
                            {
                                "image": image,
                                "env": [{"name": "SECRET", "value": SYNTHETIC_SECRET}],
                                "resources": {
                                    "requests": {
                                        "cpu": "1" if device == "cpu" else "2",
                                        "memory": "4Gi",
                                        **(
                                            {"nvidia.com/gpu": "1"}
                                            if device == "cuda"
                                            else {}
                                        ),
                                    }
                                },
                            }
                        ],
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                },
                "node": {
                    "metadata": {
                        "name": container["node_name"],
                        "creationTimestamp": "2026-09-07T00:00:00+00:00",
                        "labels": {
                            "workload.fs2.nebius/general-cpu": "true",
                            "accelerator.fs2.nebius/class": record["resources"]["gpu"][
                                "class"
                            ].lower(),
                        },
                    },
                    "status": {
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "True",
                                "lastTransitionTime": "2026-09-07T00:01:00+00:00",
                            }
                        ]
                    },
                },
            },
        )
        models.append({"model_id": model, "app_id": app_id, "outcome": "passed"})
    write(
        root / "outcome.json",
        {"outcome": "passed", "release": SOURCE, "models": models},
    )
    return root, witness_root, publication


def export(campaign, model="phenoage"):
    root, witnesses, publication = campaign
    return exporter.export_model(root, model, witnesses / f"{model}.json", publication)


@pytest.mark.parametrize("model", exporter.MODELS)
def test_export_is_exact_scoped_and_credential_free(campaign, model):
    receipt = export(campaign, model)
    assert receipt["outcome"] == "passed"
    assert receipt["release"]["source_commit"] == SOURCE
    assert receipt["runtime_image"].endswith(
        exporter.read(exporter.ROOT / "catalog/runtime/native" / f"{model}.json")[
            "record"
        ]["runtime"]["image"]["digest"]
    )
    assert receipt["scope"]["replica_transition"] == "0-to-1-to-0"
    assert receipt["scope"]["maximum_configured_replicas"] == 1
    assert receipt["scope"]["multi_replica_scale_out_qualified"] is False
    assert receipt["scope"]["other_hardware_qualified"] is False
    assert receipt["scope"]["gpu_snapshot"] == (
        "not-applicable-cpu" if model == "phenoage" else "not-qualified"
    )
    assert [row["submission"] for row in receipt["operations"]] == ["HTTP", "MCP"]
    assert all(
        row["accepted_to_terminal_seconds"] == 10 for row in receipt["operations"]
    )
    assert receipt["worker"]["node_existed_before_first_admission"] is True
    assert receipt["temporary_key_revoked_and_denied"] is True
    encoded = json.dumps(receipt)
    assert SYNTHETIC_SECRET not in encoded
    assert "key_id" not in encoded and "principal_id" not in encoded
    assert str(campaign[0].parent) not in encoded


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda v: v.update(outcome="failed"), "model_or_cleanup_not_passed"),
        (lambda v: v.update(test_key_revoked=False), "model_or_cleanup_not_passed"),
        (
            lambda v: v["zero_before"]["summary"].update(status="Desired"),
            "cold_status_not_observed",
        ),
        (lambda v: v["zero_after"]["containers"].update(total=1), "zero_not_observed"),
        (
            lambda v: v["zero_after"]["containers"].update(truncated=True),
            "zero_unavailable",
        ),
        (
            lambda v: v["zero_after"]["summary"].update(app_id="other"),
            "zero_app_mismatch",
        ),
        (lambda v: v["operations"].pop(), "logical_operations_not_two"),
        (
            lambda v: v["operations"][0].update(request_sha256="0" * 64),
            "original_input_identity_mismatch",
        ),
        (
            lambda v: v["operations"][0]["terminal"].update(status="failed"),
            "operation_not_successful",
        ),
        (
            lambda v: v["operations"][0]["result"].update(device="cuda"),
            "output_runtime_mismatch",
        ),
        (
            lambda v: v["operations"][0]["result"]["predictions"][0].update(
                phenotypic_age_years=0
            ),
            "output_parity_failed",
        ),
        (
            lambda v: v["operations"][0].update(app_observations=[]),
            "ready_worker_not_observed",
        ),
        (
            lambda v: v["operations"][0].update(client_seconds=float("nan")),
            "client_duration_invalid",
        ),
        (
            lambda v: v["operations"][0].update(submission_index=1),
            "submission_order_mismatch",
        ),
        (lambda v: v["usage"].update(logical_runs=3), "usage_not_two"),
        (
            lambda v: v["final_settings"]["serving"]["spec"]["availability"].update(
                minReplicas=1
            ),
            "settings_changed_during_trial",
        ),
    ],
)
def test_rejects_incomplete_changed_or_failed_model_receipts(campaign, mutate, code):
    alter(campaign[0] / "phenoage/outcome.json", mutate)
    with pytest.raises(ValueError, match=code):
        export(campaign)


@pytest.mark.parametrize(
    ("file", "mutate", "code"),
    [
        ("000", lambda v: v.update(status=500), "failed_http_exchange_recorded"),
        (
            "000",
            lambda v: v.update(error_type="ConnectError"),
            "transport_error_recorded",
        ),
        (
            "000",
            lambda v: v["response"]["data"].append({"id": "other"}),
            "http_discovery_scope_mismatch",
        ),
        ("001", lambda v: v["response"].update(isError=True), "mcp_tool_failed"),
        ("002", lambda v: v.update(replay="true"), "http_original_already_reused"),
        ("003", lambda v: v.update(replay="false"), "http_replay_not_reused"),
        ("004", lambda v: v.update(response={}), "http_result_proof_missing"),
        (
            "007",
            lambda v: v["response"]["structuredContent"].update(id="other"),
            "mcp_admission_or_replay_mismatch",
        ),
    ],
)
def test_rejects_recorded_http_mcp_and_replay_failures(campaign, file, mutate, code):
    alter(campaign[0] / "phenoage" / f"{file}-synthetic.json", mutate)
    with pytest.raises(ValueError, match=code):
        export(campaign)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda v: v["pod"]["metadata"].update(uid="direct-native-not-public-pod"),
            "witness_not_public_worker",
        ),
        (
            lambda v: v.update(captured_at="2026-09-07T00:00:00+00:00"),
            "witness_outside_trial",
        ),
        (
            lambda v: v["pod"]["spec"]["containers"][0].update(image="other-image"),
            "witness_runtime_image_mismatch",
        ),
        (
            lambda v: v["pod"]["spec"]["containers"][0]["resources"]["requests"].update(
                {"nvidia.com/gpu": "1"}
            ),
            "witness_gpu_count_mismatch",
        ),
        (
            lambda v: v["pod"]["status"]["conditions"][0].update(status="False"),
            "witness_pod_not_ready",
        ),
        (lambda v: v["node"]["metadata"]["labels"].clear(), "cpu_node_class_mismatch"),
    ],
)
def test_rejects_unrelated_or_invalid_runtime_witness(campaign, mutate, code):
    alter(campaign[1] / "phenoage.json", mutate)
    with pytest.raises(ValueError, match=code):
        export(campaign)


def test_node_provisioning_is_derived_not_assumed(campaign):
    alter(
        campaign[1] / "phenoage.json",
        lambda v: v["node"]["metadata"].update(
            creationTimestamp="2026-09-08T00:01:01+00:00",
        ),
    )
    assert export(campaign)["worker"]["node_existed_before_first_admission"] is False


@pytest.mark.parametrize("cached", [False, True])
def test_image_boundary_uses_only_actual_uid_and_image_without_raw_messages(cached):
    image = "example.invalid/image@sha256:" + "a" * 64
    message = (
        f'Container image "{image}" already present on machine'
        if cached
        else (
            f'Successfully pulled image "{image}" in 1m36.335s (including waiting). Image size: 4167595352 bytes.'
        )
    )
    event = {
        "involvedObject": {"uid": "actual-public-pod"},
        "reason": "Pulled",
        "message": message,
        "firstTimestamp": "2026-09-08T15:12:19Z",
        "lastTimestamp": "2026-09-08T15:12:19Z",
    }
    witness = {
        "pod": {"metadata": {"uid": "actual-public-pod"}},
        "events": {
            "items": [
                event,
                {**event, "involvedObject": {"uid": "old-direct-pod"}},
                {**event, "message": SYNTHETIC_SECRET},
            ]
        },
    }
    boundary = exporter.image_pull_boundary(witness, image)
    assert boundary["state"] == ("image-already-present" if cached else "image-pulled")
    assert len(boundary["events"]) == 1
    assert boundary["events"][0]["reported_pull_seconds"] == (
        None if cached else 96.335
    )
    assert boundary["wire_bytes"] is None
    assert SYNTHETIC_SECRET not in json.dumps(boundary)
    assert "4167595352" not in json.dumps(boundary)


def test_failure_in_either_model_produces_no_success_output_or_overlay_change(
    campaign, tmp_path, monkeypatch
):
    output = tmp_path / "export"
    entries = [
        exporter.ROOT / "catalog/runtime/deployment-runtimes" / f"{model}-{device}.json"
        for model, device in exporter.MODELS.items()
    ]
    original = [path.read_bytes() for path in entries]
    alter(
        campaign[0] / "altumage/outcome.json",
        lambda v: v.update(test_key_revoked=False),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export",
            "--campaign-root",
            str(campaign[0]),
            "--runtime-witness-root",
            str(campaign[1]),
            "--publication",
            str(campaign[2]),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(ValueError, match="model_or_cleanup_not_passed"):
        exporter.main()
    assert not output.exists()
    assert [path.read_bytes() for path in entries] == original


def test_campaign_failure_or_wrong_release_cannot_be_promoted(campaign):
    alter(campaign[2], lambda v: v.update(source_commit="d" * 40))
    with pytest.raises(ValueError, match="release_sidecar_mismatch"):
        export(campaign)
    alter(campaign[0] / "outcome.json", lambda v: v.update(outcome="failed"))
    with pytest.raises(ValueError, match="campaign_not_passed"):
        export(campaign)


@pytest.fixture(scope="module")
def catalog_binding():
    catalog_root = exporter.ROOT / "catalog/runtime"
    archive = load_catalog(catalog_root, repo_root=catalog_root / "packaged-repository")
    catalog = augment_native_catalog(
        archive, catalog_root, repo_root=catalog_root / "packaged-repository"
    )
    bindings = ServingBindings(archive.digest, MappingProxyType({}))
    return catalog, bindings, bind_gateway_catalog(catalog, bindings)


def bind_selected(tmp_path, entry, catalog_binding):
    catalog, bindings, gateway = catalog_binding
    selected = tmp_path / "synthetic-selected-runtime.json"
    write(selected, {"schema": SET_SCHEMA, "models": {entry["model_id"]: entry}})
    return bind_deployment_runtimes(
        gateway,
        catalog,
        bindings,
        selected,
        catalog_dir=exporter.ROOT / "catalog/runtime",
    )


def synthetic_promoted_entry(campaign, model):
    """Only a temporary in-memory qualification; never update selected source."""
    entry = exporter.read(
        exporter.ROOT
        / "catalog/runtime/deployment-runtimes"
        / f"{model}-{exporter.MODELS[model]}.json"
    )
    receipt_hash = exporter.value_sha(export(campaign, model))
    for state in (
        "route_active",
        "http_mcp_qualified",
        "cold_start_qualified",
        "elasticity_qualified",
    ):
        entry["qualification"]["states"][state] = True
    for field in exporter.PUBLIC_EVIDENCE_FIELDS:
        entry["qualification"]["evidence"][field] = receipt_hash
    return entry


@pytest.mark.parametrize("model", exporter.MODELS)
def test_hashed_synthetic_public_receipt_binds_only_exact_selected_runtime(
    campaign, tmp_path, catalog_binding, model
):
    entry = synthetic_promoted_entry(campaign, model)
    selected = bind_selected(tmp_path, entry, catalog_binding).model(model)
    assert selected.qualification["states"]["elasticity_qualified"]
    assert selected.qualification["states"]["http_mcp_qualified"]
    # The historical qualification must not fake an active current controller publication.
    assert not selected.routable
    entry["qualification"]["active_runtime"]["runtime_image_digest"] = (
        "sha256:" + "e" * 64
    )
    with pytest.raises(DeploymentRuntimeError, match="identity mismatch"):
        bind_selected(tmp_path, entry, catalog_binding)


@pytest.mark.parametrize("field", exporter.PUBLIC_EVIDENCE_FIELDS)
def test_true_public_flag_without_its_evidence_hash_is_rejected(
    campaign, tmp_path, catalog_binding, field
):
    entry = synthetic_promoted_entry(campaign, "phenoage")
    entry["qualification"]["evidence"][field] = None
    with pytest.raises(DeploymentRuntimeError, match="validation failed") as error:
        bind_selected(tmp_path, entry, catalog_binding)
    assert field in str(error.value.__cause__)
