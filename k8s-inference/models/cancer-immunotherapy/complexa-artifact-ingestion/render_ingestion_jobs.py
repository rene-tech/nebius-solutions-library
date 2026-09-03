#!/usr/bin/env python3
"""Render the Jobs that ingest pinned public checkpoints and publish them.

No project, region, registry, cluster, storage class, node pool, or GPU type is
written here. Every one of those arrives as a flag, so the same renderer serves
the proof-of-concept claim and whatever regional store replaces it.

Two modes, because ingress and the canonical store are two different volumes:

``stage``    downloads into a task-owned private directory on the ingress claim.
``promote``  copies the verified files onto the Terraform-managed reference
             plane, publishes them as content-addressed generations, and then
             releases the ingress copy.

Two properties of the ingress claim shape both pods and are not negotiable:

- Its volume driver is registered only on nodes carrying the storage capability
  label, so both jobs select on that label and tolerate no GPU taint. Neither
  is compute bound and neither has any business occupying an H100.
- The claim root is setgid and group-writable. The pods *join* that group and
  never set ``fsGroup``: Kubernetes applies fsGroup ownership to the whole
  volume rather than to the sub-path a pod mounts, so setting it here would
  recursively rewrite the ownership of the tenant-private AlphaFold 3 and
  PyRosetta trees that share the claim.

``promote`` additionally runs its two steps as two different accounts, because
the account that owns the ingress directories is not the account that owns the
reference plane. The copy runs as the reference-plane owner; releasing the
ingress runs as the ingress owner. Both reach the other side through a
supplementary group rather than by widening either directory.
"""

from __future__ import annotations

import argparse
import json
import sys

LABEL_PREFIX = "fs2.nebius.ai"
CLAIM_MOUNT = "/claim"
REFERENCE_MOUNT = "/reference"
TOOL_MOUNT = "/opt/fs2-ingestion"
VERIFIER_PARENT = "/opt/fs2-localization"
VERIFIER_PACKAGE = "fs2_localization"


def labels(run_id: str, role: str) -> dict[str, str]:
    return {
        f"{LABEL_PREFIX}/component": "complexa-artifact-ingestion",
        f"{LABEL_PREFIX}/role": role,
        f"{LABEL_PREFIX}/run-id": run_id,
    }


def container_security() -> dict[str, object]:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def resources(cpu: str, memory: str) -> dict[str, object]:
    return {"requests": {"cpu": cpu, "memory": memory}, "limits": {"cpu": cpu, "memory": memory}}


def job(name: str, namespace: str, run_id: str, role: str, deadline_seconds: int,
        pod: dict[str, object]) -> dict[str, object]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels(run_id, role)},
        "spec": {
            # One pod, resumed by hand if it dies: the tools are resumable, but a
            # second pod writing the same files concurrently is not safe.
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
            "activeDeadlineSeconds": deadline_seconds,
            "template": {"metadata": {"labels": labels(run_id, role)}, "spec": pod},
        },
    }


def stage_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    image: str,
    claim: str,
    config_map: str,
    staging_sub_path: str,
    artifact_ids: tuple[str, ...],
    node_selectors: dict[str, str],
    run_as_user: int,
    run_as_group: int,
    supplemental_group: int,
    cpu: str,
    memory: str,
    retries: int,
    deadline_seconds: int,
    continue_on_artifact_error: bool,
) -> dict[str, object]:
    staging_root = f"{CLAIM_MOUNT}/{staging_sub_path}"
    command = [
        "python3",
        f"{TOOL_MOUNT}/fetch_artifacts.py",
        "--contract", f"{TOOL_MOUNT}/ingestion-contract.json",
        "--staging-root", staging_root,
        "--receipt", f"{staging_root}/.receipts/staging.{run_id}.json",
        "--namespace", namespace,
        "--claim", claim,
        "--sub-path", staging_sub_path,
        "--retries", str(retries),
    ]
    for artifact_id in artifact_ids:
        command.extend(["--artifact-id", artifact_id])
    if continue_on_artifact_error:
        command.append("--continue-on-artifact-error")

    pod = {
        "restartPolicy": "Never",
        "nodeSelector": dict(node_selectors),
        "securityContext": {
            "runAsUser": run_as_user,
            "runAsGroup": run_as_group,
            "runAsNonRoot": True,
            "supplementalGroups": [supplemental_group],
        },
        "containers": [
            {
                "name": "stage",
                "image": image,
                "command": command,
                "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}],
                "volumeMounts": [
                    {"name": "claim", "mountPath": CLAIM_MOUNT},
                    {"name": "tool", "mountPath": TOOL_MOUNT, "readOnly": True},
                ],
                "resources": resources(cpu, memory),
                "securityContext": container_security(),
            }
        ],
        "volumes": [
            {"name": "claim", "persistentVolumeClaim": {"claimName": claim}},
            {"name": "tool", "configMap": {"name": config_map, "defaultMode": 0o555}},
        ],
    }
    return job(name, namespace, run_id, "stage", deadline_seconds, pod)


