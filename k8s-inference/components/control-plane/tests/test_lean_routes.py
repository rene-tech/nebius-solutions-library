from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import CATALOG_ROOT, CONTROL_ROOT, REPO_ROOT, SOLUTION_ROOT
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA
from fs2_serve_catalog.loader import load_catalog

from fs2_serve.activation_health import activation_set
from fs2_serve.admin import AdminReadService, derive_operational_model_state
from fs2_serve.admin_models import AdminActivationPhase, AdminModelState
from fs2_serve.api import _model_view
from fs2_serve.mcp_server import _model_view as mcp_model_view
from fs2_serve.qualification import build_qualification_projection
from fs2_serve.registry import Registry, RegistryError
from fs2_serve.telemetry import Metrics


def _route() -> dict[str, Any]:
    qwen = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT).model("qwen3-8b").to_dict()
    return {
        "schema": "fs2-serve.nebius.ai/lean-routes/v2",
        "routes": [
            {
                "model_id": "qwen3-8b",
                "variant_id": None,
                "model_revision": qwen["model"]["source"]["revision"],
                "runtime_image_digest": qwen["runtime"]["image"]["digest"],
                "service": {"namespace": "fs2-models", "name": "qwen3-8b-b300", "port": 8000},
                "storage_mode": "ephemeral-emptydir",
                "protocols": {"openai-chat": "/v1/chat/completions"},
                "operations": ["chat"],
                "mcp": {
                    "enabled": True,
                    "tool_name": "qwen3_8b_chat",
                    "description": "Run an authorized chat request on the retained Qwen3-8B service.",
                },
            }
        ],
    }


def _boltz2_route() -> dict[str, Any]:
    return {
        "model_id": "boltz2",
        "variant_id": None,
        "model_revision": "sha256:0788c95c8b5b6c1a73a62c656b298ecc353a8187dc22b794f496ae40672c4c98",
        "runtime_image_digest": "sha256:0788c95c8b5b6c1a73a62c656b298ecc353a8187dc22b794f496ae40672c4c98",
        "service": {"namespace": "fs2-models", "name": "boltz2-b300", "port": 8000},
        "storage_mode": "sfs-pvc",
        "protocols": {"native": "/biology/mit/boltz2/predict"},
        "operations": ["predict"],
        "mcp": {
            "enabled": True,
            "tool_name": "boltz2_predict",
            "description": "Run an authorized Boltz2 structure prediction.",
        },
    }


def _diffdock_variant_route() -> dict[str, Any]:
    return {
        "model_id": "diffdock",
        "variant_id": "diffdock-upstream-blackwell-sm103",
        "model_revision": "85c49b60d3e0b0182a59ee43a34a6d7036981284",
        "runtime_image_digest": "sha256:cb3875f7d66b8d170d0e3f16d3d9a63aee8d63fbb23fdf65ec7ea0214d849529",
        "service": {"namespace": "fs2-models", "name": "diffdock-b300", "port": 8000},
        "storage_mode": "ephemeral-emptydir",
        "protocols": {"native": "/molecular-docking/diffdock/generate"},
        "operations": ["dock"],
        "mcp": {
            "enabled": True,
            "tool_name": "infer_diffdock",
            "description": "Run an authorized DiffDock molecular docking request.",
        },
    }


def _cosmos_h100_route() -> dict[str, Any]:
    inventory = json.loads((CONTROL_ROOT / "contracts/all-models-live-services.json").read_text(encoding="utf-8"))
    return {
        "schema": "fs2-serve.nebius.ai/lean-routes/v4",
        "routes": [
            {
                **inventory["routes"]["cosmos3-nano"],
                "model_id": "cosmos3-nano",
                "service": {
                    **inventory["routes"]["cosmos3-nano"]["service"],
                    "namespace": inventory["namespace"],
                },
                "placement": {
                    "region": "eu-north1",
                    "accelerator_class": "nvidia-h100-sxm5-80gb",
                    "pool_id": "h100-preemptible-1x",
                },
            }
        ],
    }


def _load(tmp_path: Path, route: dict[str, Any]) -> Registry:
    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    bindings = tmp_path / "serving-bindings.json"
    bindings.write_text(
        json.dumps({"schema": SERVING_BINDINGS_SCHEMA, "catalog_digest": catalog.digest, "bindings": {}}) + "\n",
        encoding="utf-8",
    )
    routes = tmp_path / "lean-routes.json"
    routes.write_text(json.dumps(route) + "\n", encoding="utf-8")
    return Registry.load(
        CATALOG_ROOT,
        bindings,
        lean_routes_file=routes,
        repo_root=REPO_ROOT,
        evidence_root=None,
        max_attempts=3,
        max_gpu_seconds_per_attempt=3600,
        retry_base_seconds=1,
    )


