"""Prepare, never apply, the additive Cosmos revision in the retained voice contract.

Uses the existing speech/voice release format and CP typed contract/renderer.
Like nemotron-speech-20260916/prepare_template_update.py, keeps current template
references valid during the Helm handoff. Only private local artifacts are written.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from fs2_serve.model_deployment import (
    InfrastructureEnvelope,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    RenderContext,
    ValidationDisposition,
    canonical_digest,
    canonical_json,
    spec_digest,
    validate_model_deployment,
)
from fs2_serve.model_deployment_controller import ControllerFiles

MODEL = "cosmos3-nano"
OLD = "sha256:b2ce3b2351242330d1bb17f701968cc044bc244ff760567faaadbe915bb55214"
NEW = "sha256:a4c96a343622a0e809e61f132c71032dd6e845c9ea7d846563cdb0ce36cf0fab"
TEMPLATE_NAME = "cosmos3-nano.stockholm-v2"
VOICE_MODELS = {
    "diar-streaming-sortformer-4spk-v2-1", "magpie-tts-multilingual-357m",
    "nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b", "parakeet-realtime-eou-120m-v1",
}
TEMPLATE_FIELDS = {"templateDigests", "templateRefs", "templateCacheTiers"}

# Reuse the established Go/Helm JSON encoding helper for Terraform's resource
# digest (not CP canonical_json: adapter source contains HTML-sensitive bytes).
_helper_path = Path(__file__).resolve().parents[1] / "scientific-fleet/promote_qualifications.py"
_loader = importlib.util.spec_from_file_location("fs2_existing_qualification_promotion", _helper_path)
assert _loader is not None and _loader.loader is not None
_helper = importlib.util.module_from_spec(_loader)
sys.modules[_loader.name] = _helper
_loader.loader.exec_module(_helper)


def terraform_digest(resources):
    return "sha256:" + hashlib.sha256(_helper._helm_to_json_bytes(resources)).hexdigest()


def _index(bundles):
    result = {(b["modelRef"], b["templateDigest"]): b for b in bundles}
    if len(result) != len(bundles):
        raise ValueError("duplicate_template_identity")
    return result


def _without(value, keys):
    return {key: item for key, item in value.items() if key not in keys}


def merge_contract(live_envelope, live_bundles, before_envelope, before_bundles,
                   planned_envelope, planned_bundles, *, old=OLD, new=NEW):
    """Strictly preserve the complete live baseline and add one Cosmos revision."""
    original = copy.deepcopy((live_envelope, live_bundles))
    live, before, planned = map(_index, (live_bundles, before_bundles, planned_bundles))
    models = set(live_envelope["qualifications"])
    if len(models) != 20 or not VOICE_MODELS <= models or {key[0] for key in live} != models:
        raise ValueError("expected_complete_twenty_model_live_baseline")
    if set(before_envelope["qualifications"]) != models - VOICE_MODELS:
        raise ValueError("terraform_baseline_model_set_changed")
    if set(planned_envelope["qualifications"]) != set(before_envelope["qualifications"]):
        raise ValueError("terraform_plan_model_set_changed")
    if set(planned) != (set(before) - {(MODEL, old)}) | {(MODEL, new)}:
        raise ValueError("terraform_plan_template_set_changed")
    for identity, bundle in before.items():
        if live.get(identity) != bundle:
            raise ValueError("live_and_terraform_common_baselines_differ")
        if identity[0] != MODEL and planned.get(identity) != bundle:
            raise ValueError("unrelated_planned_bundle_change")
    for identity in before_envelope["qualifications"]:
        prior = before_envelope["qualifications"][identity]
        if live_envelope["qualifications"][identity] != prior:
            raise ValueError("live_and_terraform_qualification_baselines_differ")
        if identity != MODEL and planned_envelope["qualifications"][identity] != prior:
            raise ValueError("unrelated_planned_qualification_change")
    outside = {"qualifications", "revision"}
    if not (_without(live_envelope, outside) == _without(before_envelope, outside)
            == _without(planned_envelope, outside)):
        raise ValueError("infrastructure_or_customer_settings_changed")
    prior, proposed = (document["qualifications"][MODEL] for document in (live_envelope, planned_envelope))
    if _without(prior, TEMPLATE_FIELDS) != _without(proposed, TEMPLATE_FIELDS):
        raise ValueError("non_template_cosmos_qualification_changed")
    if proposed["templateDigests"] != [new] or set(proposed["templateRefs"].values()) != {new}:
        raise ValueError("unexpected_planned_cosmos_template")
    if terraform_digest(planned[(MODEL, new)]["resources"]) != new:
        raise ValueError("planned_cosmos_resource_digest_mismatch")
    envelope, bundles = copy.deepcopy(original)
    if TEMPLATE_NAME in prior["templateRefs"] or (MODEL, new) in live:
        raise ValueError("candidate_already_present_inspect_live_state")
    bundles.append(copy.deepcopy(planned[(MODEL, new)]))
    qualification = envelope["qualifications"][MODEL]
    qualification["templateDigests"].append(new)
    qualification["templateRefs"][TEMPLATE_NAME] = new
    qualification["templateCacheTiers"][new] = proposed["templateCacheTiers"][new]
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    files = ControllerFiles(infrastructure_envelope=InfrastructureEnvelope.model_validate(envelope),
                            bundles=[LegacyTemplateBundle.model_validate(b) for b in bundles])
    files.renderer()  # The exact serving loader's uniqueness checks.
    assert (live_envelope, live_bundles) == original
    assert bundles[:-1] == live_bundles
    assert all(envelope["qualifications"][m] == live_envelope["qualifications"][m] for m in models - {MODEL})
    return envelope, bundles


def validate_specs(live_envelope, candidate_envelope, bundles, deployments, *, new=NEW):
    original = InfrastructureEnvelope.model_validate(live_envelope)
    candidate = InfrastructureEnvelope.model_validate(candidate_envelope)
    renderer = ControllerFiles(infrastructure_envelope=candidate,
                               bundles=[LegacyTemplateBundle.model_validate(b) for b in bundles]).renderer()
    compatibility = []
    cosmos = []
    for item in deployments:
        spec = ModelDeploymentSpec.model_validate(item["spec"])
        if spec.model_ref not in candidate.qualifications:
            raise ValueError("current_modeldeployment_missing_from_candidate")
        before = validate_model_deployment(spec, original)
        after = validate_model_deployment(spec, candidate)
        if before.disposition != after.disposition:
            raise ValueError("current_modeldeployment_compatibility_changed")
        compatibility.append({"name": item["metadata"]["name"], "model": spec.model_ref,
                              "before": before.disposition.value, "after": after.disposition.value})
        if spec.model_ref == MODEL:
            cosmos.append(item)
    if len(cosmos) != 1:
        raise ValueError("expected_one_current_cosmos_modeldeployment")
    current = cosmos[0]
    proposal = {"name": current["metadata"]["name"], "namespace": current["metadata"]["namespace"],
                "spec": copy.deepcopy(current["spec"])}
    proposal["spec"]["runtime"]["templateRef"] = {"name": TEMPLATE_NAME, "digest": new}
    renders = {}
    for label, raw in (("current", current["spec"]), ("proposed", proposal["spec"])):
        spec = ModelDeploymentSpec.model_validate(raw)
        decision = validate_model_deployment(spec, candidate)
        if decision.disposition is not ValidationDisposition.ACCEPTED:
            raise ValueError("cosmos_spec_not_accepted")
        pool = candidate.pools[decision.admitted_pool_ref]
        plan = renderer.render(spec, RenderContext(
            name=proposal["name"], namespace=proposal["namespace"],
            generation=current["metadata"]["generation"] + (label == "proposed"),
            pool=pool, eligible_pools=[candidate.pools[p] for p in spec.placement.pool_refs],
            prometheus_server_address="http://prometheus.fs2-observability.svc:9090", preview=True,
        ))
        renders[label] = {"resource_count": len(plan.resources), "template": raw["runtime"]["templateRef"]}
    return proposal, {"current_modeldeployments": compatibility, "cosmos_renders": renders,
                      "source_uid": current["metadata"]["uid"], "source_generation": current["metadata"]["generation"],
                      "source_spec_digest": spec_digest(ModelDeploymentSpec.model_validate(current["spec"]))}


def immutable_configmap(prefix, key, document):
    # Identical data/name hashing and native Kubernetes format to voice onboarding.
    data = {key: canonical_json(document).decode()}
    identity = hashlib.sha256(canonical_json(data)).hexdigest()
    return {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"name": prefix + identity[:16], "namespace": "fs2-system",
                         "labels": {"workload.fs2.nebius/owner": "cosmos-stockholm-20260917"}}, "data": data}


def load_contracts(plan, live):
    records = {}
    for resource in plan["resource_changes"]:
        if resource["address"] == "kubernetes_config_map_v1.model_controller_envelope[0]":
            records["envelope"] = resource["change"]
        elif resource["address"] == "kubernetes_config_map_v1.model_controller_bundles[0]":
            records["bundles"] = resource["change"]
    if set(records) != {"envelope", "bundles"}:
        raise ValueError("missing_exact_plan_contract_resources")
    result = []
    for key in ("infrastructure-envelope.json", "renderer-bundles.json"):
        matches = [item for item in live["items"] if key in item.get("data", {})]
        if len(matches) != 1 or not matches[0].get("immutable"):
            raise ValueError("expected_one_immutable_live_contract")
        result.append(json.loads(matches[0]["data"][key]))
    for phase in ("before", "after"):
        result.extend(json.loads(records[kind][phase]["data"][key]) for kind, key in (
            ("envelope", "infrastructure-envelope.json"), ("bundles", "renderer-bundles.json")))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "live-configmaps", "modeldeployments", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    solution = Path(__file__).resolve().parents[2]
    relative_yaml = "k8s-inference/models/general-media/k8s/cosmos3-nano.yaml"
    git = shutil.which("git")
    if git is None:
        raise ValueError("git_executable_unavailable")
    source_commit = subprocess.check_output(  # noqa: S603 - git, explicit operator-provided source ref
        [git, "-C", str(solution), "rev-parse", args.source_commit], text=True
    ).strip()
    committed_yaml = subprocess.check_output(  # noqa: S603 - fixed repository-relative Cosmos source path
        [git, "-C", str(solution), "show", f"{source_commit}:{relative_yaml}"]
    )
    if committed_yaml != (solution / "models/general-media/k8s/cosmos3-nano.yaml").read_bytes():
        raise ValueError("committed_cosmos_source_differs_from_worktree")
    inputs = {name: path.read_bytes() for name, path in (
        ("plan", args.plan), ("live_configmaps", args.live_configmaps), ("modeldeployments", args.modeldeployments))}
    contracts = load_contracts(json.loads(inputs["plan"]), json.loads(inputs["live_configmaps"]))
    envelope, bundles = merge_contract(*contracts)
    proposal, validation = validate_specs(contracts[0], envelope, bundles,
                                          json.loads(inputs["modeldeployments"])["items"])
    objects = [immutable_configmap("fs2-cosmos-envelope-", "infrastructure-envelope.json", envelope),
               immutable_configmap("fs2-cosmos-bundles-", "renderer-bundles.json", bundles)]
    values = {"modelController": {"infrastructureEnvelopeConfigMapName": objects[0]["metadata"]["name"],
                                   "rendererBundlesConfigMapName": objects[1]["metadata"]["name"]}}
    receipt = {"applied": False, "model_count": len(envelope["qualifications"]), "bundle_revisions": len(bundles),
               "preserved_models": 19, "old_template": OLD, "new_template": NEW, "template_name": TEMPLATE_NAME,
               "envelope_revision": envelope["revision"], "values": values, "validation": validation,
               "source_commit": source_commit, "source_yaml_sha256": hashlib.sha256(committed_yaml).hexdigest(),
               "input_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in inputs.items()},
               "configmaps": [{"name": item["metadata"]["name"],
                                "data_sha256": hashlib.sha256(canonical_json(item["data"])).hexdigest()}
                               for item in objects]}
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    current = next(
        item for item in json.loads(inputs["modeldeployments"])["items"] if item["spec"]["modelRef"] == MODEL
    )
    rollback = {"name": proposal["name"], "namespace": proposal["namespace"], "spec": current["spec"]}
    for name, value in (("configmaps.json", {"apiVersion": "v1", "kind": "List", "items": objects}),
                        ("values.json", values), ("app-proposals.json", [proposal]),
                        ("rollback-app-proposals.json", [rollback]),
                        ("template-refs.json", {MODEL: proposal["spec"]["runtime"]["templateRef"]}),
                        ("validation.json", receipt)):
        (args.output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: receipt[key] for key in ("applied", "model_count", "bundle_revisions", "values")}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
        # Validation errors can embed input documents. Operator files stay private.
        raise SystemExit("Candidate preparation refused; inspect inputs privately.") from None