def promote_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    image: str,
    claim: str,
    config_map: str,
    verifier_config_map: str,
    staging_sub_path: str,
    host_root: str,
    tree_prefix: str,
    artifact_ids: tuple[str, ...],
    node_selectors: dict[str, str],
    reference_user: int,
    reference_group: int,
    ingress_user: int,
    ingress_group: int,
    supplemental_groups: tuple[int, ...],
    cpu: str,
    memory: str,
    deadline_seconds: int,
    reclaim: bool,
    dry_run_reclaim: bool,
) -> dict[str, object]:
    staging_root = f"{CLAIM_MOUNT}/{staging_sub_path}"
    generations_root = "/".join(part for part in (REFERENCE_MOUNT, tree_prefix, "generations") if part)
    receipt_dir = "/".join(part for part in (REFERENCE_MOUNT, tree_prefix, ".receipts") if part)
    promotion_receipt = f"{receipt_dir}/promotion.{run_id}.json"

    promote_command = [
        "python3",
        f"{TOOL_MOUNT}/promote_generations.py",
        "--contract", f"{TOOL_MOUNT}/ingestion-contract.json",
        "--staging-root", staging_root,
        "--generations-root", generations_root,
        "--localization-package-parent", VERIFIER_PARENT,
        "--tree-sub-path", f"{tree_prefix}/generations" if tree_prefix else "generations",
        "--volume-kind", "host-path",
        "--host-root", host_root,
        "--visibility", "public",
        "--allow-cross-filesystem-copy",
        "--receipt", promotion_receipt,
    ]
    for artifact_id in artifact_ids:
        promote_command.extend(["--artifact-id", artifact_id])

    reclaim_command = [
        "python3",
        f"{TOOL_MOUNT}/reclaim_staging.py",
        "--promotion-receipt", promotion_receipt,
        "--staging-root", staging_root,
        "--receipt", f"{staging_root}/.receipts/reclaim.{run_id}.json",
    ]
    if dry_run_reclaim:
        reclaim_command.append("--dry-run")

    tool_mounts = [
        {"name": "tool", "mountPath": TOOL_MOUNT, "readOnly": True},
        {"name": "verifier", "mountPath": f"{VERIFIER_PARENT}/{VERIFIER_PACKAGE}", "readOnly": True},
    ]

    containers = [
        {
            # Runs as the reference plane's owner, and reaches the ingress claim
            # through the claim's group. It never writes to the ingress side.
            "name": "promote",
            "image": image,
            "command": promote_command,
            "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}],
            "securityContext": {**container_security(), "runAsUser": reference_user,
                                "runAsGroup": reference_group},
            "volumeMounts": [
                {"name": "claim", "mountPath": CLAIM_MOUNT, "readOnly": True},
                {"name": "reference", "mountPath": REFERENCE_MOUNT},
                *tool_mounts,
            ],
            "resources": resources(cpu, memory),
        }
    ]
    if reclaim:
        containers.append(
            {
                # Runs as the ingress owner, which is the only account that may
                # delete there, and reads the reference plane through its group
                # to confirm the generation before releasing anything.
                "name": "reclaim",
                "image": image,
                "command": reclaim_command,
                "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}],
                "securityContext": {**container_security(), "runAsUser": ingress_user,
                                    "runAsGroup": ingress_group},
                "volumeMounts": [
                    {"name": "claim", "mountPath": CLAIM_MOUNT},
                    {"name": "reference", "mountPath": REFERENCE_MOUNT, "readOnly": True},
                    *tool_mounts,
                ],
                "resources": resources("200m", "256Mi"),
            }
        )

    pod = {
        "restartPolicy": "Never",
        "nodeSelector": dict(node_selectors),
        "securityContext": {
            "runAsNonRoot": True,
            "supplementalGroups": list(supplemental_groups),
        },
        # Sequential: nothing may be released until the generation it replaces
        # has actually been published, so the reclaim step is an init container
        # ordering problem solved by running the promote step first.
        "initContainers": [containers[0]],
        "containers": containers[1:] or [_noop_container(image, ingress_user, ingress_group)],
        "volumes": [
            {"name": "claim", "persistentVolumeClaim": {"claimName": claim}},
            {"name": "reference", "hostPath": {"path": host_root, "type": "Directory"}},
            {"name": "tool", "configMap": {"name": config_map, "defaultMode": 0o555}},
            {"name": "verifier", "configMap": {"name": verifier_config_map, "defaultMode": 0o555}},
        ],
    }
    return job(name, namespace, run_id, "promote", deadline_seconds, pod)


