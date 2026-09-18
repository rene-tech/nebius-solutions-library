"""Prepare exact molecular runtime promotion from retained live contracts; never apply.

Uses the established voice/Cosmos immutable ConfigMap format and typed renderer.
Keep every existing template valid; GenMol alone needs a new template to avoid
reusing compiler caches whose paths embed the old image digest.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from fs2_serve_catalog.loader import load_catalog

from fs2_serve.configuration import (
    ConfigurationService,
    InMemoryConfigurationRepository,
    StaticCatalogConfigurationAdapter,
    catalog_configuration_contracts,
)
from fs2_serve.configuration_models import PlatformConfiguration
from fs2_serve.deployment_runtimes import deployment_runtime_configuration_identity
from fs2_serve.model_deployment import (
    InfrastructureEnvelope,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    RenderContext,
    ValidationDisposition,
    canonical_digest,
    canonical_json,
    validate_model_deployment,
)
from fs2_serve.model_deployment_controller import ControllerFiles
from fs2_serve.native_catalog import augment_native_catalog

ROOT = Path(__file__).resolve().parents[2]
OLD = {"genmol": "7a89a6f254e5a56dad391b8707315f8abd77d7138cacd585c706f63440463aaf",
       "proteinmpnn": "f27dda10178fb799dc8f75c2b7ced00643a6281fd3e37e95cd353700e6d62e38"}
NEW = {"genmol": "97c82cfacca3845ce3c9f50f430bb02b52747d97a76177294add531c42b26979",
       "proteinmpnn": "81acc477690e8c020e7499f474cd13c1cf4e21f1d210d10cda57f65d3793e227"}
VOICES = {"diar-streaming-sortformer-4spk-v2-1", "magpie-tts-multilingual-357m",
          "nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b", "parakeet-realtime-eou-120m-v1"}
CACHE_ENV = {"CUDA_CACHE_PATH", "HOME", "TORCH_EXTENSIONS_DIR", "TORCHINDUCTOR_CACHE_DIR",
             "TRITON_CACHE_DIR", "XDG_CACHE_HOME"}
TEMPLATE_NAME = "genmol.scientific-repair-20260918"

# Reuse the established Go/Helm resource JSON digest, not a second template format.
_spec = importlib.util.spec_from_file_location("fs2_existing_cosmos_contract",
    ROOT / "acceptance/cosmos-stockholm-deployment-20260917/prepare_contract.py")
assert _spec is not None and _spec.loader is not None
_prior = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _prior
_spec.loader.exec_module(_prior)


def genmol_template(previous, image):
    candidate = copy.deepcopy(previous)
    containers = [container for resource in candidate["resources"] if resource["kind"] == "Deployment"
                  for container in resource["spec"]["template"]["spec"]["containers"]
                  if container["name"] == previous["runtimeContainerName"]]
    if len(containers) != 1 or not containers[0]["image"].endswith("@sha256:" + OLD["genmol"]):
        raise ValueError("unexpected_genmol_primary_template")
    container = containers[0]
    container["image"] = image
    changed = set()
    for env in container.get("env", []):
        if env["name"] in CACHE_ENV:
            prefix = "/models/.fs2/runtime/" + OLD["genmol"] + "/"
            if not env.get("value", "").startswith(prefix):
                raise ValueError("unexpected_genmol_compiler_cache_path")
            env["value"] = env["value"].replace(prefix, "/models/.fs2/runtime/" + NEW["genmol"] + "/", 1)
            changed.add(env["name"])
    if changed != CACHE_ENV:
        raise ValueError("missing_genmol_compiler_cache_identity")
    candidate["templateDigest"] = _prior.terraform_digest(candidate["resources"])
    LegacyTemplateBundle.model_validate(candidate)
    return candidate


def extend(envelope, bundles, route_data, deployments, successors):
    before = copy.deepcopy((envelope, bundles, route_data, deployments))
    envelope, bundles, route_data = copy.deepcopy((envelope, bundles, route_data))
    models = set(envelope["qualifications"])
    if len(models) != 20 or not VOICES <= models or not set(NEW) <= models:
        raise ValueError("expected_complete_twenty_model_contract")
    runtimes = json.loads(route_data["deployment-runtimes.json"])
    projection = json.loads(route_data["qualification-projection.json"])
    proposals = []
    for model_id, digest in NEW.items():
        matches = [item for item in deployments if item["spec"]["modelRef"] == model_id]
        if len(matches) != 1:
            raise ValueError("expected_one_target_modeldeployment")
        item, successor = matches[0], successors[model_id]
        current = item["spec"]
        if not current["runtime"]["image"].endswith("@sha256:" + OLD[model_id]):
            raise ValueError("unexpected_current_runtime_image")
        if current["cache"]["snapshotPreference"] != "Never" or current["fastStart"]["level"] != "Off":
            raise ValueError("target_requires_reviewed_conventional_loading")
        image = current["runtime"]["image"].split("@")[0] + "@sha256:" + digest
        if (successor["record"]["runtime"]["image"]["reference"] != image
                or successor["qualification"]["active_runtime"]["runtime_image_digest"] != "sha256:" + digest):
            raise ValueError("successor_candidate_identity_mismatch")
        historical = runtimes["models"][model_id]
        for key in ("model", "cache", "resources", "interface", "startup"):
            if successor["record"][key] != historical["record"][key]:
                raise ValueError("non_runtime_record_contract_changed")
        flags = successor["qualification"]["states"]
        if (not all(flags[k] for k in ("registered", "runtime_ready", "semantic_qualified"))
                or any(flags[k] for k in ("route_active", "http_mcp_qualified", "cold_start_qualified",
                                          "elasticity_qualified"))):
            raise ValueError("unsupported_successor_qualification_claim")
        qualification = envelope["qualifications"][model_id]
        if image in qualification["runtimeImages"]:
            raise ValueError("candidate_already_present_inspect_live_state")
        qualification["runtimeImages"].append(image)
        proposal = {"name": item["metadata"]["name"], "namespace": item["metadata"]["namespace"],
                    "spec": copy.deepcopy(current)}
        proposal["spec"]["runtime"]["image"] = image
        if model_id == "genmol":
            template = current["runtime"]["templateRef"]["digest"]
            previous = next(b for b in bundles if b["modelRef"] == model_id and b["templateDigest"] == template)
            candidate = genmol_template(previous, image)
            bundles.append(candidate)
            candidate_digest = candidate["templateDigest"]
            qualification["templateDigests"].append(candidate_digest)
            qualification["templateRefs"][TEMPLATE_NAME] = candidate_digest
            qualification["templateCacheTiers"][candidate_digest] = qualification["templateCacheTiers"][template]
            proposal["spec"]["runtime"]["templateRef"] = {"name": TEMPLATE_NAME, "digest": candidate_digest}
        runtimes["models"][model_id] = copy.deepcopy(successor)
        rows = [row for row in projection["rows"] if row["model_id"] == model_id]
        if len(rows) != 1:
            raise ValueError("missing_exact_qualification_projection")
        rows[0].clear()
        rows[0].update(copy.deepcopy(successor["qualification"]))
        proposals.append(proposal)
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    route_data["deployment-runtimes.json"] = canonical_json(runtimes).decode()
    route_data["qualification-projection.json"] = canonical_json(projection).decode()
    assert bundles[:-1] == before[1]
    assert all(envelope["qualifications"][m] == before[0]["qualifications"][m] for m in models - set(NEW))
    return envelope, bundles, route_data, proposals


def validate_candidate(original, candidate, bundles, deployments, proposals):
    original, candidate = map(InfrastructureEnvelope.model_validate, (original, candidate))
    renderer = ControllerFiles(infrastructure_envelope=candidate,
        bundles=[LegacyTemplateBundle.model_validate(bundle) for bundle in bundles]).renderer()
    current_checks, proposed_checks = [], []
    for item in deployments:
        spec = ModelDeploymentSpec.model_validate(item["spec"])
        before, after = (validate_model_deployment(spec, envelope) for envelope in (original, candidate))
        if before.disposition != after.disposition:
            raise ValueError("current_modeldeployment_compatibility_changed")
        current_checks.append({"model": spec.model_ref, "before": before.disposition.value,
                               "after": after.disposition.value})
    for proposal in proposals:
        spec = ModelDeploymentSpec.model_validate(proposal["spec"])
        decision = validate_model_deployment(spec, candidate)
        if decision.disposition is not ValidationDisposition.ACCEPTED:
            raise ValueError("candidate_spec_not_accepted")
        plan = renderer.render(spec, RenderContext(name=proposal["name"], namespace=proposal["namespace"],
            generation=1, pool=candidate.pools[decision.admitted_pool_ref],
            eligible_pools=[candidate.pools[p] for p in spec.placement.pool_refs],
            prometheus_server_address="http://prometheus.fs2-observability.svc:9090", preview=True))
        containers = [container for resource in plan.resources if resource.kind == "Deployment"
                      for container in resource.manifest["spec"]["template"]["spec"]["containers"]
                      if container["image"] == spec.runtime.image]
        if not containers:
            raise ValueError("candidate_runtime_not_rendered")
        proposed_checks.append({"model": spec.model_ref, "disposition": decision.disposition.value,
                                "image": spec.runtime.image, "runtime_containers": len(containers)})
    return {"current_modeldeployments": current_checks, "proposed_renders": proposed_checks}


def configmap(prefix, data):
    digest = hashlib.sha256(canonical_json(data)).hexdigest()
    return {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"namespace": "fs2-system", "name": prefix + digest[:16],
                         "labels": {"workload.fs2.nebius/owner": "scientific-qualification-20260918"}}, "data": data}


def rebase_admin_configuration(configuration, previous_entries, successors):
    """Keep operator settings; align only the two immutable artifact identities."""
    result = copy.deepcopy(configuration)
    identity_fields = {
        "artifact_manifest_sha256": "artifact_manifest_sha256",
        "acquisition_contract_sha256": "acquisition_contract_sha256",
        "provenance_sha256": "provenance_sha256",
        "semantic_health_contract_sha256": "semantic_health_contract_sha256",
        "image_digest": "runtime_image_digest",
        "model_revision": "model_revision",
    }
    for model_id in NEW:
        old = deployment_runtime_configuration_identity(previous_entries[model_id])
        new = deployment_runtime_configuration_identity(successors[model_id])
        artifact = result["models"][model_id]["artifact"]
        for field, identity_field in identity_fields.items():
            if artifact[field] != old[identity_field]:
                raise ValueError("bootstrap_identity_changed_since_capture:" + model_id + ":" + field)
            artifact[field] = new[identity_field]
    return result


async def validate_admin_configuration(configuration, entries):
    """Exercise the actual gateway bootstrap boundary before draining an App."""
    catalog_dir = ROOT / "catalog/runtime"
    repo_root = catalog_dir / "packaged-repository"
    catalog = augment_native_catalog(load_catalog(catalog_dir, repo_root=repo_root), catalog_dir, repo_root=repo_root)
    desired = PlatformConfiguration.model_validate(configuration)
    service = ConfigurationService(
        repository=InMemoryConfigurationRepository(desired),
        catalog=StaticCatalogConfigurationAdapter(
            catalog_configuration_contracts(catalog, deployment_runtime_entries=entries)
        ),
    )
    validation = await service.validate_bootstrap(desired)
    if not validation.valid:
        errors = [issue.model_dump(mode="json") for issue in validation.issues if issue.severity == "error"]
        raise ValueError("gateway_bootstrap_rejected:" + json.dumps(errors, sort_keys=True))
    return validation.model_dump(mode="json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("live-configmaps", "live-routes", "live-admin-configuration", "modeldeployments", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    git = shutil.which("git")
    if git is None:
        raise ValueError("git_unavailable")
    successors = {}
    for model_id in NEW:
        path = ROOT / f"catalog/runtime/deployment-runtimes/{model_id}-portable-h100-20260918.json"
        command = [git, "-C", str(ROOT), "show", f"{args.source_commit}:k8s-inference/{path.relative_to(ROOT)}"]
        committed = subprocess.check_output(command)  # noqa: S603 - fixed Git source read, explicit operator revision
        if committed != path.read_bytes():
            raise ValueError("candidate_source_differs_from_commit")
        successors[model_id] = json.loads(committed)
    maps = json.loads(args.live_configmaps.read_bytes())["items"]
    def document(key):
        return json.loads(next(item["data"][key] for item in maps if key in item["data"]))
    original = document("infrastructure-envelope.json")
    deployments = json.loads(args.modeldeployments.read_bytes())["items"]
    routes = json.loads(args.live_routes.read_bytes())
    candidate, bundles, route_data, proposals = extend(original, document("renderer-bundles.json"),
                                                       routes["data"], deployments, successors)
    validation = validate_candidate(original, candidate, bundles, deployments, proposals)
    admin_map = json.loads(args.live_admin_configuration.read_bytes())
    old_configuration = json.loads(admin_map["data"]["admin-configuration.json"])
    new_configuration = rebase_admin_configuration(old_configuration,
        json.loads(routes["data"]["deployment-runtimes.json"])["models"], successors)
    validation["gateway_bootstrap"] = asyncio.run(validate_admin_configuration(
        new_configuration, json.loads(route_data["deployment-runtimes.json"])["models"]))
    objects = [configmap("fs2-science-envelope-", {"infrastructure-envelope.json": canonical_json(candidate).decode()}),
               configmap("fs2-science-bundles-", {"renderer-bundles.json": canonical_json(bundles).decode()}),
               configmap("fs2-science-routes-", route_data),
               configmap("fs2-science-admin-", {
                   "admin-configuration.json": canonical_json(new_configuration).decode()})]
    values = {"modelController": {"infrastructureEnvelopeConfigMapName": objects[0]["metadata"]["name"],
                                  "rendererBundlesConfigMapName": objects[1]["metadata"]["name"]},
              "catalog": {"leanRoutes": {"configMapName": objects[2]["metadata"]["name"]}},
              "adminConfiguration": {"configMapName": objects[3]["metadata"]["name"],
                  "sha256": hashlib.sha256(canonical_json(new_configuration)).hexdigest()}}
    receipt = {"applied": False, "source_commit": args.source_commit, "values": values, "validation": validation,
               "model_count": len(candidate["qualifications"]), "preserved_sibling_models": 18,
               "all_prior_bundles_preserved": True, "added_genmol_template": TEMPLATE_NAME,
               "genmol_compiler_cache_paths_rekeyed": sorted(CACHE_ENV),
               "new_images_have_snapshot_evidence": False,
               "input_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in (
                   ("live_configmaps", args.live_configmaps), ("live_routes", args.live_routes),
                   ("modeldeployments", args.modeldeployments),
                   ("live_admin_configuration", args.live_admin_configuration))},
               "configmaps": [{"name": obj["metadata"]["name"],
                               "data_sha256": hashlib.sha256(canonical_json(obj["data"])).hexdigest()}
                              for obj in objects]}
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    outputs = {"configmaps.json": {"apiVersion": "v1", "kind": "List", "items": objects},
               "values.json": values, "app-proposals.json": proposals, "validation.json": receipt,
               "rollback-app-proposals.json": [{"name": item["metadata"]["name"],
                   "namespace": item["metadata"]["namespace"], "spec": item["spec"]}
                   for item in deployments if item["spec"]["modelRef"] in NEW]}
    for name, value in outputs.items():
        (args.output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"applied": False, "values": values, "models": sorted(NEW)}))


if __name__ == "__main__":
    main()
