import copy
import hashlib
import json

import prepare as p
import pytest
import yaml
from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA

from fs2_serve.deployment_runtimes import SET_SCHEMA
from fs2_serve.registry import RegistryError


def bundle(model):
    docs = list(yaml.safe_load_all((p.ROOT / f"models/general-media/k8s/{model}.yaml").read_text()))
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    for container in [*pod["containers"], *pod["initContainers"]]:
        container["image"] = "registry.test/runtime@sha256:" + p.OLD[model]
    return {
        "modelRef": model,
        "runtimeProfile": "custom",
        "templateDigest": "sha256:" + ("a" if model == "evo2-40b" else "b") * 64,
        "primaryWorkloadName": model,
        "runtimeContainerName": "model" if model == "evo2-40b" else "server",
        "primaryServiceName": model,
        "primaryServicePort": 8000,
        "resources": [deployment],
    }


@pytest.mark.parametrize("model", p.NEW)
def test_templates_change_only_runtime_images_annotations_and_compiler_paths(model):
    before = bundle(model)
    original = copy.deepcopy(before)
    after = p.template(before, model)
    assert before == original
    assert after["templateDigest"] != before["templateDigest"]
    old_resource, new_resource = before["resources"][0], after["resources"][0]
    old_pod, new_pod = old_resource["spec"]["template"]["spec"], new_resource["spec"]["template"]["spec"]
    for old, new in zip(
        [*old_pod["containers"], *old_pod["initContainers"]],
        [*new_pod["containers"], *new_pod["initContainers"]],
        strict=True,
    ):
        assert new["image"] == p.NEW[model]
        new["image"] = old["image"]
        for old_env, new_env in zip(old.get("env", []), new.get("env", []), strict=True):
            if new_env["name"] in p.CACHE_ENV:
                assert p.NEW[model].split("@sha256:")[1] in new_env["value"]
                new_env["value"] = old_env["value"]
        assert new == old
    for old_meta, new_meta in (
        (old_resource["metadata"], new_resource["metadata"]),
        (old_resource["spec"]["template"]["metadata"], new_resource["spec"]["template"]["metadata"]),
    ):
        new_meta["annotations"]["fs2.nebius/runtime-image-digest"] = old_meta["annotations"][
            "fs2.nebius/runtime-image-digest"
        ]
    after["templateDigest"] = before["templateDigest"]
    assert after == before


def fixture():
    old = p.read(p.ROOT / "catalog/runtime/deployment-runtimes/evo2-40b-portable-h100.json")
    entries = {"evo2-40b": old}
    names = set(p.NEW) | p.existing.VOICES | {f"sibling-{i}" for i in range(13)}
    bundles = [bundle(model) for model in p.NEW]
    qualification = {
        model: {
            "runtimeImages": [],
            "templateDigests": [],
            "templateRefs": {},
            "templateCacheTiers": {},
            "gpuSnapshotBundles": {"old": {"retain": True}},
        }
        for model in names
    }
    owners, rows, successors = [], [], {}
    for model, template in zip(p.NEW, bundles, strict=True):
        digest = template["templateDigest"]
        qualification[model].update(
            runtimeImages=["registry.test/runtime@sha256:" + p.OLD[model]],
            templateDigests=[digest],
            templateRefs={"legacy": digest},
            templateCacheTiers={digest: "SharedFilesystem"},
        )
        row = copy.deepcopy(old["qualification"])
        row["model_id"] = model
        rows.append(row)
        base = old["record"] if model == "evo2-40b" else p.catalog().model(model).to_dict()
        successors[model] = p.successor(base, row, model, "a" * 64, p.SOURCE[model])
        owners.append(
            {
                "metadata": {"name": model, "namespace": "fs2-models"},
                "spec": {
                    "modelRef": model,
                    "runtime": {
                        "image": "registry.test/runtime@sha256:" + p.OLD[model],
                        "templateRef": {"name": "legacy", "digest": digest},
                    },
                    "cache": {
                        "tier": "SharedFilesystem",
                        "snapshotPreference": "Prefer",
                        "snapshotRef": {"name": "old"},
                    },
                    "fastStart": {"level": "Off"},
                    "availability": {"minReplicas": 1, "maxReplicas": 2 if model == "evo2-40b" else 4},
                    "placement": {
                        "poolRefs": ["h100-reserved-8x"] if model == "evo2-40b" else ["h100-1x", "h100-reserved-8x"]
                    },
                },
            }
        )
    routes = {
        "deployment-runtimes.json": json.dumps({"models": entries}),
        "qualification-projection.json": json.dumps({"rows": rows}),
        "lean-routes.json": "unchanged",
    }
    contracts = p.existing.catalog_configuration_contracts(p.catalog(), deployment_runtime_entries=entries)
    admin = {
        "models": {
            model: {
                "artifact": {field: getattr(contracts[model], attribute) for field, attribute in p.IDENTITIES.items()},
                "settings": {"maxReplicas": 1},
            }
            for model in p.NEW
        }
    }
    return {"revision": "old", "qualifications": qualification}, bundles, routes, admin, owners, successors