def test_lean_route_binds_static_hot_qwen_service(tmp_path: Path) -> None:
    registry = _load(tmp_path, _route())
    model = registry.get("qwen3-8b")

    assert registry.catalog.routable_model_ids() == ("qwen3-8b",)
    assert model.lean_static is True
    assert model.binding.activation.enabled is False
    assert model.binding.service_origin == "http://qwen3-8b-b300.fs2-models.svc.cluster.local:8000"
    assert model.binding.backend_runtime_image_digest == model.gateway.runtime_image_digest
    assert model.gateway.support_state == "lean-live-verified"
    projected_states = {
        (desired, ready): derive_operational_model_state(
            model,
            sources_fresh=True,
            health_failure=False,
            activation_phase=AdminActivationPhase.NONE,
            desired_replicas=desired,
            ready_replicas=ready,
            queued_operations=0,
        )[0]
        for desired, ready in ((1, 1), (2, 1), (0, 0))
    }
    assert projected_states == {
        (1, 1): AdminModelState.HOT,
        (2, 1): AdminModelState.LOADING,
        (0, 0): AdminModelState.COLD,
    }
    assert model.gateway.mcp_invocable and model.binding.mcp_enabled
    assert model.valid_at(datetime.now(UTC))
    public = json.dumps(registry.render_runtime_config())
    assert model.binding.service_origin not in public
    assert "lean-static-hot-route" not in public


def test_empty_terraform_routes_leave_catalog_unroutable(tmp_path: Path) -> None:
    registry = _load(
        tmp_path,
        {"schema": "fs2-serve.nebius.ai/lean-routes/v4", "routes": []},
    )

    assert registry.catalog.routable_model_ids() == ()


@pytest.mark.parametrize(
    "document",
    (
        {"schema": "fs2-serve.nebius.ai/lean-routes/v2", "routes": []},
        {
            "schema": "fs2-serve.nebius.ai/lean-routes/v3",
            "routes": [],
            "qualification": {},
        },
    ),
)
def test_empty_legacy_routes_are_rejected(tmp_path: Path, document: dict[str, Any]) -> None:
    with pytest.raises(RegistryError, match="canonical gateway catalog validation failed") as error:
        _load(tmp_path, document)
    assert error.value.__cause__ is not None
    assert "lean route count is invalid" in str(error.value.__cause__)