def reclaim_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    promotion_run_id: str,
    image: str,
    claim: str,
    config_map: str,
    staging_sub_path: str,
    host_root: str,
    tree_prefix: str,
    node_selectors: dict[str, str],
    ingress_user: int,
    ingress_group: int,
    supplemental_groups: tuple[int, ...],
    deadline_seconds: int,
    dry_run: bool,
) -> dict[str, object]:
    """Release the ingress copy on its own, against an existing promotion receipt.

    Deleting the ingress bytes is the one irreversible step in this pipeline and
    the copy that precedes it is the expensive one, so they are separable: a
    publication can be inspected before anything is released, without paying to
    copy twenty gigabytes a second time.
    """

    staging_root = f"{CLAIM_MOUNT}/{staging_sub_path}"
    receipt_dir = "/".join(part for part in (REFERENCE_MOUNT, tree_prefix, ".receipts") if part)
    command = [
        "python3",
        f"{TOOL_MOUNT}/reclaim_staging.py",
        "--promotion-receipt", f"{receipt_dir}/promotion.{promotion_run_id}.json",
        "--staging-root", staging_root,
        "--receipt", f"{staging_root}/.receipts/reclaim.{run_id}.json",
    ]
    if dry_run:
        command.append("--dry-run")

    pod = {
        "restartPolicy": "Never",
        "nodeSelector": dict(node_selectors),
        "securityContext": {"runAsNonRoot": True, "supplementalGroups": list(supplemental_groups)},
        "containers": [
            {
                "name": "reclaim",
                "image": image,
                "command": command,
                "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}],
                "securityContext": {**container_security(), "runAsUser": ingress_user,
                                    "runAsGroup": ingress_group},
                "volumeMounts": [
                    {"name": "claim", "mountPath": CLAIM_MOUNT},
                    {"name": "reference", "mountPath": REFERENCE_MOUNT, "readOnly": True},
                    {"name": "tool", "mountPath": TOOL_MOUNT, "readOnly": True},
                ],
                "resources": resources("200m", "256Mi"),
            }
        ],
        "volumes": [
            {"name": "claim", "persistentVolumeClaim": {"claimName": claim}},
            {"name": "reference", "hostPath": {"path": host_root, "type": "Directory"}},
            {"name": "tool", "configMap": {"name": config_map, "defaultMode": 0o555}},
        ],
    }
    return job(name, namespace, run_id, "reclaim", deadline_seconds, pod)


def _noop_container(image: str, run_as_user: int, run_as_group: int) -> dict[str, object]:
    """A Job needs one regular container even when the work is all in init.

    It carries an explicit account because the pod declares ``runAsNonRoot`` and
    the kubelet cannot confirm that for an image whose own user is root.
    """

    return {
        "name": "done",
        "image": image,
        "command": ["python3", "-c", "print('promotion complete')"],
        "securityContext": {**container_security(), "runAsUser": run_as_user,
                            "runAsGroup": run_as_group},
        "resources": resources("100m", "128Mi"),
    }