def test_both_successors_preserve_scaling_placement_siblings_and_historical_snapshots():
    inputs = fixture()
    original = copy.deepcopy(inputs)
    envelope, bundles, routes, admin, proposals = p.extend(*inputs)
    assert inputs == original and bundles[:-2] == original[1]
    assert routes["lean-routes.json"] == "unchanged"
    for model in envelope["qualifications"]:
        assert envelope["qualifications"][model]["gpuSnapshotBundles"] == {"old": {"retain": True}}
        if model not in p.NEW:
            assert envelope["qualifications"][model] == original[0]["qualifications"][model]
    for proposal in proposals:
        model, spec = proposal["name"], proposal["spec"]
        old = next(owner["spec"] for owner in original[4] if owner["metadata"]["name"] == model)
        assert spec["availability"] == old["availability"] and spec["placement"] == old["placement"]
        assert spec["cache"] == {"tier": "SharedFilesystem", "snapshotPreference": "Never"}
        assert spec["fastStart"]["level"] == "Off"
        assert admin["models"][model]["settings"] == {"maxReplicas": 1}
    assert len(json.loads(routes["deployment-runtimes.json"])["models"]) == 2
    for row in json.loads(routes["qualification-projection.json"])["rows"]:
        assert not row["states"]["cold_start_qualified"] and not row["states"]["http_mcp_qualified"]


@pytest.mark.parametrize(
    "mutation", ["one_successor", "missing_voice", "wrong_image", "stale_admin", "new_cold_claim", "cache_path"]
)
def test_reject_partial_or_drifted_inputs(mutation):
    values = fixture()
    if mutation == "one_successor":
        values[5].pop("nv-segment-ct")
    elif mutation == "missing_voice":
        values[0]["qualifications"].pop(next(iter(p.existing.VOICES)))
    elif mutation == "wrong_image":
        values[4][0]["spec"]["runtime"]["image"] = "unexpected"
    elif mutation == "stale_admin":
        values[3]["models"]["nv-segment-ct"]["artifact"]["image_digest"] = "stale"
    elif mutation == "new_cold_claim":
        values[5]["evo2-40b"]["qualification"]["states"]["cold_start_qualified"] = True
    else:
        pod = values[1][0]["resources"][0]["spec"]["template"]["spec"]
        next(env for env in pod["initContainers"][0]["env"] if env["name"] == "FS2_RUNTIME_CACHE_ROOT")["value"] = (
            "/different/cache"
        )
    with pytest.raises(ValueError):
        p.extend(*values)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def registry_inputs():
    """Real canonical records/qualification identities; only evidence is synthetic."""
    original = p.read(p.ROOT / "catalog/runtime/deployment-runtimes/evo2-40b-portable-h100.json")
    entries = {}
    catalog = p.catalog()
    digest = p.evidence_sha256({"test_fixture": "isolated-runtime-acceptance"})
    for model in p.NEW:
        record = original["record"] if model == "evo2-40b" else catalog.model(model).to_dict()
        row = copy.deepcopy(original["qualification"])
        row["model_id"] = model
        row["active_runtime"] = {
            "model_revision": record["model"]["source"]["revision"],
            "runtime_image_digest": record["runtime"]["image"]["digest"],
            "service": {"namespace": "fs2-models", "name": model, "port": 8000},
        }
        row["policy"] = {
            "license_id": record["model"]["source"]["license"]["id"],
            "non_clinical": record["interface"]["policy"]["non_clinical"],
            "commercial_use": record["interface"]["policy"]["commercial_use"],
        }
        entries[model] = p.successor(record, row, model, digest, p.SOURCE[model])
    directory = p.ROOT / "catalog/runtime"
    archival = p.existing.load_catalog(directory, repo_root=directory / "packaged-repository")
    bindings = {"schema": SERVING_BINDINGS_SCHEMA, "catalog_digest": archival.digest, "bindings": {}}
    routes = {
        "deployment-runtimes.json": json.dumps({"schema": SET_SCHEMA, "models": entries}),
        "lean-routes.json": json.dumps({"schema": "fs2-serve.nebius.ai/lean-routes/v4", "routes": []}),
    }
    return routes, bindings


