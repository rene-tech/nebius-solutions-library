"""Compare-and-replace the API and companion images, preserving other live settings."""

import argparse
import copy
import json
import os
import re
import subprocess
from pathlib import Path

CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
REPO = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
)
BEFORE_API = (
    REPO + "@sha256:aae7e6f1f7dafe528051d69da07697d62531bc15ea930de4194aaf14c66fcd61"
)
BEFORE_TOOLS = (
    REPO + "@sha256:a8fb464f2b383b099b892909f39317a7a5275c90cd88e835248b083c97ed76da"
)


def extend(
    template, image, before_api=BEFORE_API, before_tools=BEFORE_TOOLS, tools_image=None
):
    tools_image = tools_image or image
    after = copy.deepcopy(template)
    changes = 0
    for field in ("containers", "initContainers"):
        for container in after["spec"].get(field, []):
            if container["image"].startswith(REPO + "@"):
                if container["image"] not in (before_api, image):
                    raise ValueError("Concurrent API release; rebase first")
                changes += container["image"] != image
                container["image"] = image
            for env in container.get("env", []):
                if env["name"] == "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE":
                    if env.get("value") not in (before_tools, tools_image):
                        raise ValueError("Concurrent companion release; rebase first")
                    changes += env["value"] != tools_image
                    env["value"] = tools_image
    if changes:
        after.setdefault("metadata", {}).setdefault("annotations", {})[
            "fs2.nebius.ai/image-digest"
        ] = image.split("@")[1]
    return after


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--baseline-api", default=BEFORE_API)
    p.add_argument("--baseline-tools", default=BEFORE_TOOLS)
    p.add_argument(
        "--tools-image",
        help="Keep the already-qualified companion for an API-only change",
    )
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    for image in (
        args.image,
        args.baseline_api,
        args.baseline_tools,
        args.tools_image or args.image,
    ):
        if not re.fullmatch(re.escape(REPO) + r"@sha256:[a-f0-9]{64}", image):
            p.error("Use immutable manifests in the existing regional repository")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = [
        "kubectl",
        "--context",
        CONTEXT,
        "-n",
        "fs2-system",
        "--request-timeout=30s",
    ]
    commands = []
    for kind, name in [
        ("deployment", "fs2-serve-control-plane"),
        ("deployment", "fs2-serve-control-plane-model-controller"),
        ("cronjob", "fs2-serve-control-plane-maintenance"),
    ]:
        obj = json.loads(
            subprocess.check_output([*kube, "get", kind, name, "-o", "json"])
        )
        path = (
            "/spec/jobTemplate/spec/template" if kind == "cronjob" else "/spec/template"
        )
        spec = obj["spec"]["jobTemplate"]["spec"] if kind == "cronjob" else obj["spec"]
        before = spec["template"]
        after = extend(
            before, args.image, args.baseline_api, args.baseline_tools, args.tools_image
        )
        if before == after:
            continue
        for suffix, original, replacement in [
            ("patch", before, after),
            ("rollback", after, before),
        ]:
            patch = [
                {"op": "test", "path": path, "value": original},
                {"op": "replace", "path": path, "value": replacement},
            ]
            (args.output / f"{name}.{suffix}.json").write_text(
                json.dumps(patch, indent=2) + "\n"
            )
        command = [
            *kube,
            "patch",
            kind,
            name,
            "--type=json",
            "--patch-file",
            str(args.output / f"{name}.patch.json"),
        ]
        subprocess.run([*command, "--dry-run=server", "-o", "name"], check=True)
        commands.append(command)
    if args.apply:
        for command in commands:
            subprocess.run([*command, "-o", "name"], check=True)
    print(
        json.dumps(
            {"image": args.image, "targets": len(commands), "applied": args.apply}
        )
    )


if __name__ == "__main__":
    main()