def test_terraform_v4_route_uses_exact_eu_north1_h100_placement(tmp_path: Path) -> None:
    registry = _load(tmp_path, _cosmos_h100_route())
    model = registry.get("cosmos3-nano")

    assert model.binding.backend_region == "eu-north1"
    assert model.binding.backend_gpu_class == "nvidia-h100-sxm5-80gb"
    assert model.gateway.gpu_class == "nvidia-h100-sxm5-80gb"
    assert model.binding.backend_port == 8080
    assert _model_view(model)["gpu_class"] == "nvidia-h100-sxm5-80gb"
    assert AdminReadService._identity(model).gpu_class == "nvidia-h100-sxm5-80gb"
    rendered = Metrics(registry.list()).render()
    assert b'gpu_class="nvidia-h100-sxm5-80gb"' in rendered


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["routes"][0]["placement"].update(region="EU North 1"),
            "placement region is invalid",
        ),
        (
            lambda value: value["routes"][0]["placement"].update(accelerator_class="NVIDIA H100"),
            "placement accelerator class is invalid",
        ),
        (
            lambda value: value["routes"][0]["placement"].update(pool_id="H100/pool"),
            "placement pool ID is invalid",
        ),
        (
            lambda value: value["routes"][0].pop("placement"),
            "lean route 0 fields are invalid",
        ),
    ],
)
def test_terraform_v4_route_rejects_malformed_placement(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    route = _cosmos_h100_route()
    mutate(route)

    with pytest.raises(RegistryError, match="canonical gateway catalog validation failed") as error:
        _load(tmp_path, route)
    assert error.value.__cause__ is not None
    assert message in str(error.value.__cause__)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(qualification={}), "legacy lean route v2 cannot include qualification"),
        (
            lambda value: value.update(schema="fs2-serve.nebius.ai/lean-routes/v3"),
            "qualified lean route v3 requires qualification",
        ),
    ],
)
def test_lean_route_schema_versions_reject_contradictory_qualification_shape(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    route = _route()
    mutate(route)

    with pytest.raises(RegistryError, match="canonical gateway catalog validation failed") as error:
        _load(tmp_path, route)
    assert error.value.__cause__ is not None
    assert message in str(error.value.__cause__)


def test_lean_routes_bind_multiple_catalog_models_and_nim_revision(tmp_path: Path) -> None:
    route = _route()
    route["routes"].append(_boltz2_route())
    registry = _load(tmp_path, route)

    assert registry.catalog.routable_model_ids() == ("boltz2", "qwen3-8b")
    model = registry.get("boltz2")
    assert model.binding.service_origin == "http://boltz2-b300.fs2-models.svc.cluster.local:8000"
    assert model.binding.endpoints == {"native": "/biology/mit/boltz2/predict"}
    assert model.binding.mcp_tool_name == "boltz2_predict"


@pytest.mark.parametrize(("profile", "expected_count"), [("minimal", 1), ("full_catalog", 16)])
def test_terraform_route_sets_disable_the_activation_controller_handshake(
    tmp_path: Path, profile: str, expected_count: int
) -> None:
    inventory = json.loads((CONTROL_ROOT / "contracts/all-models-live-services.json").read_text(encoding="utf-8"))
    profiles = json.loads((SOLUTION_ROOT / "catalog/profiles/model-profiles.json").read_text(encoding="utf-8"))[
        "profiles"
    ]
    model_ids = profiles[profile]["canonical_routes"]
    routes = {
        "schema": (
            "fs2-serve.nebius.ai/lean-routes/v3" if profile == "full_catalog" else "fs2-serve.nebius.ai/lean-routes/v2"
        ),
        "routes": [
            {
                **inventory["routes"][model_id],
                "model_id": model_id,
                "service": {
                    **inventory["routes"][model_id]["service"],
                    "namespace": inventory["namespace"],
                },
            }
            for model_id in sorted(model_ids)
        ],
    }
    if profile == "full_catalog":
        catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
        routes["qualification"] = build_qualification_projection(
            catalog,
            routes["routes"],
            inventory["qualification"],
        )

    assert routes["schema"].endswith("/v3" if profile == "full_catalog" else "/v2")

    registry = _load(tmp_path, routes)
    selected = registry.list(enabled_only=True)
    assert len(selected) == expected_count
    assert {model.id for model in selected} == set(model_ids)
    assert all(model.lean_static for model in selected)
    assert all(not model.binding.activation.enabled for model in selected)
    assert activation_set(selected).required is False
    assert all((model.gateway.qualification is not None) == (profile == "full_catalog") for model in selected)
    if profile == "full_catalog":
        segment = registry.get("nv-segment-ct")
        public = _model_view(segment)
        admin = AdminReadService._identity(segment)
        assert mcp_model_view is _model_view
        assert public["active_runtime"] == admin.active_runtime.model_dump(mode="json")
        assert public["qualification"] == admin.qualification.model_dump(mode="json")
        assert public["policy"] == admin.policy.model_dump(mode="json")
        assert public["active_runtime"]["variant_id"] == ("nv-segment-ct-upstream-blackwell-sm103")
        assert public["active_runtime"]["kind"] == "independent-runtime"
        assert public["qualification"]["kind"] == "reviewed-evidence-snapshot"
        assert public["qualification"]["states"]["runtime_ready"] is True
        assert public["qualification"]["observed_at"] == inventory["qualification"]["observed_at"]
        assert public["policy"]["non_clinical"] is True
        cxr = _model_view(registry.get("nv-reason-cxr-3b"))
        assert cxr["policy"]["non_clinical"] is True
        assert cxr["policy"]["commercial_use"] == "prohibited"


def test_terraform_v4_full_catalog_uses_catalog_derived_route_ceiling(tmp_path: Path) -> None:
    inventory = json.loads((CONTROL_ROOT / "contracts/all-models-live-services.json").read_text(encoding="utf-8"))
    model_contract = json.loads((SOLUTION_ROOT / "catalog/profiles/model-profiles.json").read_text(encoding="utf-8"))
    model_ids = model_contract["profiles"]["full_catalog"]["canonical_routes"]
    routes = []
    for model_id in sorted(model_ids):
        deployment = model_contract["model_autoscaling_targets"][model_id]["deployment"]
        labels = model_contract["workload_placements"][deployment]["required_node_labels"]
        routes.append(
            {
                **inventory["routes"][model_id],
                "model_id": model_id,
                "service": {
                    **inventory["routes"][model_id]["service"],
                    "namespace": inventory["namespace"],
                },
                "placement": {
                    "region": "eu-north1",
                    "accelerator_class": labels["accelerator.fs2.nebius/class"],
                    "pool_id": labels.get("accelerator.fs2.nebius/pool-id"),
                },
            }
        )

    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    assert len(routes) == len(catalog.tested_model_ids)
    registry = _load(
        tmp_path,
        {"schema": "fs2-serve.nebius.ai/lean-routes/v4", "routes": routes},
    )
    assert len(registry.list(enabled_only=True)) == len(catalog.tested_model_ids)
    assert all(model.binding.backend_region == "eu-north1" for model in registry.list(enabled_only=True))
    assert registry.get("cosmos3-nano").gateway.gpu_class == "nvidia-b300-sxm6-288gb"


def test_lean_route_binds_exact_qualified_sm103_variant(tmp_path: Path) -> None:
    route = _route()
    route["routes"].append(_diffdock_variant_route())
    registry = _load(tmp_path, route)

    model = registry.get("diffdock")
    assert model.model_revision == "85c49b60d3e0b0182a59ee43a34a6d7036981284"
    assert model.gateway.runtime_image_digest == (
        "sha256:cb3875f7d66b8d170d0e3f16d3d9a63aee8d63fbb23fdf65ec7ea0214d849529"
    )
    assert model.binding.service_origin == "http://diffdock-b300.fs2-models.svc.cluster.local:8000"


def test_structure_manifests_bind_published_route_images() -> None:
    inventory = json.loads((CONTROL_ROOT / "contracts/all-models-live-services.json").read_text())

    manifest_paths = {
        model: SOLUTION_ROOT / f"models/structure/manifests/{model}.yaml" for model in ("diffdock", "proteinmpnn")
    }
    deployments = {}
    for model, path in manifest_paths.items():
        documents = [value for value in yaml.safe_load_all(path.read_text()) if value]
        deployments[model] = next(value for value in documents if value["kind"] == "Deployment")

    expected_diffdock = "sha256:cb3875f7d66b8d170d0e3f16d3d9a63aee8d63fbb23fdf65ec7ea0214d849529"
    expected_proteinmpnn = "sha256:13d195ac5e24ca9d75de058f08141ece37e60962dcdffb50b6d24ea474313d47"
    for model, expected in (("diffdock", expected_diffdock), ("proteinmpnn", expected_proteinmpnn)):
        runtime = next(
            value
            for value in deployments[model]["spec"]["template"]["spec"]["containers"]
            if value["name"] == "runtime"
        )
        assert runtime["image"].endswith("@" + expected)
        assert inventory["routes"][model]["runtime_image_digest"] == expected

    diffdock_runtime = deployments["diffdock"]["spec"]["template"]["spec"]["containers"][0]
    assert diffdock_runtime["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert diffdock_runtime["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert "exec" not in diffdock_runtime["readinessProbe"]
    assert "exec" not in diffdock_runtime["livenessProbe"]


def test_metrics_keep_non_routable_models_as_inventory(tmp_path: Path) -> None:
    registry = _load(tmp_path, _route())
    inventory = registry.get("nv-segment-ct", require_enabled=False)

    assert inventory.enabled is False
    revision = inventory.gateway.model_revision
    assert revision is not None

    rendered = Metrics(registry.list()).render()
    assert b'model="nv-segment-ct"' in rendered
    assert revision.encode() in rendered
    assert registry.get("qwen3-8b").model_revision.encode() in rendered


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.update(runtime_image_digest="vllm:latest"), "digest-pinned"),
        (lambda item: item.update(model_revision="0" * 40), "revision"),
        (lambda item: item["service"].update(namespace="default"), "valid Service in fs2-models"),
        (lambda item: item["service"].update(name="unowned-runtime"), "owned by its model ID"),
        (lambda item: item["service"].update(port=0), "service port is invalid"),
        (lambda item: item.update(storage_mode="ephemeral-localized"), "storage mode"),
        (lambda item: item.update(storage_mode="emptyDir"), "storage mode"),
        (lambda item: item.update(protocols={"openai-chat": "/generate"}), "canonical interface"),
        (lambda item: item.update(operations=["admin"]), "operations"),
        (lambda item: item["mcp"].update(description="token at https://private.invalid"), "description"),
    ],
)
def test_lean_route_rejects_authority_drift(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    route = _route()
    mutate(route["routes"][0])
    with pytest.raises(RegistryError, match="canonical gateway catalog validation failed") as error:
        _load(tmp_path, route)
    assert error.value.__cause__ is not None
    assert message in str(error.value.__cause__)


def test_lean_route_revalidation_withdraws_on_invalid_update(tmp_path: Path) -> None:
    catalog = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    bindings = tmp_path / "serving-bindings.json"
    bindings.write_text(
        json.dumps({"schema": SERVING_BINDINGS_SCHEMA, "catalog_digest": catalog.digest, "bindings": {}}) + "\n",
        encoding="utf-8",
    )
    routes = tmp_path / "lean-routes.json"
    routes.write_text(json.dumps(_route()) + "\n", encoding="utf-8")
    registry = Registry.load(
        CATALOG_ROOT,
        bindings,
        lean_routes_file=routes,
        repo_root=REPO_ROOT,
        evidence_root=None,
        max_attempts=3,
        max_gpu_seconds_per_attempt=3600,
        retry_base_seconds=1,
    )
    assert registry.get("qwen3-8b").enabled

    invalid = _route()
    invalid["routes"][0]["service"]["name"] = "QWEN"
    routes.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
    assert registry.revalidate() is False
    assert registry.validation_health()["healthy"] is False
    assert registry.list(enabled_only=True) == []