@pytest.mark.parametrize("configmap", [False, True])
def test_actual_registry_startup_accepts_bare_evidence_digest_and_both_successors(configmap):
    routes, bindings = registry_inputs()
    if configmap:
        bindings = {"kind": "ConfigMap", "data": {"serving-bindings.json": json.dumps(bindings)}}
    checks = p.validate_registry(routes, bindings)
    assert checks["valid"] is True
    assert checks["selected_runtime_models"] == sorted(p.NEW)
    assert checks["registry_models"] >= len(p.NEW)
    evidence = {"test_fixture": "isolated-runtime-acceptance"}
    digest = p.evidence_sha256(evidence)
    assert digest == hashlib.sha256(p.canonical_json(evidence)).hexdigest()
    assert p.canonical_digest(evidence) == "sha256:" + digest


@pytest.mark.parametrize("model", p.NEW)
def test_actual_registry_startup_rejects_the_failed_release_prefixed_evidence_digest(model):
    routes, bindings = registry_inputs()
    runtimes = json.loads(routes["deployment-runtimes.json"])
    evidence = runtimes["models"][model]["qualification"]["evidence"]
    evidence["retained_deployments_sha256"] = "sha256:" + evidence["retained_deployments_sha256"]
    routes["deployment-runtimes.json"] = json.dumps(runtimes)
    with pytest.raises(RegistryError) as failure:
        p.validate_registry(routes, bindings)
    cause = failure.value
    while cause.__cause__ is not None:
        cause = cause.__cause__
    assert "retained_deployments_sha256" in str(cause)


def test_registry_preflight_does_not_replace_invalid_captured_bindings_with_empty_bindings():
    routes, bindings = registry_inputs()
    bindings["catalog_digest"] = hashlib.sha256(b"wrong-catalog").hexdigest()
    with pytest.raises(RegistryError):
        p.validate_registry(routes, bindings)


def test_registry_preflight_validates_the_captured_variant_promotions_input():
    routes, bindings = registry_inputs()
    captured = {
        "kind": "ConfigMap",
        "data": {
            "serving-bindings.json": json.dumps(bindings),
            "model-variant-promotions.json": json.dumps({"schema": "not-a-supported-promotion-schema"}),
        },
    }
    with pytest.raises(RegistryError) as failure:
        p.validate_registry(routes, captured)
    cause = failure.value
    while cause.__cause__ is not None:
        cause = cause.__cause__
    assert "promotion" in str(cause)


def test_ct_gate_requires_all_modes_two_matching_cohorts_and_exact_result_bytes(tmp_path):
    write(tmp_path / "identity.json", {"test_fixture": True})
    for cohort in ("r1b", "r2"):
        for scan in range(9):
            for mode in ("label", "point", "label-plus-point"):
                path = tmp_path / cohort / f"scan{scan}-{mode}"
                write(path / "result.json", {"test_fixture": True, "scan": scan})
                write(path / "evaluation.json", {"service_semantic_pass": True, "prompt_mode": mode})
                write(
                    path / "receipt.json",
                    {
                        "case_id": path.name,
                        "image": p.NEW["nv-segment-ct"],
                        "http_status": 200,
                        "service_semantic_pass": True,
                        "result_sha256": p.sha(path / "result.json"),
                    },
                )
    assert p.ct_evidence(tmp_path)["requests"] == 54
    write(tmp_path / "r2/scan0-point/result.json", {"mutated": True})
    with pytest.raises(ValueError, match="mismatched receipt"):
        p.ct_evidence(tmp_path)


@pytest.mark.parametrize("mutation", [None, "wrong_image", "health_failure", "overlap", "extra_gpu_call"])
def test_evo2_gate_binds_exact_image_health_and_serialized_actual_call_count(tmp_path, mutation):
    write(tmp_path / "v2-final-report.json", {"isolated_runtime_qualified": True, "requests": 96, "passed": 96})
    write(
        tmp_path / "v2-health-observations.json",
        {
            "replay_completed": True,
            "failed": int(mutation == "health_failure"),
            "observations": 200,
            "health_latency_max_seconds": 0.5,
        },
    )
    write(tmp_path / "v2-concurrency.json", {"passed": True})
    write(
        tmp_path / "v2-pod-final.json",
        {"spec": {"containers": [{"image": "old" if mutation == "wrong_image" else p.NEW["evo2-40b"]}]}},
    )
    for i in range(98 + int(mutation == "extra_gpu_call")):
        row = {"started_unix_seconds": i * 2, "finished_unix_seconds": i * 2 + 1, "active_model_requests_at_start": 1}
        if mutation == "overlap" and i == 97:
            row["started_unix_seconds"] = 0
        write(tmp_path / "v2-server-evidence-with-concurrency" / f"http-memory-{i:03}.json", row)
        if i < 96:
            write(tmp_path / "v2-server-evidence" / f"http-memory-{i:03}.json", row)
    if mutation:
        with pytest.raises(ValueError):
            p.evo2_evidence(tmp_path)
    else:
        assert p.evo2_evidence(tmp_path)["requests"] == 98