def selector_pairs(parser: argparse.ArgumentParser, items: list[str]) -> dict[str, str]:
    selectors: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator:
            parser.error(f"--node-selector expects KEY=VALUE, got {item!r}")
        selectors[key] = value
    return selectors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="mode", required=True)

    for mode in ("stage", "promote", "reclaim"):
        sub = modes.add_parser(mode)
        sub.add_argument("--name", required=True)
        sub.add_argument("--namespace", required=True)
        sub.add_argument("--run-id", required=True)
        sub.add_argument("--image", required=True, help="a stdlib-only Python image")
        sub.add_argument("--claim", required=True)
        sub.add_argument("--config-map", required=True)
        sub.add_argument("--staging-sub-path", required=True,
                         help="task-owned private sub-path inside the ingress claim")
        sub.add_argument("--artifact-id", action="append", default=[], dest="artifact_ids")
        sub.add_argument("--node-selector", action="append", default=[], dest="node_selectors",
                         metavar="KEY=VALUE")
        sub.add_argument("--memory", default="2Gi")
        if mode == "stage":
            sub.add_argument("--run-as-user", type=int, default=65532)
            sub.add_argument("--run-as-group", type=int, default=65532)
            sub.add_argument("--supplemental-group", type=int, default=65532)
            # Staging is network bound and streams in 8 MiB blocks, so it needs
            # one core for the SHA-256 pass and very little resident memory.
            # Asking for more only makes the pod unschedulable on the small,
            # shared, storage-capable CPU nodes.
            sub.add_argument("--cpu", default="1")
            sub.add_argument("--retries", type=int, default=8)
            sub.add_argument("--deadline-seconds", type=int, default=10800)
            sub.add_argument("--continue-on-artifact-error", action="store_true")
        elif mode == "reclaim":
            sub.add_argument("--promotion-run-id", required=True,
                             help="the run whose promotion receipt authorizes this release")
            sub.add_argument("--host-root", required=True)
            sub.add_argument("--tree-prefix", default="scientific-localization/public")
            sub.add_argument("--ingress-user", type=int, default=65532)
            sub.add_argument("--ingress-group", type=int, default=65532)
            sub.add_argument("--supplemental-group", action="append", type=int, default=[],
                             dest="supplemental_groups")
            sub.add_argument("--deadline-seconds", type=int, default=3600)
            sub.add_argument("--dry-run", action="store_true")
        else:
            sub.add_argument("--verifier-config-map", required=True,
                             help="ConfigMap holding the reviewed successor's fs2_localization package")
            sub.add_argument("--host-root", required=True,
                             help="Terraform-managed reference plane root on the node")
            sub.add_argument("--tree-prefix", default="scientific-localization/public")
            sub.add_argument("--reference-user", type=int, default=1000)
            sub.add_argument("--reference-group", type=int, default=1000)
            sub.add_argument("--ingress-user", type=int, default=65532)
            sub.add_argument("--ingress-group", type=int, default=65532)
            sub.add_argument("--supplemental-group", action="append", type=int, default=[],
                             dest="supplemental_groups")
            sub.add_argument("--cpu", default="1")
            sub.add_argument("--deadline-seconds", type=int, default=10800)
            sub.add_argument("--no-reclaim", action="store_true",
                             help="publish without releasing the ingress copy")
            sub.add_argument("--dry-run-reclaim", action="store_true")

    options = parser.parse_args(argv)
    selectors = selector_pairs(parser, options.node_selectors)

    if options.mode == "stage":
        rendered = stage_job(
            name=options.name, namespace=options.namespace, run_id=options.run_id,
            image=options.image, claim=options.claim, config_map=options.config_map,
            staging_sub_path=options.staging_sub_path.strip("/"),
            artifact_ids=tuple(options.artifact_ids), node_selectors=selectors,
            run_as_user=options.run_as_user, run_as_group=options.run_as_group,
            supplemental_group=options.supplemental_group, cpu=options.cpu, memory=options.memory,
            retries=options.retries, deadline_seconds=options.deadline_seconds,
            continue_on_artifact_error=options.continue_on_artifact_error,
        )
    elif options.mode == "reclaim":
        rendered = reclaim_job(
            name=options.name, namespace=options.namespace, run_id=options.run_id,
            promotion_run_id=options.promotion_run_id, image=options.image, claim=options.claim,
            config_map=options.config_map, staging_sub_path=options.staging_sub_path.strip("/"),
            host_root=options.host_root, tree_prefix=options.tree_prefix.strip("/"),
            node_selectors=selectors, ingress_user=options.ingress_user,
            ingress_group=options.ingress_group,
            supplemental_groups=tuple(options.supplemental_groups) or (options.ingress_group,),
            deadline_seconds=options.deadline_seconds, dry_run=options.dry_run,
        )
    else:
        groups = tuple(options.supplemental_groups) or (options.ingress_group, options.reference_group)
        rendered = promote_job(
            name=options.name, namespace=options.namespace, run_id=options.run_id,
            image=options.image, claim=options.claim, config_map=options.config_map,
            verifier_config_map=options.verifier_config_map,
            staging_sub_path=options.staging_sub_path.strip("/"),
            host_root=options.host_root, tree_prefix=options.tree_prefix.strip("/"),
            artifact_ids=tuple(options.artifact_ids), node_selectors=selectors,
            reference_user=options.reference_user, reference_group=options.reference_group,
            ingress_user=options.ingress_user, ingress_group=options.ingress_group,
            supplemental_groups=groups, cpu=options.cpu, memory=options.memory,
            deadline_seconds=options.deadline_seconds, reclaim=not options.no_reclaim,
            dry_run_reclaim=options.dry_run_reclaim,
        )

    json.dump(rendered, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
