"""Exact native declarations/templates; no live traffic or hardware simulation."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog/runtime"
SOURCE = "3a3dbe0fdb258579f9d4eb1b35a2d1525f5fc0d3"

# Test the exact source that the wheel packages, not a previously installed
# development wheel (whose archival loader may predate this checkout).
sys.path.insert(0, str(ROOT / "components/control-plane/src"))
sys.path.insert(0, str(CATALOG))


def read(path: Path):
    return json.loads(path.read_text())


@pytest.mark.parametrize("model,device", [("phenoage", "cpu"), ("altumage", "cuda")])
def test_declaration_matches_exact_native_image_and_two_requests(model, device):
    declaration = read(CATALOG / f"native/{model}.json")
    selected = read(CATALOG / f"deployment-runtimes/{model}-{device}.json")
    receipt = read(ROOT / f"acceptance/aging-20260908/{model}-r01.json")
    record = declaration["record"]
    assert selected["record"] == record
    assert declaration["runtime_architecture"] == device
    assert record["runtime"]["image"]["reference"] == receipt["image"]
    assert record["evidence"][0]["source_commit"] == SOURCE
    assert [
        row["payload_sha256"] for row in declaration["semantic_requests"]["requests"]
    ] == [row["request_sha256"] for row in receipt["native_http_predictions"]]
    assert (
        declaration["semantic_requests"]["serialization"]
        == "sha256-json-compact-no-newline/v1"
    )
    assert selected["qualification"]["states"] == {
        "registered": True,
        "route_active": True,
        "runtime_ready": True,
        "semantic_qualified": True,
        "http_mcp_qualified": True,
        "cold_start_qualified": True,
        "elasticity_qualified": True,
    }
    public_path = ROOT / f"acceptance/aging-20260908/qualification-r05/{model}.json"
    public = read(public_path)
    evidence_hash = hashlib.sha256(public_path.read_bytes()).hexdigest()
    for field in (
        "audited_live_routes_sha256",
        "model_discovery_sha256",
        "mcp_discovery_sha256",
        "http_mcp_acceptance_sha256",
        "cold_start_acceptance_sha256",
        "elasticity_acceptance_sha256",
    ):
        assert selected["qualification"]["evidence"][field] == evidence_hash
    assert public["outcome"] == "passed" and public["logical_runs"] == 2
    assert (
        public["model_id"] == model and public["variant_id"] == selected["variant_id"]
    )
    assert public["runtime_image"] == record["runtime"]["image"]["reference"]
    assert public["model_source"] == record["model"]["source"]["revision"]
    assert (
        public["artifact_manifest_sha256"]
        == record["cache"]["artifact"]["manifest_digest"]
    )
    assert (
        public["record_sha256_canonical_json"]
        == hashlib.sha256(
            json.dumps(
                record, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )
    assert public["scope"]["replica_transition"] == "0-to-1-to-0"
    assert public["scope"]["maximum_configured_replicas"] == 1
    assert not public["scope"]["multi_replica_scale_out_qualified"]
    assert not public["scope"]["other_hardware_qualified"]
    assert public["scope"]["gpu_snapshot"] == (
        "not-applicable-cpu" if model == "phenoage" else "not-qualified"
    )
    assert public["temporary_key_revoked_and_denied"]
    for prefix in ("source", "fixture"):
        relative = record["semantic_validator"][f"{prefix}_path"]
        expected = record["semantic_validator"][f"{prefix}_sha256"]
        original = ROOT.parent / relative
        packaged = CATALOG / "packaged-repository" / relative
        assert original.read_bytes() == packaged.read_bytes()
        assert hashlib.sha256(packaged.read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("model", ["phenoage", "altumage"])
def test_artifact_manifest_has_actual_content_and_canonical_identity(model):
    declaration_path = CATALOG / f"native/{model}.json"
    declaration = read(declaration_path)
    reference = declaration["artifact_manifest"]
    path = declaration_path.parent / reference["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    manifest = read(path)
    assert manifest["owner"] == "runtime-image"
    assert manifest["content"]["expanded_bytes"] == sum(
        row["bytes"] for row in manifest["content"]["files"]
    )
    assert manifest["kind"] == ("formula" if model == "phenoage" else "weights")
    if model == "phenoage":
        assert manifest["content"]["files"] == [
            {
                "path": "aging/phenoage/runtime.py",
                "bytes": 2298,
                "sha256": "f30bf3dc5acdea8c1f6f7cc96be7c94da01262d5c1362d352c0bcbcddb495ba7",
            }
        ]
        assert "rene-tech/nebius-solutions-library" in manifest["source"]["uri"]
    else:
        assert manifest["content"]["expanded_bytes"] == 3567654
        assert {row["path"] for row in manifest["content"]["files"]} == {
            "weights.pt",
            "preprocessing.npz",
            "cpgs.json",
        }


@pytest.mark.parametrize(
    "model,cpu,memory,gpus",
    [("phenoage", 1000, 268435456, 0), ("altumage", 2000, 4294967296, 1)],
)
def test_native_template_matches_resources_and_routes(model, cpu, memory, gpus):
    documents = list(
        yaml.safe_load_all((ROOT / f"models/aging/k8s/{model}.yaml").read_text())
    )
    deployment, service = documents
    record = read(CATALOG / f"native/{model}.json")["record"]
    assert (
        deployment["metadata"]["namespace"]
        == service["metadata"]["namespace"]
        == "fs2-models"
    )
    assert deployment["spec"]["selector"]["matchLabels"] == service["spec"]["selector"]
    assert (
        deployment["spec"]["replicas"] == 0
    )  # Controller/bootstrap owns the real hot floor.
    pod = deployment["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == record["runtime"]["image"]["reference"]
    assert container["command"] == record["runtime"]["command"]
    assert (
        int(container["resources"]["requests"]["cpu"]) * 1000
        == record["resources"]["cpu_millis"]
        == cpu
    )
    assert record["resources"]["memory_bytes"] == memory
    assert int(container["resources"]["requests"].get("nvidia.com/gpu", 0)) == gpus
    assert int(container["resources"]["limits"].get("nvidia.com/gpu", 0)) == gpus
    assert container["readinessProbe"]["httpGet"] == {
        "path": "/v1/health/ready",
        "port": "http",
    }
    assert not pod["automountServiceAccountToken"]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    environment = {row["name"]: row["value"] for row in container["env"]}
    assert environment["AGING_DEVICE"] == ("cuda" if gpus else "cpu")
    if not gpus:
        assert pod["nodeSelector"]["capacity.fs2.nebius/pool-id"] == "batch-cpu"
        assert pod["tolerations"] == [
            {
                "key": "workload.fs2.nebius/general-cpu",
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]


def test_profile_schema_and_h100_only_binding():
    profile = read(ROOT / "catalog/profiles/model-profiles.json")
    Draft202012Validator(
        read(ROOT / "catalog/profiles/model-profiles.schema.json")
    ).validate(profile)
    assert profile["managed_native_model_ids"] == ["altumage", "phenoage"]
    assert profile["profiles"]["aging"]["canonical_routes"] == ["altumage", "phenoage"]
    compatibility = read(ROOT / "catalog/profiles/model-accelerator-compatibility.json")
    (binding,) = compatibility["models"]["altumage"]["runtimes"]["altumage-cuda-v1"][
        "bindings"
    ]
    assert binding["accelerator_class"] == "nvidia-h100-sxm5-80gb"
    path, expected = binding["evidence"].split("@sha256:")
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected
    assert "phenoage" not in compatibility["models"]
    assert set(profile["profiles"]["full_catalog"]["canonical_routes"]) == set(
        read(CATALOG / "catalog.json")["tested_model_ids"]
    )


def test_production_catalog_copy_loads_without_the_developer_checkout(tmp_path):
    from fs2_serve.native_catalog import augment_native_catalog
    from fs2_serve_catalog.loader import load_catalog

    # Materialize only the directories the production Dockerfile copies.
    root = tmp_path / "runtime-catalog"
    root.mkdir()
    for name in ("catalog.json", "pyproject.toml", "uv.lock"):
        shutil.copyfile(CATALOG / name, root / name)
    for name in (
        "contracts",
        "kubernetes",
        "models",
        "native",
        "deployment-runtimes",
        "schema",
        "sql",
        "validators",
        "packaged-repository",
    ):
        shutil.copytree(
            CATALOG / name, root / name, ignore=shutil.ignore_patterns("__pycache__")
        )
    archived = load_catalog(root, repo_root=root / "packaged-repository")
    native = augment_native_catalog(
        archived, root, repo_root=root / "packaged-repository"
    )
    assert native.digest == archived.digest
    assert set(native.records) == set(archived.records) | {"altumage", "phenoage"}
    dockerfile = (ROOT / "components/control-plane/Dockerfile").read_text()
    assert (
        "COPY k8s-inference/catalog/runtime/native /workspace/runtime-catalog/native"
        in dockerfile
    )
    assert (
        "COPY k8s-inference/catalog/runtime/deployment-runtimes /workspace/runtime-catalog/deployment-runtimes"
        in dockerfile
    )
    assert "assert native.digest == catalog.digest" in dockerfile


def test_stack_native_admin_bootstrap_without_archival_route_fabrication():
    loader = importlib.machinery.SourceFileLoader(
        "aging_stack_test", str(ROOT / "inference-stack")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    stack = importlib.util.module_from_spec(spec)
    loader.exec_module(stack)
    entries = {
        model: read(CATALOG / f"deployment-runtimes/{model}-{device}.json")
        for model, device in [("phenoage", "cpu"), ("altumage", "cuda")]
    }
    workload = {
        "model_image_overrides": {
            model: row["record"]["runtime"]["image"]["reference"]
            for model, row in entries.items()
        },
        "model_pool_overrides": {},
        "model_scaling_mode": "keda",
        "model_scaling_overrides": {},
        "hot_model_ids": ["altumage", "phenoage"],
        "keda_polling_interval_seconds": 5,
        "keda_cooldown_period_seconds": 300,
        "general_cpu_lane": {"local_queue": "general-cpu"},
    }
    dynamic = {
        "accelerator_pool_contract": {
            "pools": {
                "h100": {
                    "accelerator_class": "nvidia-h100-sxm5-80gb",
                    "capacity": {"type": "regular", "min_nodes": 1, "max_nodes": 2},
                    "node": {"gpus_per_node": 8},
                    "resource_api": {"resource_name": "nvidia.com/gpu"},
                    "scheduling": {"stable_node_labels": {}, "tolerations": []},
                }
            }
        },
        "general_cpu_pool_contract": {
            "schema": "fs2-serve.nebius.ai/general-cpu-pools/v1",
            "node_selector": {"workload.fs2.nebius/general-cpu": "true"},
            "taint": {
                "key": "workload.fs2.nebius/general-cpu",
                "value": "true",
                "effect": "NoSchedule",
            },
            "pools": {
                "batch-cpu": {
                    "capacity_type": "regular",
                    "min_nodes": 1,
                    "max_nodes": 2,
                    "schedulable_capacity": {
                        "cpu_millicores": 7000,
                        "memory_mib": 28672,
                    },
                }
            },
        },
    }
    baseline, digest, _ = stack.derived_admin_configuration(
        {"selected_model_ids": ["altumage", "phenoage"]}, dynamic, workload
    )
    from fs2_serve.configuration_models import PlatformConfiguration

    validated = PlatformConfiguration.model_validate(baseline)
    assert validated.model_dump(mode="json") == baseline
    assert digest == stack.canonical_sha256(baseline)
    assert baseline["models"]["phenoage"]["placement"]["accelerators"] == 0
    assert baseline["models"]["phenoage"]["placement"]["cpu_millis"] == 1000
    assert baseline["pools"]["batch-cpu"]["allocatable_memory_bytes"] == 30064771072
    assert baseline["models"]["phenoage"]["queue"]["local_queue"] == "general-cpu"
    assert baseline["models"]["altumage"]["placement"]["accelerators"] == 1
    for model in entries:
        assert baseline["models"][model]["mcp"] == {
            "exposed": True,
            "tool_name": f"infer_{model}",
        }
        assert (
            baseline["models"][model]["artifact"]["model_revision"]
            == entries[model]["record"]["model"]["source"]["revision"]
        )
    archived = read(stack.MODEL_ROUTES_PATH)
    assert not set(entries) & archived["routes"].keys()
