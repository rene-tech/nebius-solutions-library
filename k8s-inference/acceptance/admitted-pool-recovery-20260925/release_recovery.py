#!/usr/bin/env python3
"""Apply only pool recovery code/config/RBAC to the exact current Helm chart."""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

NAME = "fs2-serve-control-plane"
CHART = "k8s-inference/charts/control-plane/fs2-serve-control-plane"
RECOVERY = {
    "failureConfirmationSeconds": 120,
    "admittedUnscheduledTimeoutSeconds": 7200,
    "backoffBaseSeconds": 15,
    "backoffMaxSeconds": 300,
}
ENV = {
    "FS2_SCIENTIFIC_POOL_FAILURE_CONFIRMATION_SECONDS": "120",
    "FS2_SCIENTIFIC_ADMITTED_UNSCHEDULED_TIMEOUT_SECONDS": "7200",
    "FS2_SCIENTIFIC_POOL_RECOVERY_BACKOFF_BASE_SECONDS": "15",
    "FS2_SCIENTIFIC_POOL_RECOVERY_BACKOFF_MAX_SECONDS": "300",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def extract(chart, root):
    root.mkdir(parents=True, exist_ok=False)
    (root / "Chart.yaml").write_text(yaml.safe_dump(chart["metadata"], sort_keys=False))
    (root / "values.yaml").write_text(yaml.safe_dump(chart.get("values", {}), sort_keys=False))
    for item in [*chart.get("templates", []), *chart.get("files", [])]:
        relative = PurePosixPath(item["name"])
        require(not relative.is_absolute() and ".." not in relative.parts, "invalid_chart_member")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(item["data"]))
    if chart.get("schema"):
        (root / "values.schema.json").write_bytes(base64.b64decode(chart["schema"]))
    for child in chart.get("dependencies", []):
        extract(child, root / "charts" / child["metadata"]["name"])


def resources(content):
    result = {}
    for obj in yaml.safe_load_all(content):
        if not obj:
            continue
        identity = (obj["kind"], obj["metadata"].get("namespace", "fs2-system"), obj["metadata"]["name"])
        require(identity not in result, "duplicate_resource")
        result[identity] = obj
    return result


def normalize(obj, image):
    if isinstance(obj, list):
        return [normalize(item, image) for item in obj]
    if not isinstance(obj, dict):
        return obj
    result = {}
    for key, value in obj.items():
        if key == "image" and value == image:
            result[key] = "EXACT_CONTROL_PLANE_IMAGE"
        elif key == "fs2.nebius.ai/image-digest" and value == image.split("@")[-1]:
            result[key] = "EXACT_CONTROL_PLANE_DIGEST"
        elif key == "env":
            for item in value:
                if item.get("name") in ENV:
                    require(item == {"name": item["name"], "value": ENV[item["name"]]}, "unexpected_recovery_env")
            result[key] = [normalize(item, image) for item in value if item.get("name") not in ENV]
        else:
            result[key] = normalize(value, image)
    return result


