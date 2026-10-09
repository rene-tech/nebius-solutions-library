"""Read-only final spec/qualification/key-policy checks after QA grant restoration."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from fs2_serve.model_deployment import ModelDeploymentSpec
from manage_release import DEPLOYMENTS, MODELS, admin, get


def main(args):
    receipt = {
        "at": datetime.now(UTC).isoformat(),
        "origin": args.origin,
        "apps": {},
        "deployments": {},
    }
    proposals = json.loads((args.directory / "proposals.json").read_text())
    for proposal in proposals:
        live = get(args, "modeldeployment", proposal["name"], "fs2-models")
        assert ModelDeploymentSpec.model_validate(
            live["spec"]
        ) == ModelDeploymentSpec.model_validate(proposal["spec"])
        receipt["apps"][proposal["name"]] = {
            "execution_spec_preserved": True,
            "phase": live["status"]["phase"],
            "availability": live["spec"]["availability"],
            "image": live["spec"]["runtime"]["image"],
        }
    for name in (*DEPLOYMENTS, "scientific-ai-website"):
        live = get(args, "deployment", name)
        assert live["status"]["readyReplicas"] == live["spec"]["replicas"]
        assert live["status"]["updatedReplicas"] == live["spec"]["replicas"]
        receipt["deployments"][name] = {
            "ready": live["status"]["readyReplicas"],
            "images": [
                item["image"] for item in live["spec"]["template"]["spec"]["containers"]
            ],
        }
    api = get(args, "deployment", DEPLOYMENTS[-1])
    for volume in api["spec"]["template"]["spec"]["volumes"]:
        config = volume.get("configMap", {})
        if not {item["key"] for item in config.get("items", [])}.intersection(
            {"infrastructure-envelope.json", "deployment-runtimes.json"}
        ):
            continue
        data = get(args, "configmap", config["name"])["data"]
        if "infrastructure-envelope.json" in data:
            envelope = json.loads(data["infrastructure-envelope.json"])
            for model in MODELS:
                assert envelope["qualifications"][model]["scaleToZeroQualified"]
            receipt["envelope"] = config["name"]
        if "deployment-runtimes.json" in data:
            selections = json.loads(data["deployment-runtimes.json"])
            for model in MODELS:
                assert all(
                    selections["models"][model]["qualification"]["states"].values()
                )
            receipt["qualified_selections"] = config["name"]
    before = json.loads((args.directory / "before-qa-grants.json").read_text())
    with admin(args) as client:
        response = client.get("/admin/api/v1/keys", params={"tenant_id": "system"})
        response.raise_for_status()
        key = next(
            item
            for item in response.json()["data"]["items"]
            if item["id"] == before["id"]
        )
        for field in ("models", "scopes", "max_concurrency", "expires_at"):
            assert key.get(field) == before.get(field), field
        receipt["qa_policy_restored"] = True
    probe = json.loads((args.directory / "publication-probe/cohort.json").read_text())
    assert probe["passed"] and len(probe["receipts"]) == 4
    receipt["post_publication_requests"] = [
        {
            "model": row["model"],
            "path": row["path"],
            "operation_id": row["operation_id"],
            "passed": row["passed"],
        }
        for row in probe["receipts"]
    ]
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "closeout_checks_passed": True,
                "qa_policy_restored": True,
                "model_specs_preserved": True,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
    )
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
