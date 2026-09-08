"""Build and publish one aging worker from an exact committed source tree.

Registry authentication stays with the operator's normal Docker/Skopeo setup.
The output directory retains the OCI image, build log and publication receipt.
No Kubernetes resources are changed by this command.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("altumage", "phenoage"))
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--ref", required=True, help="Committed Git revision")
    parser.add_argument("--registry", required=True, help="Registry/repository prefix")
    parser.add_argument("--builder", default="default")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.model == "phenoage" and args.device != "cpu":
        parser.error("clinical PhenoAge has no CUDA runtime")
    registry = args.registry.rstrip("/")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.:-]*(?:/[a-z0-9][a-z0-9._-]*)+", registry):
        parser.error(
            "--registry must be a registry/repository prefix, without credentials or a tag"
        )
    repo = Path(__file__).resolve().parents[3]

    def output(command: list[str]) -> str:
        return subprocess.check_output(command, cwd=repo, text=True).strip()

    commit = output(["git", "rev-parse", "--verify", args.ref + "^{commit}"])
    tree = output(["git", "rev-parse", commit + "^{tree}"])
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = directory / "publication.json"
    if receipt_path.exists():
        parser.error(
            "output directory already contains a publication receipt; select a fresh directory"
        )
    context = Path(tempfile.mkdtemp(prefix="source-", dir=directory))
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", commit, "k8s-inference/models/aging"],
        cwd=repo,
    )
    subprocess.run(["tar", "-xf", "-", "-C", str(context)], input=archive, check=True)
    image_repository = f"{registry}/{args.model}-{args.device}"
    tag = f"{image_repository}:{commit[:12]}"
    oci_path = directory / "image.oci.tar"
    command = [
        "docker",
        "buildx",
        "build",
        "--builder",
        args.builder,
        "--platform",
        "linux/amd64",
        "--provenance=mode=max",
        "--file",
        str(context / f"k8s-inference/models/aging/Dockerfile.{args.model}"),
        "--label",
        f"org.opencontainers.image.revision={commit}",
        "--tag",
        tag,
        "--output",
        f"type=oci,dest={oci_path}",
        "--metadata-file",
        str(directory / "build-metadata.json"),
    ]
    if args.model == "altumage":
        wheel_index = "cu128" if args.device == "cuda" else "cpu"
        command += [
            "--build-arg",
            f"TORCH_INDEX_URL=https://download.pytorch.org/whl/{wheel_index}",
        ]
    command.append(str(context))
    with (directory / "build.log").open("x") as log:
        subprocess.run(
            command, cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True
        )
        subprocess.run(
            ["skopeo", "copy", "--all", f"oci-archive:{oci_path}", f"docker://{tag}"],
            cwd=repo,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    digest = output(["crane", "digest", "--platform", "linux/amd64", tag])
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("registry did not return a valid image digest")
    receipt = {
        "model_id": args.model,
        "device": args.device,
        "source_commit": commit,
        "source_tree": tree,
        "image": f"{image_repository}@{digest}",
        "image_digest": digest,
        "platform": "linux/amd64",
        "gpu_qualified": False,
        "note": "Publication is not evidence of deployed inference or GPU qualification.",
    }
    with receipt_path.open("x") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
