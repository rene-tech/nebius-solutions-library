"""Prepare and dry-run a GROMACS-only activation against the actual live API.

This tool never applies changes. It captures the live Deployment, mounted
ConfigMaps and scientific profiles, then writes content-addressed ConfigMaps,
an exact-template JSON patch, an inverse patch and a narrow Helm overlay.
The new API/tools image must package the source profiles used here; image
publication and customer/GPU qualification are separate release gates.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import runpy
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
NAMESPACE = "fs2-system"
NAME = "fs2-serve-control-plane"
REPO = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
SELECTED = frozenset({"gromacs", "gromacs-mpi"})
API_IMAGE = REPO + "@sha256:6a2876b380f43717ea37cb21dd4504af5884c9a305c486562a42ef103420f946"
TOOLS_IMAGE = REPO + "@sha256:e880bc5b9f2dae293ca0fe591b1f1c4bad31d55e7185bb06da355c0205d69109"
EXECUTION_CM = "fs2-r927c465c6d-scientific-execution-64277b315007"
SCHEDULING_CM = "fs2-scientific-scheduling-10e4fbdbd1b6"
PROOFS = runpy.run_path(str(ROOT / "models/molecular-dynamics/gromacs/activation/prepare.py"))
ROLLBACK_WARNING = (
    "Old readers reject stored execution-shape fields, including completed/cancelled records. "
    "The inverse old-image patch is safe only before the first shaped admission. "
    "After that, retain an updated reader; draining alone is insufficient. "
    "Durable-state compatibility remediation requires a separate reviewed procedure."
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    """Match the chart's Go/Sprig toJson bytes for ConfigMap content addresses."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    for char, escaped in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        raw = raw.replace(char, escaped)
    return raw.encode()


def indexed(rows, label):
    result = {row["model_id"]: row for row in rows}
    if len(result) != len(rows) or not SELECTED.issubset(result):
        raise ValueError(f"{label} has duplicate or missing GROMACS identities")
    return result


def profile_without_map_reference(profile):
    value = copy.deepcopy(profile)
    if isinstance(value.get("qualification"), dict):
        value["qualification"].pop("execution_map_sha256", None)
    return value


def merge_execution(live, live_profiles, source, source_profiles):
    """Preserve every unrelated live byte; never fabricate a qualification proof."""
    if live["schema"] != source["schema"] or live_profiles["schema"] != source_profiles["schema"]:
        raise ValueError("scientific contract schema changed")
    live_rows, source_rows = indexed(live["models"], "live map"), indexed(source["models"], "source map")
    old_profiles = indexed(live_profiles["profiles"], "live profiles")
    new_profiles = indexed(source_profiles["profiles"], "source profiles")
    if set(live_rows) != set(source_rows) or set(old_profiles) != set(new_profiles):
        raise ValueError("source/live unrelated model inventory differs")
    for model in set(live_rows) - SELECTED:
        if live_rows[model] != source_rows[model]:
            raise ValueError(f"source/live unrelated execution row differs: {model}")
    for model in set(old_profiles) - SELECTED:
        if profile_without_map_reference(old_profiles[model]) != profile_without_map_reference(new_profiles[model]):
            raise ValueError(f"source/live unrelated profile differs: {model}")
    PROOFS["validate_profile_qualifications"](live_profiles["profiles"], live)
    PROOFS["validate_profile_qualifications"](source_profiles["profiles"], source)
    desired = copy.deepcopy(live)
    desired["models"] = [
        copy.deepcopy(source_rows[row["model_id"]]) if row["model_id"] in SELECTED else row for row in desired["models"]
    ]
    desired["qualification_baselines"] = PROOFS["rebase_qualification_baselines"](live, desired, SELECTED)
    desired_rows = indexed(desired["models"], "desired map")
    # A source proof is reusable only when every referenced row is exactly the
    # source row it proves. The live snapshot registry is deliberately retained.
    for digest, ids in PROOFS["qualification_proofs"](source).items():
        if all(desired_rows.get(model) == source_rows[model] for model in ids):
            desired["qualification_baselines"][digest] = ids
    PROOFS["validate_profile_qualifications"](source_profiles["profiles"], desired)
    for model in SELECTED:
        identity = new_profiles[model]["execution_identity"]
        if desired_rows[model]["execution_identity_sha256"] != identity["execution_identity_sha256"]:
            raise ValueError(f"source runtime/profile identity differs: {model}")
        if identity["execution_identity_sha256"] != PROOFS["digest"](
            {key: value for key, value in identity.items() if key != "execution_identity_sha256"}
        ) or identity["workload_recipe_sha256"] != PROOFS["digest"](new_profiles[model]["workload"]):
            raise ValueError(f"source profile identity or workload recipe is stale: {model}")
        if any(
            not row["image"].endswith("@" + identity["runtime_image_digest"]) for row in desired_rows[model]["stages"]
        ):
            raise ValueError(f"source image differs from profile: {model}")
    return desired


