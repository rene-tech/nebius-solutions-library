"""Apply only the chart's artifact-transfer rule, preserving live sibling routes.

Capture the old route and rollback patch. Run the website public routing checker
before and after this operation; never apply an old full Helm release here.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--routing-checker", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    chart = (
        Path(__file__).resolve().parents[3]
        / "charts/control-plane/fs2-serve-control-plane"
    )
    # Captured Helm values predate the deployed database release. This command
    # renders ONLY HTTPRoute; restore the chart's own migration contract for
    # schema validation, never apply those migration/Deployment templates.
    values = json.loads(args.values.read_text())
    defaults = yaml.safe_load((chart / "values.yaml").read_text())
    values["migration"]["releaseContract"] = defaults["migration"]["releaseContract"]
    route_values = args.output / "render-values.private.json"
    route_values.write_text(json.dumps(values))
    rendered = yaml.safe_load(
        subprocess.check_output(
            [
                "helm",
                "template",
                "fs2-serve-control-plane",
                str(chart),
                "-n",
                "fs2-system",
                "-f",
                str(route_values),
                "--show-only",
                "templates/httproute.yaml",
            ]
        )
    )
    new_rule = next(
        rule
        for rule in rendered["spec"]["rules"]
        if any(
            match.get("path", {}).get("value") == "/v1/scientific-artifacts/uploads"
            for match in rule["matches"]
        )
    )
    kube = ["kubectl", "--context", args.context, "-n", "fs2-system"]
    before = json.loads(
        subprocess.check_output(
            [*kube, "get", "httproute", "fs2-serve-control-plane", "-o", "json"]
        )
    )
    if before["spec"].get("hostnames") or rendered["spec"].get("hostnames"):
        raise ValueError("Never introduce hostname shadowing on the shared listener")
    rules = before["spec"]["rules"]
    ordinary = next(
        rule
        for rule in rules
        if any(match.get("path", {}).get("value") == "/v1" for match in rule["matches"])
    )

    def targets(rule):
        return [
            (
                ref.get("group", ""),
                ref.get("kind", "Service"),
                ref.get("namespace", "fs2-system"),
                ref["name"],
                ref["port"],
                ref.get("weight", 1),
            )
            for ref in rule["backendRefs"]
        ]

    if (
        targets(new_rule) != targets(ordinary)
        or new_rule["filters"] != ordinary["filters"]
    ):
        raise ValueError(
            "Artifact route must retain the exact backend and identity-header filters"
        )
    new_rule["backendRefs"] = ordinary["backendRefs"]
    if any(
        any(
            match.get("path", {}).get("value") == "/v1/scientific-artifacts/uploads"
            for match in rule["matches"]
        )
        for rule in rules
    ):
        raise ValueError(
            "Artifact rule already exists; inspect it rather than appending another"
        )
    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": before["metadata"]["resourceVersion"],
        },
        {"op": "test", "path": "/spec/rules", "value": rules},
        {"op": "add", "path": "/spec/rules/-", "value": new_rule},
    ]
    rollback = [
        {"op": "test", "path": "/spec/rules", "value": rules + [new_rule]},
        {"op": "replace", "path": "/spec/rules", "value": rules},
    ]
    for name, value in (
        ("before.json", before),
        ("patch.json", patch),
        ("rollback.json", rollback),
    ):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    command = [
        *kube,
        "patch",
        "httproute",
        "fs2-serve-control-plane",
        "--type=json",
        "--patch-file",
        str(args.output / "patch.json"),
    ]
    subprocess.run([*command, "--dry-run=server"], check=True)
    if args.apply:

        def check_routes(label):
            result = subprocess.run(
                ["node", str(args.routing_checker)], capture_output=True, text=True
            )
            (args.output / f"public-routing-{label}.json").write_text(result.stdout)
            if result.returncode or not json.loads(result.stdout)["ok"]:
                raise RuntimeError(
                    "Public route qualification failed; see retained receipt"
                )

        check_routes("before")
        subprocess.run(command, check=True)
        try:
            check_routes("after")
        except Exception:
            subprocess.run(
                [
                    *kube,
                    "patch",
                    "httproute",
                    "fs2-serve-control-plane",
                    "--type=json",
                    "--patch-file",
                    str(args.output / "rollback.json"),
                ],
                check=True,
            )
            raise
    print(
        json.dumps(
            {
                "applied": args.apply,
                "artifact_timeout": new_rule["timeouts"],
                "other_rules_unchanged": True,
            }
        )
    )


if __name__ == "__main__":
    main()
