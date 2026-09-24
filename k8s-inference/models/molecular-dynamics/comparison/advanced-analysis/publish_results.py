"""Archive explicit scientific directories or publish immutable, verified objects.

Publication is restricted to the user's requested bucket/prefix. It never
changes ACLs, policies, limits, existing objects or credentials. Only explicit
files listed in the publication manifest are sent; private SDK directories and
credential files must not be included in that list.
"""
import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
from pathlib import Path
import subprocess
import tarfile
import tempfile

BUCKET = "renes-bucket"
PREFIX = "four-engine-alanine-20260923/advanced-analysis-20260924/"
AWS = ["/home/tux/.local/bin/aws", "--profile", "nebius-hs2", "--endpoint-url",
       "https://storage.eu-north1.nebius.cloud", "s3api"]


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_new(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def archive(source, target):
    source = source.resolve(strict=True)
    target = target.resolve()
    if target.exists() or target.is_relative_to(source):
        raise ValueError("Archive must be new and outside source")
    members = sorted(source.rglob("*"))
    if any(p.is_symlink() or (not p.is_file() and not p.is_dir()) for p in members):
        raise ValueError("Only actual scientific files/directories may be archived")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive mode and deterministic member order; original bytes are not edited.
    with target.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz", compresslevel=1) as tar:
        tar.add(source, arcname=source.name, recursive=False)
        for member in members:
            tar.add(member, arcname=source.name + "/" + str(member.relative_to(source)), recursive=False)
    return {"archive": str(target), "bytes": target.stat().st_size, "sha256": digest(target),
            "source": str(source), "files": sum(p.is_file() for p in members)}


def transfer(row, root):
    name = row["path"]
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise ValueError("Invalid publication file path")
    if path.stat().st_size != row["bytes"] or digest(path) != row["sha256"]:
        raise ValueError("Publication file changed")
    key = PREFIX + name
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    result = subprocess.run(AWS + ["put-object", "--bucket", BUCKET, "--key", key,
        "--body", str(path), "--if-none-match", "*", "--content-type", content_type,
        "--metadata", "sha256=" + row["sha256"]], capture_output=True, text=True)
    if result.returncode and not any(code in result.stderr for code in ("PreconditionFailed", "(412)")):
        raise RuntimeError("Object upload failed: " + result.stderr)
    # A repeated execution verifies an existing object; it never overwrites it.
    with tempfile.TemporaryDirectory(prefix="fs2-alanine-readback-") as temporary:
        downloaded = Path(temporary) / "object"
        get = subprocess.run(AWS + ["get-object", "--bucket", BUCKET, "--key", key, str(downloaded)],
                             capture_output=True, text=True, check=True)
        metadata = json.loads(get.stdout)
        if (downloaded.stat().st_size != row["bytes"] or digest(downloaded) != row["sha256"]
                or metadata.get("Metadata", {}).get("sha256") != row["sha256"]):
            raise ValueError("Full object readback failed size/hash/metadata check")
    return {**row, "bucket": BUCKET, "key": key, "readback_verified": True,
            "created_new": result.returncode == 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    a = sub.add_parser("archive")
    a.add_argument("--source", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    a.add_argument("--receipt", type=Path, required=True)
    p = sub.add_parser("publish")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists():
        raise ValueError("Use a new receipt path")
    if args.action == "archive":
        value = archive(args.source, args.output)
    else:
        plan = json.loads(args.manifest.read_text())
        if plan["bucket"] != BUCKET or plan["prefix"] != PREFIX:
            raise ValueError("Publication target differs from authorized task")
        rows = plan["files"]
        if len({r["path"] for r in rows}) != len(rows):
            raise ValueError("Duplicate object keys")
        root = args.manifest.parent.resolve()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            uploaded = list(workers.map(lambda row: transfer(row, root), rows))
        value = {"status": "all-full-readbacks-verified", "bucket": BUCKET, "prefix": PREFIX,
                 "manifest_sha256": digest(args.manifest), "objects": uploaded}
    save_new(args.receipt, value)
    print(json.dumps({k: v for k, v in value.items() if k != "objects"}))


if __name__ == "__main__":
    main()