def validate_delta(old, new, old_image, new_image):
    role_key = ("Role", "kube-system", NAME + "-scientific-pool-health")
    binding_key = ("RoleBinding", "kube-system", NAME + "-scientific-pool-health")
    require(set(new) - set(old) == {role_key, binding_key} and not set(old) - set(new), "unexpected_resource_set")
    role = new[role_key]
    require(role["rules"] == [{"apiGroups": [""], "resources": ["configmaps"],
                               "resourceNames": ["cluster-autoscaler-status"], "verbs": ["get"]}], "unexpected_health_rbac")
    runtime_sa = old[("Deployment", "fs2-system", NAME)]["spec"]["template"]["spec"]["serviceAccountName"]
    binding = new[binding_key]
    require(binding["subjects"] == [{"kind": "ServiceAccount", "name": runtime_sa, "namespace": "fs2-system"}]
            and binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role_key[2]},
            "unexpected_health_subject")
    changes = []
    for key, prior in old.items():
        following = new[key]
        if prior == following:
            continue
        left, right = normalize(prior, old_image), normalize(following, new_image)
        if key == ("ClusterRole", "fs2-system", NAME + "-scientific-batch-flavors"):
            rules = right["rules"]
            nodes = [rule for rule in rules if rule["resources"] == ["nodes"]]
            require(len(nodes) == 1 and nodes[0]["verbs"] == ["get", "list"], "unexpected_node_permissions")
            nodes[0]["verbs"] = ["get"]
        require(left == right, "unrelated_resource_fields_changed:" + ":".join(key))
        changes.append(key)
    gateway = new[("Deployment", "fs2-system", NAME)]["spec"]["template"]["spec"]["containers"][0]
    require(gateway["image"] == new_image and ENV.items() <= {
        (item["name"], item.get("value")) for item in gateway["env"]
    }, "gateway_recovery_not_enabled")
    return changes + [role_key, binding_key]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "source-ref", "baseline-ref", "image"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    require(re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", args.image), "immutable_image_required")
    for ref in (args.source_ref, args.baseline_ref):
        require(re.fullmatch(r"[a-f0-9]{40}", ref), "exact_source_revision_required")
    args.output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[3]
    helm = ["helm", "--kubeconfig", args.kubeconfig, "--kube-context", args.context, "-n", "fs2-system"]
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "--request-timeout=30s"]
    history_cmd = helm + ["history", NAME, "--max", "1", "-o", "json"]
    history = json.loads(subprocess.check_output(history_cmd))
    require(len(history) == 1 and history[0]["status"] == "deployed", "release_not_stable")
    revision = history[0]["revision"]
    secret = json.loads(subprocess.check_output(kube + ["-n", "fs2-system", "get", "secret",
                         f"sh.helm.release.v1.{NAME}.v{revision}", "-o", "json"]))
    release = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(secret["data"]["release"]))))
    save(args.output / "release-private.json", release)
    chart = args.output / "chart"
    extract(release["chart"], chart)
    for name in ("templates/_helpers.tpl", "templates/scientific-batch-rbac.yaml", "values.schema.json"):
        baseline = subprocess.check_output(["git", "show", args.baseline_ref + ":" + CHART + "/" + name], cwd=repo)
        current = (chart / name).read_bytes()
        require(json.loads(current) == json.loads(baseline) if name.endswith(".json") else current == baseline,
                "live_chart_template_differs_from_reviewed_baseline:" + name)
        candidate = subprocess.check_output(["git", "show", args.source_ref + ":" + CHART + "/" + name], cwd=repo)
        (chart / name).write_bytes(candidate)
    defaults = yaml.safe_load((chart / "values.yaml").read_text())
    defaults["scientificBatch"]["poolRecovery"] = RECOVERY
    (chart / "values.yaml").write_text(yaml.safe_dump(defaults, sort_keys=False))
    values = copy.deepcopy(release["config"])
    old_image = values["image"]["repository"] + "@" + values["image"]["digest"]
    repository, image_digest = args.image.split("@")
    require(repository == values["image"]["repository"], "image_repository_changed")
    values["image"]["digest"] = image_digest
    values["scientificBatch"]["poolRecovery"] = RECOVERY
    # Preserve actual installed tools image, not a potentially empty caller default.
    prior_objects = resources(release["manifest"])
    prior_env = prior_objects[("Deployment", "fs2-system", NAME)]["spec"]["template"]["spec"]["containers"][0]["env"]
    tools_image = next(item["value"] for item in prior_env if item["name"] == "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE")
    require(re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", tools_image), "installed_tools_image_not_pinned")
    values["scientificBatch"]["toolsImage"] = tools_image
    save(args.output / "values-private.json", values)
    rendered = subprocess.check_output(helm + ["template", NAME, str(chart), "--is-upgrade", "-f", str(args.output / "values-private.json")])
    (args.output / "rendered-private.yaml").write_bytes(rendered)
    old = resources("\n---\n".join([release["manifest"], *(hook["manifest"] for hook in release.get("hooks", []))]))
    changed = validate_delta(old, resources(rendered), old_image, args.image)
    receipt = {"previous_revision": revision, "previous_image": old_image, "next_image": args.image,
               "source_revision": args.source_ref, "baseline_revision": args.baseline_ref,
               "preserved_scientific_tools_image": tools_image,
               "changed_resources": changed, "preserved_other_fields": True,
               "starter_pack_preserved": values["customerStorage"]["starterPack"] == release["config"]["customerStorage"]["starterPack"],
               "previous_chart_sha256": hashlib.sha256(json.dumps(release["chart"], sort_keys=True).encode()).hexdigest(),
               "rendered_sha256": hashlib.sha256(rendered).hexdigest(), "applied": False}
    save(args.output / "render-receipt.json", receipt)
    upgrade = helm + ["upgrade", NAME, str(chart), "-f", str(args.output / "values-private.json")]
    dry = subprocess.run(upgrade + ["--dry-run=server"], capture_output=True)
    (args.output / "dry-run-private.txt").write_bytes(dry.stdout + dry.stderr)
    require(dry.returncode == 0, "helm_server_dry_run_failed")
    if args.apply:
        require(json.loads(subprocess.check_output(history_cmd)) == history, "release_changed_recapture_required")
        def workload_snapshot():
            pods = json.loads(subprocess.check_output(kube + ["-n", "fs2-models", "get", "pods", "-o", "json"]))
            return [{"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                     "tenant": pod["metadata"].get("labels", {}).get("fs2.nebius.ai/tenant-id"),
                     "operation": pod["metadata"].get("labels", {}).get("fs2.nebius.ai/operation-id"),
                     "phase": pod.get("status", {}).get("phase"), "node": pod["spec"].get("nodeName")}
                    for pod in pods["items"]]
        save(args.output / "scientific-pods-before.json", workload_snapshot())
        receipt["apply_started_at"] = datetime.now(UTC).isoformat()
        applied = subprocess.run(upgrade + ["--wait", "--timeout", "10m", "--rollback-on-failure"], capture_output=True)
        (args.output / "apply-private.txt").write_bytes(applied.stdout + applied.stderr)
        require(applied.returncode == 0, "helm_recovery_rollout_failed")
        receipt.update(applied=True, new_revision=json.loads(subprocess.check_output(history_cmd))[0]["revision"],
                       apply_finished_at=datetime.now(UTC).isoformat())
        save(args.output / "scientific-pods-after.json", workload_snapshot())
        save(args.output / "applied-receipt.json", receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
