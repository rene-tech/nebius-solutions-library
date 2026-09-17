#!/usr/bin/env python3
"""Package raw acceptance artifacts without changing their bytes or local paths.

Run `pack` after every worker has stopped writing. `verify` checks archives and
all member checksums; `unpack` restores the paths used by analysis scripts.
No model weights, checkpoint images, vendor sources or environments are packed.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ARCHIVES = ROOT / "evidence-archives"
GROUPS = {
    "boltz2": ("boltz2/raw", "boltz2/evidence"),
    "openfold": ("openfold/raw",),
    "coverage": ("coverage/raw", "coverage/artifacts"),
    "protenix": ("protenix/raw",),
    "snapshot": ("snapshot/raw",),
    "snapshot-openfold3": ("snapshot/openfold3/raw",),
    "snapshot-protenix": (
        "snapshot/protenix/raw", "snapshot/protenix/xjaw/raw",
        "snapshot/protenix/y0jt/raw",
    ),
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pack():
    ARCHIVES.mkdir(exist_ok=True)
    manifest = {"schema": "fs2.bioir-evidence-archives/v1", "archives": []}
    for name, directories in GROUPS.items():
        destination = ARCHIVES / f"{name}.tar.gz"
        files = sorted(path for directory in directories
                       for path in (ROOT / directory).rglob("*") if path.is_file())
        members = []
        with destination.open("wb") as stream:
            with gzip.GzipFile(fileobj=stream, filename="", mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as archive:
                    for path in files:
                        if path.is_symlink():
                            raise ValueError(f"refusing symlink: {path}")
                        data = path.read_bytes()
                        relative = path.relative_to(ROOT).as_posix()
                        entry = tarfile.TarInfo(relative)
                        entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
                        archive.addfile(entry, io.BytesIO(data))
                        members.append({"path": relative, "bytes": len(data), "sha256": sha256(data)})
        manifest["archives"].append({"file": destination.name,
                                     "bytes": destination.stat().st_size,
                                     "sha256": sha256(destination.read_bytes()),
                                     "members": members})
    (ARCHIVES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify(unpack=False):
    manifest = json.loads((ARCHIVES / "manifest.json").read_text())
    checked = 0
    for record in manifest["archives"]:
        path = ARCHIVES / record["file"]
        if path.parent != ARCHIVES or sha256(path.read_bytes()) != record["sha256"]:
            raise ValueError(f"archive path/checksum mismatch: {path}")
        expected = {item["path"]: item for item in record["members"]}
        with tarfile.open(path, "r:gz") as archive:
            seen = set()
            for member in archive:
                name = PurePosixPath(member.name)
                if not member.isfile() or name.is_absolute() or ".." in name.parts:
                    raise ValueError(f"unsafe archive member: {member.name}")
                if member.name not in expected or member.name in seen:
                    raise ValueError(f"unexpected/duplicate member: {member.name}")
                data = archive.extractfile(member).read()
                receipt = expected[member.name]
                if len(data) != receipt["bytes"] or sha256(data) != receipt["sha256"]:
                    raise ValueError(f"member checksum mismatch: {member.name}")
                if unpack:
                    target = ROOT / member.name
                    if not target.resolve().is_relative_to(ROOT):
                        raise ValueError(f"destination escapes evidence root: {target}")
                    if target.exists() and target.read_bytes() != data:
                        raise ValueError(f"refusing to overwrite changed evidence: {target}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        target.write_bytes(data)
                seen.add(member.name)
            if seen != expected.keys():
                raise ValueError(f"missing archive members: {path}")
            checked += len(seen)
    return {"archives": len(manifest["archives"]), "verified_files": checked,
            "compressed_bytes": sum(item["bytes"] for item in manifest["archives"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "verify", "unpack"))
    args = parser.parse_args()
    if args.action == "pack":
        pack()
    print(json.dumps(verify(unpack=args.action == "unpack")))