def extend_mpi_pools(scheduling, source_profiles, requested):
    desired = copy.deepcopy(scheduling)
    current = desired["model_eligible_pool_ids"]["gromacs-mpi"]
    profile = indexed(source_profiles["profiles"], "source profiles")["gromacs-mpi"]
    shapes = profile["workload"]["stages"][0].get("execution_shapes", [])
    for pool in requested:
        counts = [
            s["placement"]["accelerator"]["count"] for s in shapes if pool in s["placement"]["accelerator"]["pool_ids"]
        ]
        capacity = desired.get("accelerator_node_capacity", {}).get(pool, {})
        if (
            not counts
            or pool not in desired["pools"]
            or desired["pools"][pool]["accelerator_resource_name"] != "nvidia.com/gpu"
            or capacity.get("accelerator_count", 0) < max(counts)
        ):
            raise ValueError(f"MPI pool lacks a declared shape or sufficient per-node GPU capacity: {pool}")
        if pool not in current:
            current.append(pool)
    return desired


def named(items, name, label):
    found = [item for item in items if item.get("name") == name]
    if len(found) != 1:
        raise ValueError(f"expected exactly one {label}: {name}")
    return found[0]


def select_api_pod(deployment, replicasets, pods, expected_image):
    owners = {
        row["metadata"]["uid"]
        for row in replicasets["items"]
        if any(
            owner.get("uid") == deployment["metadata"]["uid"] and owner.get("controller") is True
            for owner in row["metadata"].get("ownerReferences", [])
        )
    }
    candidates = [
        pod
        for pod in pods["items"]
        if not pod["metadata"].get("deletionTimestamp")
        and any(
            owner.get("uid") in owners and owner.get("controller") is True
            for owner in pod["metadata"].get("ownerReferences", [])
        )
        and any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])
        )
        and any(c["name"] == "control-plane" and c["image"] == expected_image for c in pod["spec"]["containers"])
    ]
    if not candidates:
        raise ValueError("no ready API Pod owned by the captured Deployment matches the expected image")
    return candidates[0]


def configmap(current, key, raw, annotation):
    digest = sha(raw)
    prefix = current["metadata"]["name"].rsplit("-", 1)[0]
    name = prefix[:50].rstrip("-") + "-" + digest[:12]
    metadata = {"name": name, "namespace": NAMESPACE}
    for field in ("labels", "annotations"):
        if current["metadata"].get(field):
            metadata[field] = copy.deepcopy(current["metadata"][field])
    metadata.setdefault("annotations", {})[annotation] = digest
    metadata["annotations"].pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": metadata,
        "immutable": True,
        "data": {key: raw.decode()},
    }


