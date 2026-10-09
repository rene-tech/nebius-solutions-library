"""Read-only, exact-cluster migration-38 proposal; deliberately has no apply mode.

Kubernetes objects stay in memory. Output contains only whitelisted metadata,
image patches, hashes, and Secret references, never Secret or environment values.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
NS = "fs2-system"
BASE = "fs2-serve-control-plane"
API = ("Deployment", NS, BASE)
CONTROLLER = ("Deployment", NS, BASE + "-model-controller")
MAINTENANCE = ("CronJob", NS, BASE + "-maintenance")
WORKSHOP = ("Deployment", NS, "fs2-mindeval-workshop")
DIRECT = {API, CONTROLLER, MAINTENANCE, WORKSHOP}
DB_CONTAINERS = {
    API: {("containers", "control-plane"), ("initContainers", "wait-schema")},
    CONTROLLER: {("containers", "model-controller")},
    MAINTENANCE: {("containers", "maintenance")},
    WORKSHOP: {("containers", "workshop")},
}
IMAGE_PATTERN = re.compile(
    r"cr\.eu-north1\.nebius\.cloud/e00akg9ndpx77eaexh/fs2-platform/"
    r"fs2-serve-control-plane@sha256:[0-9a-f]{64}"
)
ROLES = {
    "serve",
    "wait-schema",
    "model-controller",
    "maintenance",
    "migrate",
    "bootstrap-access",
    "gpu-allocation-observer",
}


def identity(obj):
    return obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"]


def pod_spec(obj):
    spec = obj["spec"]
    if obj["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def pod_path(obj):
    return (
        "/spec/"
        + ("jobTemplate/spec/" if obj["kind"] == "CronJob" else "")
        + "template/spec"
    )


def configmaps(obj):
    return {
        v["name"]: v["configMap"]["name"]
        for v in pod_spec(obj).get("volumes", [])
        if "configMap" in v
    }


def database_refs(container):
    refs = []
    for env in container.get("env", []):
        name = env["name"]
        if name in {
            "FS2_DATABASE_URL",
            "WORKSHOP_DATABASE_URL",
            "DATABASE_URL",
            "PGHOST",
        }:
            ref = env.get("valueFrom", {}).get("secretKeyRef")
            refs.append(
                {
                    "env": name,
                    "secret": ref.get("name") if ref else None,
                    "key": ref.get("key") if ref else None,
                }
            )
    return refs


def inventory(items):
    """Fail closed for unclassified shared-DB consumers, without exposing DSNs."""
    rows = []
    for obj in items:
        kind, namespace, name = key = identity(obj)
        spec = pod_spec(obj)
        application_database = any(
            env["name"] in {"FS2_DATABASE_URL", "WORKSHOP_DATABASE_URL"}
            for field in ("initContainers", "containers")
            for container in spec.get(field, [])
            for env in container.get("env", [])
        )
        if not namespace.startswith("fs2-") and not application_database:
            continue
        done = kind == "Job" and bool(obj.get("status", {}).get("completionTime"))
        separate = namespace == "fs2-stt-qualification-20260927"
        containers = []
        for field in ("initContainers", "containers"):
            for container in spec.get(field, []):
                refs = database_refs(container)
                if container.get("envFrom") and not separate:
                    raise ValueError(f"Unreviewed envFrom on {kind}/{namespace}/{name}")
                if refs and not separate:
                    known_job = kind == "Job" and (
                        name.startswith(BASE + "-maintenance-")
                        or name == "fs2-scientific-claim-index-20261005"
                        or re.fullmatch(r"fs2-schema38-migrate-[a-f0-9]{12}", name)
                    )
                    if key not in DIRECT and not known_job:
                        raise ValueError(
                            f"Unreviewed database consumer: {kind}/{namespace}/{name}"
                        )
                    if (
                        key in DB_CONTAINERS
                        and (field, container["name"]) not in DB_CONTAINERS[key]
                    ):
                        raise ValueError(
                            f"Unreviewed database sidecar: {kind}/{namespace}/{name}"
                        )
                    if any(
                        not str(ref["secret"] or "").startswith("fs2-serve-database")
                        for ref in refs
                    ):
                        raise ValueError(
                            f"Unreviewed database reference: {kind}/{namespace}/{name}"
                        )
                args = container.get("args", [])
                role = (
                    args[0] if args and args[0] in ROLES else "independent-entrypoint"
                )
                containers.append(
                    {
                        "field": field,
                        "name": container["name"],
                        "image": container["image"],
                        "role": role,
                        "database": refs,
                    }
                )
        if not separate and key not in DIRECT:
            # A new sidecar can mount a DSN without exposing it in an env var.
            mounted_db = any(
                str(volume.get("secret", {}).get("secretName", "")).startswith(
                    "fs2-serve-database"
                )
                or any(
                    str(source.get("secret", {}).get("name", "")).startswith(
                        "fs2-serve-database"
                    )
                    for source in volume.get("projected", {}).get("sources", [])
                )
                for volume in spec.get("volumes", [])
            )
            if mounted_db and not any(c["database"] for c in containers):
                raise ValueError(
                    f"Unreviewed mounted database reference: {kind}/{namespace}/{name}"
                )
        if separate:
            action = "preserve: separate STT database"
        elif key == API:
            action = "stage main and strict wait-schema init together"
        elif key in {CONTROLLER, MAINTENANCE}:
            action = "update coherent direct-DB reader; no strict ledger gate"
        elif key == WORKSHOP:
            action = "preserve: independent workshop schema in shared database"
        elif any(c["database"] for c in containers):
            action = (
                "preserve completed Job"
                if done
                else "wait for one-shot database Job to finish"
            )
        else:
            action = "preserve: no direct database credential or strict ledger gate"
        rows.append(
            {
                "kind": kind,
                "namespace": namespace,
                "name": name,
                "uid": obj["metadata"]["uid"],
                "action": action,
                "active": obj.get("status", {}).get("active", 0),
                "completed": done,
                "containers": containers,
                "configmaps": configmaps(obj),
            }
        )
    if not DIRECT.issubset({identity(obj) for obj in items}):
        raise ValueError("An expected shared-database deployment is missing")
    return rows


def tests_for(obj):
    return [
        {"op": "test", "path": "/metadata/" + field, "value": obj["metadata"][field]}
        for field in ("uid", "resourceVersion")
    ]


def image_patch(obj, field, name, image):
    containers = pod_spec(obj).get(field, [])
    matches = [(i, c) for i, c in enumerate(containers) if c["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {field}/{name}")
    index, container = matches[0]
    path = f"{pod_path(obj)}/{field}/{index}"
    return [
        {"op": "test", "path": path + "/name", "value": name},
        {"op": "test", "path": path + "/image", "value": container["image"]},
        {"op": "replace", "path": path + "/image", "value": image},
    ]


def original_flag(obj, name):
    return {"present": name in obj["spec"], "value": obj["spec"].get(name)}


def preserved_hash(obj):
    """Hash all spec fields except the exact permitted image/flag changes."""
    normalized = copy.deepcopy(obj)
    key = identity(obj)
    if key == API:
        normalized["spec"].pop("paused", None)
    if key == MAINTENANCE:
        normalized["spec"].pop("suspend", None)
    for field, container_name in (
        (("containers", "control-plane"), ("initContainers", "wait-schema"))
        if key == API
        else (
            ("containers", "model-controller" if key == CONTROLLER else "maintenance"),
        )
    ):
        for container in pod_spec(normalized).get(field, []):
            if container["name"] == container_name:
                container["image"] = "<release-image>"
                if key == API and field == "containers":
                    for env in container.get("env", []):
                        if env["name"] == "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE":
                            env["value"] = "<release-image>"
    return hashlib.sha256(
        json.dumps(normalized["spec"], sort_keys=True).encode()
    ).hexdigest()


def stage(obj, image):
    key = identity(obj)
    patch = tests_for(obj)
    flag = "paused" if key == API else "suspend" if key == MAINTENANCE else None
    if flag:
        patch.append({"op": "add", "path": "/spec/" + flag, "value": True})
    if key == API:
        patch += image_patch(obj, "containers", "control-plane", image)
        patch += image_patch(obj, "initContainers", "wait-schema", image)
        for index, container in enumerate(pod_spec(obj)["containers"]):
            if container["name"] != "control-plane":
                continue
            for env_index, env in enumerate(container.get("env", [])):
                if env["name"] == "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE":
                    if not IMAGE_PATTERN.fullmatch(env.get("value", "")):
                        raise ValueError(
                            "Future scientific tools must use an immutable reviewed image"
                        )
                    path = f"{pod_path(obj)}/containers/{index}/env/{env_index}/value"
                    patch += [
                        {"op": "test", "path": path, "value": env["value"]},
                        {"op": "replace", "path": path, "value": image},
                    ]
                    break
            else:
                raise ValueError("Missing future scientific tools image binding")
    else:
        patch += image_patch(
            obj,
            "containers",
            "model-controller" if key == CONTROLLER else "maintenance",
            image,
        )
    return {
        "kind": key[0],
        "namespace": key[1],
        "name": key[2],
        "patch": patch,
        "preserved_spec_sha256": preserved_hash(obj),
        "original_flag": {"name": flag, **original_flag(obj, flag)} if flag else None,
        "configmaps": configmaps(obj),
    }


def restore_patch(fresh, prepared, image):
    """Construct a fresh-RV flag restore only after verifying the staged spec."""
    if identity(fresh) != (prepared["kind"], prepared["namespace"], prepared["name"]):
        raise ValueError("Restore target identity differs")
    if fresh["metadata"]["uid"] != prepared["patch"][0]["value"]:
        raise ValueError("Restore target UID differs")
    if preserved_hash(fresh) != prepared["preserved_spec_sha256"]:
        raise ValueError("Non-image specification changed since staging")
    for change in prepared["patch"]:
        if change["op"] == "replace":
            value = fresh
            for part in change["path"].strip("/").split("/"):
                value = value[int(part)] if isinstance(value, list) else value[part]
            if value != image:
                raise ValueError("Candidate image not fully staged")
    flag = prepared["original_flag"]
    if not flag or fresh["spec"].get(flag["name"]) is not True:
        raise ValueError("Target is not held for this release")
    path = "/spec/" + flag["name"]
    restore = (
        {"op": "replace", "path": path, "value": flag["value"]}
        if flag["present"]
        else {"op": "remove", "path": path}
    )
    return [*tests_for(fresh), {"op": "test", "path": path, "value": True}, restore]


def migration_job(image):
    job = yaml.safe_load(
        (
            ROOT / "acceptance/customer-workbenches-20261002/migration-job.yaml"
        ).read_text()
    )
    job["metadata"]["name"] = "fs2-schema38-migrate-" + image.rsplit(":", 1)[1][:12]
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["image"] = image
    container["command"] = ["python", "-c"]
    container["args"] = [(HERE / "guarded_migrate.py").read_text()]
    return job


def prepare(items, image):
    if not IMAGE_PATTERN.fullmatch(image):
        raise ValueError(
            "Candidate must be an immutable control-plane digest in the existing registry"
        )
    rows = inventory(items)
    objects = {identity(obj): obj for obj in items}
    return {
        "schema": "fs2-schema38-review-only-v1",
        "context": CONTEXT,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "candidate_image": image,
        "inventory": rows,
        "stage_in_order": [
            stage(objects[key], image) for key in (MAINTENANCE, API, CONTROLLER)
        ],
        "migration_job": migration_job(image),
        "restore_requires_fresh_read": True,
        "rdma_publication_requires_all_reader_barrier": True,
    }


def prepare_restore(items, prepared, image, target):
    if prepared["context"] != CONTEXT or prepared["candidate_image"] != image:
        raise ValueError("Prepared release identity differs")
    if target not in {"api", "maintenance"}:
        raise ValueError("Select exactly one restore target: api or maintenance")
    inventory(items)
    key = API if target == "api" else MAINTENANCE
    matches = [
        entry
        for entry in prepared["stage_in_order"]
        if (entry["kind"], entry["namespace"], entry["name"]) == key
    ]
    if len(matches) != 1:
        raise ValueError("Prepared release must contain exactly one selected target")
    objects = {identity(obj): obj for obj in items}
    return {
        "review_only": True,
        "restore_target": target,
        "restore": [
            {
                "kind": key[0],
                "namespace": key[1],
                "name": key[2],
                "patch": restore_patch(objects[key], matches[0], image),
            }
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write a new sanitized review artifact; never overwrite",
    )
    parser.add_argument(
        "--restore-from",
        type=Path,
        help="Emit fresh-RV restore proposals after staging; still never applies",
    )
    parser.add_argument(
        "--restore-target",
        choices=("api", "maintenance"),
        help="Required with --restore-from; each release phase restores only its selected target",
    )
    args = parser.parse_args()
    if bool(args.restore_from) != bool(args.restore_target):
        parser.error("--restore-from and --restore-target must be supplied together")
    items = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "--context",
                CONTEXT,
                "--request-timeout=30s",
                "get",
                "deployments,cronjobs,jobs,daemonsets,statefulsets",
                "-A",
                "-o",
                "json",
            ]
        )
    )["items"]
    if args.restore_from:
        prepared = json.loads(args.restore_from.read_text())
        output = prepare_restore(
            items, prepared, args.candidate_image, args.restore_target
        )
    else:
        output = prepare(items, args.candidate_image)
    if args.output:
        with args.output.open("x") as handle:
            handle.write(json.dumps(output, indent=2) + "\n")
        print(json.dumps({"review_only": True, "output": str(args.output)}))
    else:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
