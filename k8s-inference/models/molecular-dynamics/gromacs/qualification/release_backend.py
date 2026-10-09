"""Apply an exact additive GROMACS release only if captured live values still match.

Normal Helm release, existing Secret references, no quota or node-group changes.
Render output is private because operator values must not be printed to stdout.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--activation", type=Path, required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--admin-image-digest")
    parser.add_argument("--admin-source-commit")
    parser.add_argument("--admin-source-tree")
    parser.add_argument("--admin-sbom-file", type=Path)
    parser.add_argument(
        "--backend-only",
        action="store_true",
        help="Preserve every execution/profile row while repairing result publication.",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image_digest):
        raise ValueError("Use an immutable published image digest.")
    admin_values = (args.admin_image_digest, args.admin_source_commit, args.admin_source_tree, args.admin_sbom_file)
    if any(admin_values):
        if not all(admin_values):
            raise ValueError("An admin image update requires its digest, source commit/tree and exact SBOM.")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.admin_image_digest) or any(
            not re.fullmatch(r"[a-f0-9]{40}", value)
            for value in (args.admin_source_commit, args.admin_source_tree)
        ):
            raise ValueError("Pin immutable admin source and image identities.")
        if json.loads(args.admin_sbom_file.read_text()).get("bomFormat") != "CycloneDX":
            raise ValueError("The admin image SBOM must be CycloneDX JSON.")
    root = Path(__file__).resolve().parents[4]
    chart = root / "charts/control-plane/fs2-serve-control-plane"
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    helm = [
        "helm",
        "--kubeconfig",
        args.kubeconfig,
        "--kube-context",
        args.context,
        "-n",
        "fs2-system",
    ]
    baseline = json.loads((args.baseline / "values.json").read_text())
    live = json.loads(
        subprocess.check_output(
            helm + ["get", "values", "fs2-serve-control-plane", "-o", "json"]
        )
    )
    if live != baseline:
        raise ValueError(
            "The live release changed. Recapture/rebase; do not overwrite another task."
        )
    if args.backend_only:
        args.activation.mkdir(mode=0o700, parents=True, exist_ok=False)
        config = baseline["scientificBatch"]
        overlay = {"scientificBatch": {"executionMap": config["executionMap"]}}
        (args.activation / "activation.values.json").write_text(json.dumps(overlay))
        cm = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": config["schedulingContractConfigMapName"],
                "namespace": config["schedulingContractNamespace"],
            },
            "data": {
                config["schedulingContractKey"]: (
                    args.baseline / "scheduling.json"
                ).read_text()
            },
        }
        (args.activation / "scheduling.configmap.json").write_text(json.dumps(cm))
    overlay_path = args.activation / "activation.values.json"
    overlay = json.loads(overlay_path.read_text())
    source = json.loads(
        (root / "catalog/runtime/contracts/scientific-execution-map.json").read_text()
    )
    if source["models"] != overlay["scientificBatch"]["executionMap"]["models"]:
        raise ValueError("The source and rendered execution rows differ.")
    merged = {**baseline, "image": {**baseline["image"], "digest": args.image_digest}}
    for key, value in overlay.items():
        merged[key] = {**merged[key], **value}
    if args.admin_image_digest:
        current_admin = baseline["adminConsole"]
        if not current_admin["enabled"]:
            raise ValueError("This update may not enable a previously disabled admin console.")
        merged["adminConsole"] = {
            **current_admin,
            "image": {**current_admin["image"], "digest": args.admin_image_digest},
            "provenance": {
                **current_admin.get("provenance", {}),
                "sourceCommit": args.admin_source_commit,
                "sourceTree": args.admin_source_tree,
                "sbomFormat": "cyclonedx-json",
                "sbomSha256": hashlib.sha256(args.admin_sbom_file.read_bytes()).hexdigest(),
            },
        }
    render_values = args.activation / "release.values.json"
    if render_values.exists():
        raise ValueError("Use a new activation directory for each release attempt.")
    with os.fdopen(
        os.open(render_values, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        json.dump(merged, output)
    rendered = subprocess.check_output(
        helm
        + ["template", "fs2-serve-control-plane", str(chart), "-f", str(render_values)]
    )
    with os.fdopen(
        os.open(
            args.activation / "rendered.yaml",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        ),
        "wb",
    ) as output:
        output.write(rendered)
    cm = args.activation / "scheduling.configmap.json"
    subprocess.run(kube + ["apply", "--dry-run=server", "-f", str(cm)], check=True)
    print(
        json.dumps(
            {
                "rendered": True,
                "existing_apps_preserved": len(source["models"]) - 1,
                "previous_digest": baseline["image"]["digest"],
                "next_digest": args.image_digest,
                "apply": args.apply,
            }
        ),
        flush=True,
    )
    if args.apply:
        subprocess.run(kube + ["apply", "-f", str(cm)], check=True)
        subprocess.run(
            helm
            + [
                "upgrade",
                "fs2-serve-control-plane",
                str(chart),
                "-f",
                str(render_values),
                "--rollback-on-failure",
                "--wait",
                "--timeout",
                "8m",
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