def prepare(
    deployment,
    execution_cm,
    scheduling_cm,
    live_profiles,
    source_map,
    source_profiles,
    *,
    image,
    tools_image,
    expected_api,
    expected_tools,
    expected_execution,
    expected_scheduling,
    enable_mpi_pools=(),
    mechanics_only=False,
):
    for value in (image, tools_image, expected_api, expected_tools):
        if re.fullmatch(re.escape(REPO) + r"@sha256:[a-f0-9]{64}", value) is None:
            raise ValueError("API/tools images require immutable digests in the existing repository")
    if image == expected_api and not mechanics_only:
        raise ValueError(
            "shape activation requires an updated API reader; use --mechanics-only for a no-release dry-run"
        )
    before = deployment["spec"]["template"]
    container = named(before["spec"]["containers"], "control-plane", "API container")
    env = {item["name"]: item for item in container["env"]}
    if len(env) != len(container["env"]):
        raise ValueError("API environment has duplicate names")
    if container["image"] != expected_api or env["FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE"].get("value") != expected_tools:
        raise ValueError("API/tools image baseline changed")
    for volume_name, cm, expected, key in (
        ("scientific-batch-execution", execution_cm, expected_execution, "execution-map.json"),
        ("scientific-batch-scheduling", scheduling_cm, expected_scheduling, "kueue-scheduling.json"),
    ):
        volume = named(before["spec"]["volumes"], volume_name, "ConfigMap volume")
        if (
            volume["configMap"]["name"] != expected
            or cm["metadata"]["name"] != expected
            or cm["metadata"]["namespace"] != NAMESPACE
            or cm.get("immutable") is not True
            or set(cm["data"]) != {key}
            or volume["configMap"].get("items") != [{"key": key, "path": key}]
        ):
            raise ValueError(f"ConfigMap baseline or mount changed: {volume_name}")
        if not expected.endswith("-" + sha(cm["data"][key].encode())[:12]):
            raise ValueError(f"ConfigMap name does not address its captured bytes: {volume_name}")
    scheduling_raw = scheduling_cm["data"]["kueue-scheduling.json"].encode()
    if env["FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256"].get("value") != sha(scheduling_raw):
        raise ValueError("mounted scheduling bytes differ from the Deployment digest")
    for field, expected in (
        ("FS2_CATALOG_DIR", "/opt/fs2/catalog"),
        ("FS2_SCIENTIFIC_BATCH_EXECUTION_MAP_FILE", "/etc/fs2-scientific-batch/execution-map.json"),
        ("FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_FILE", "/etc/fs2-scientific-batch/kueue-scheduling.json"),
    ):
        if env[field].get("value") != expected:
            raise ValueError(f"unexpected contract path: {field}")
    execution = merge_execution(
        json.loads(execution_cm["data"]["execution-map.json"]), live_profiles, source_map, source_profiles
    )
    scheduling = json.loads(scheduling_raw)
    updated_scheduling = extend_mpi_pools(scheduling, source_profiles, enable_mpi_pools)
    new_execution = configmap(
        execution_cm, "execution-map.json", canonical(execution), "fs2-serve.nebius.ai/execution-map-sha256"
    )
    new_scheduling = (
        configmap(
            scheduling_cm,
            "kueue-scheduling.json",
            canonical(updated_scheduling),
            "fs2-serve.nebius.ai/scheduling-contract-sha256",
        )
        if updated_scheduling != scheduling
        else copy.deepcopy(scheduling_cm)
    )
    schedule_digest = sha(new_scheduling["data"]["kueue-scheduling.json"].encode())
    desired = copy.deepcopy(before)
    candidate = named(desired["spec"]["containers"], "control-plane", "API container")
    candidate["image"] = image
    named(candidate["env"], "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE", "environment")["value"] = tools_image
    named(candidate["env"], "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256", "environment")["value"] = schedule_digest
    for name, cm in (("scientific-batch-execution", new_execution), ("scientific-batch-scheduling", new_scheduling)):
        named(desired["spec"]["volumes"], name, "volume")["configMap"]["name"] = cm["metadata"]["name"]
    annotations = desired.setdefault("metadata", {}).setdefault("annotations", {})
    annotations["fs2.nebius.ai/image-digest"] = image.rsplit("@", 1)[1]
    annotations["fs2.nebius.ai/scientific-execution-map-sha256"] = sha(canonical(execution))
    annotations["fs2.nebius.ai/scientific-scheduling-sha256"] = schedule_digest
    annotations["fs2.nebius.ai/scientific-profiles-sha256"] = sha(canonical(source_profiles))
    patch = [
        {"op": "test", "path": "/spec/template", "value": before},
        {"op": "replace", "path": "/spec/template", "value": desired},
    ]
    inverse = [
        {"op": "test", "path": "/spec/template", "value": desired},
        {"op": "replace", "path": "/spec/template", "value": before},
    ]
    overlay = {
        "image": {"repository": image.rsplit("@", 1)[0], "digest": image.rsplit("@", 1)[1]},
        "podAnnotations": {
            key: annotations[key]
            for key in (
                "fs2.nebius.ai/scientific-execution-map-sha256",
                "fs2.nebius.ai/scientific-scheduling-sha256",
                "fs2.nebius.ai/scientific-profiles-sha256",
            )
        },
        "scientificBatch": {
            "toolsImage": tools_image,
            "executionMap": execution,
            "executionMapConfigMapName": new_execution["metadata"]["name"].rsplit("-", 1)[0],
            "schedulingContractConfigMapName": new_scheduling["metadata"]["name"],
            "schedulingContractSha256": schedule_digest,
        },
    }
    cms = [new_execution]
    if updated_scheduling != scheduling:
        cms.append(new_scheduling)
    return {
        "patch": patch,
        "inverse": inverse,
        "configmaps": cms,
        "helm_overlay": overlay,
        "execution_map": execution,
        "profiles": source_profiles,
        "summary": {
            "selected_models": sorted(SELECTED),
            "enabled_mpi_pools": list(enable_mpi_pools),
            "api_image": image,
            "tools_image": tools_image,
            "applied": False,
            "mechanics_only": mechanics_only,
            "source_profiles_sha256": sha(canonical(source_profiles)),
            "execution_map_raw_sha256": sha(canonical(execution)),
            "scheduling_raw_sha256": schedule_digest,
            "image_catalog_verification": "required against the exact API/tools build before applying",
            "rollback": ROLLBACK_WARNING,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--tools-image", help="Defaults to the new API image")
    parser.add_argument("--expected-api-image", default=API_IMAGE)
    parser.add_argument("--expected-tools-image", default=TOOLS_IMAGE)
    parser.add_argument("--expected-execution-configmap", default=EXECUTION_CM)
    parser.add_argument("--expected-scheduling-configmap", default=SCHEDULING_CM)
    parser.add_argument("--enable-mpi-pool", action="append", choices=("l40s-1x", "l40s-4x"), default=[])
    parser.add_argument("--mechanics-only", action="store_true", help="Validate patch mechanics, not a release")
    parser.add_argument("--context", default=CONTEXT)
    parser.add_argument(
        "--source-map", type=Path, default=ROOT / "catalog/runtime/contracts/scientific-execution-map.json"
    )
    parser.add_argument(
        "--source-profiles", type=Path, default=ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", args.context, "--request-timeout=30s", "-n", NAMESPACE]

    def run(*arguments):
        # The executable and verbs are fixed here; operator arguments are separate argv entries, never shell text.
        return json.loads(subprocess.check_output([*kube, *arguments]))  # noqa: S603

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    deployment = run("get", "deployment", NAME, "-o", "json")
    save("before.deployment.json", deployment)
    captures = {}
    for label, volume_name in (
        ("execution", "scientific-batch-execution"),
        ("scheduling", "scientific-batch-scheduling"),
    ):
        name = named(deployment["spec"]["template"]["spec"]["volumes"], volume_name, "volume")["configMap"]["name"]
        captures[label] = run("get", "configmap", name, "-o", "json")
        save(f"before.{label}.configmap.json", captures[label])
    pods = run("get", "pods", "-l", "app.kubernetes.io/instance=" + NAME, "-o", "json")
    replicasets = run("get", "replicasets", "-l", "app.kubernetes.io/instance=" + NAME, "-o", "json")
    pod = select_api_pod(deployment, replicasets, pods, args.expected_api_image)
    live_profiles = run(
        "exec",
        pod["metadata"]["name"],
        "-c",
        "control-plane",
        "--",
        "cat",
        "/opt/fs2/catalog/contracts/scientific-workload-profiles.json",
    )
    save("before.scientific-profiles.json", live_profiles)
    save(
        "capture.json",
        {
            "context": args.context,
            "pod": pod["metadata"]["name"],
            "pod_uid": pod["metadata"]["uid"],
            "deployment_uid": deployment["metadata"]["uid"],
            "resource_version": deployment["metadata"]["resourceVersion"],
        },
    )
    bundle = prepare(
        deployment,
        captures["execution"],
        captures["scheduling"],
        live_profiles,
        json.loads(args.source_map.read_text()),
        json.loads(args.source_profiles.read_text()),
        image=args.image,
        tools_image=args.tools_image or args.image,
        expected_api=args.expected_api_image,
        expected_tools=args.expected_tools_image,
        expected_execution=args.expected_execution_configmap,
        expected_scheduling=args.expected_scheduling_configmap,
        enable_mpi_pools=args.enable_mpi_pool,
        mechanics_only=args.mechanics_only,
    )
    for filename, key in (
        ("patch.json", "patch"),
        ("rollback.before-shaped-admissions-only.json", "inverse"),
        ("helm-overlay.json", "helm_overlay"),
        ("execution-map.json", "execution_map"),
        ("scientific-profiles.json", "profiles"),
        ("summary.json", "summary"),
    ):
        save(filename, bundle[key])
    save("configmaps.json", {"apiVersion": "v1", "kind": "List", "items": bundle["configmaps"]})
    for index, cm in enumerate(bundle["configmaps"]):
        save(f"configmap-{index}.json", cm)
    for mode in ("client", "server"):
        results = []
        for index, cm in enumerate(bundle["configmaps"]):
            checked = run(
                "apply", "-f", str(args.output / f"configmap-{index}.json"), "--dry-run=" + mode, "-o", "json"
            )
            if checked["data"] != cm["data"] or checked.get("immutable") is not True:
                raise ValueError(f"{mode} dry-run changed the ConfigMap payload")
            results.append(checked)
        save(f"configmaps.{mode}-dry-run.json", results)
        result = run(
            "patch",
            "deployment",
            NAME,
            "--type=json",
            "--patch-file",
            str(args.output / "patch.json"),
            "--dry-run=" + mode,
            "-o",
            "json",
        )
        if result["spec"]["template"] != bundle["patch"][-1]["value"]:
            raise ValueError(f"{mode} dry-run changed the intended template")
        save(f"deployment.{mode}-dry-run.json", result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "server_dry_run": "passed",
                "applied": False,
                "mechanics_only": args.mechanics_only,
                "apply_order": ["configmaps.json", "patch.json"],
                "rollback_warning": bundle["summary"]["rollback"],
            }
        )
    )


if __name__ == "__main__":
    main()
