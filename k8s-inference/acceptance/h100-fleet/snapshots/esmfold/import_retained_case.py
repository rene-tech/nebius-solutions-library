#!/usr/bin/env python3
"""Reuse an unchanged retained controller-issued workspace as a snapshot case."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pod", "workspace", "directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    pod = json.loads(args.pod.read_bytes())
    runtime = next(item for item in pod["spec"]["containers"] if item["name"] == "scientific-stage")
    command = runtime["command"] + runtime.get("args", [])
    prepared = command[command.index("--input") + 1].removeprefix("/mnt/fs2-scientific/")
    with tarfile.open(args.workspace) as archive:
        member = next(item for item in archive.getmembers() if item.name.removeprefix("./") == prepared)
        assert member.isfile()
        data = archive.extractfile(member).read()
        json.loads(data)
    environment = {item["name"]: item["value"] for item in runtime.get("env", [])
                   if "value" in item and item["name"].startswith("FS2_")
                   and not any(word in item["name"] for word in ("TOKEN", "CAPABILITY", "SECRET", "PASSWORD"))}
    (args.directory / "original-command.json").write_text(json.dumps(command))
    (args.directory / "request-environment.json").write_text(json.dumps(environment))
    shutil.copyfile(args.workspace, args.directory / "prepared-workspace.tar")
    (args.directory / "receipt.json").write_text(json.dumps({
        "source_pod_uid": pod["metadata"]["uid"], "runtime_image": runtime["image"],
        "prepared_sha256": hashlib.sha256(data).hexdigest(),
        "workspace_sha256": hashlib.file_digest(args.workspace.open("rb"), "sha256").hexdigest(),
        "source": "retained genuine controller-issued argv, localization marker and prepared handoff; unchanged bytes",
    }, indent=2))


if __name__ == "__main__":
    main()
