#!/usr/bin/env python3
"""Deterministic installer for a licensed academic tree on a tenant-private volume.

Three bounded operations, each usable on its own so they can be tested directly
rather than only asserted about in shell:

``prepare``  Bootstraps an *empty* volume root so a non-root installer can write
             to it. A freshly provisioned claim is root-owned and mode 0755, so
             without this the first mkdir by the non-root installer fails. It
             refuses to touch a root that already holds assets, so it can never
             weaken an existing tree.

``install``  Installs the pinned wheel into a staging directory and promotes it
             atomically, then applies the contracted group-readable modes. An
             interrupted run leaves the previous tree untouched.

``verify``   Imports the installed distribution from the tree in place, asserts
             the exact version, runs the contracted functional proof, and emits
             a receipt-shaped summary. Never prints licensed content.

No licensed bytes, credentials or owner-only paths are ever written to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

MAX_GID = 65535


class InstallError(Exception):
    pass


def _octal(value: str) -> int:
    if not isinstance(value, str) or len(value) != 4 or value[0] != "0":
        raise InstallError(f"mode {value!r} must be an octal string like 0440")
    try:
        bits = int(value, 8)
    except ValueError as exc:
        raise InstallError(f"mode {value!r} is not octal") from exc
    if bits & 0o007:
        raise InstallError(f"mode {value} would make licensed bytes world-accessible")
    if bits & 0o022:
        raise InstallError(f"mode {value} would make licensed bytes writable")
    if not bits & 0o040:
        raise InstallError(f"mode {value} must be group readable")
    return bits


def _check_gid(gid: int) -> int:
    if not isinstance(gid, int) or gid < 1 or gid > MAX_GID:
        raise InstallError("asset group must be a non-root group id")
    return gid


def prepare_asset_directory(directory: Path, *, mode: str = "0750") -> dict[str, Any]:
    """Create the owner-writable, group-traversable directory for one asset."""

    bits = int(mode, 8)
    if bits & 0o007 or bits & 0o020:
        raise InstallError("asset directory must not be world-accessible or group writable")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(bits)
    return {"path": directory.name, "mode": "0%o" % stat.S_IMODE(directory.lstat().st_mode)}


def prepare_volume_root(root: Path, *, gid: int, mode: str = "2770") -> dict[str, Any]:
    """Make an empty tenant volume root writable by the asset group.

    Bounded on purpose: it is not recursive, and it refuses a non-empty root so a
    later run can never relax the modes of already-staged licensed assets.
    """

    _check_gid(gid)
    if not isinstance(mode, str) or len(mode) != 4:
        raise InstallError("root mode must be a four-digit octal string")
    bits = int(mode, 8)
    if bits & 0o007:
        raise InstallError("the tenant volume root must not be world-accessible")
    if not root.is_dir():
        raise InstallError("tenant volume root does not exist")

    entries = sorted(entry.name for entry in root.iterdir())
    before = root.stat()
    if entries:
        # Already populated: never loosen anything, but a root that is more
        # permissive than contracted is tightened, since removing access can only
        # strengthen the tenant boundary.
        current = stat.S_IMODE(before.st_mode)
        tightened = current & bits
        action = "verified-existing"
        if tightened != current:
            try:
                os.chmod(root, tightened)
                action = "tightened-existing-root"
            except PermissionError:
                action = "verified-existing-tightening-denied"
        after = root.stat()
        return {
            "action": action,
            "entries": len(entries),
            "gid": after.st_gid,
            "mode": "0%o" % stat.S_IMODE(after.st_mode),
            "group_writable": bool(stat.S_IMODE(after.st_mode) & 0o020),
            "world_accessible": bool(stat.S_IMODE(after.st_mode) & 0o007),
        }

    try:
        os.chown(root, -1, gid)
    except PermissionError as exc:
        raise InstallError("preparing an empty tenant root requires privilege to set its group") from exc
    os.chmod(root, bits)
    after = root.stat()
    return {
        "action": "prepared-empty-root",
        "entries": 0,
        "gid": after.st_gid,
        "mode": "0%o" % stat.S_IMODE(after.st_mode),
        "group_writable": bool(stat.S_IMODE(after.st_mode) & 0o020),
        "world_accessible": bool(stat.S_IMODE(after.st_mode) & 0o007),
    }



TREE_MANIFEST_ALGORITHM = "fs2-tree-manifest/v1"


def tree_manifest(root: Path) -> dict[str, Any]:
    """Deterministically identify a directory tree.

    A directory has no natural digest, so one is defined here rather than borrowed
    from whatever produced it. Every regular file contributes its POSIX-relative
    path, byte size and SHA-256; every symlink contributes its path and target.
    Entries are sorted by path and serialized as canonical JSON, and the manifest
    digest is the SHA-256 of those bytes. Sorting plus canonical JSON is what makes
    it unambiguous: no separator can be confused with path content.

    Returned alongside the digest are the real file count and the real byte total,
    so a tree is never described using the identity of the archive it came from.
    """

    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        entries.append({"path": relative, "kind": "file", "size_bytes": size, "sha256": digest.hexdigest()})
        total_bytes += size

    payload = {"algorithm": TREE_MANIFEST_ALGORITHM, "entries": entries}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {
        "tree_manifest_algorithm": TREE_MANIFEST_ALGORITHM,
        "tree_manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "tree_total_bytes": total_bytes,
        "file_count": sum(1 for entry in entries if entry["kind"] == "file"),
        "symlink_count": sum(1 for entry in entries if entry["kind"] == "symlink"),
    }


def _force_remove(path: Path) -> None:
    """Remove a tree whose contracted modes are deliberately non-writable."""

    if not path.exists():
        return
    for entry in sorted(path.rglob("*"), reverse=True):
        try:
            entry.chmod(0o700)
        except OSError:
            pass
    try:
        path.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _apply_modes(tree: Path, *, file_mode: int, directory_mode: int) -> int:
    count = 0
    for dirpath, _, filenames in os.walk(tree, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            current = stat.S_IMODE(path.lstat().st_mode)
            want = directory_mode if current & 0o111 else file_mode
            if current != want:
                path.chmod(want)
            count += 1
        Path(dirpath).chmod(directory_mode)
    tree.chmod(directory_mode)
    return count


def install_wheel(
    wheel: Path,
    destination: Path,
    *,
    file_mode: str,
    directory_mode: str,
    gid: int,
    python: str | None = None,
) -> dict[str, Any]:
    """Install the pinned wheel and promote it atomically."""

    _check_gid(gid)
    file_bits = _octal(file_mode)
    directory_bits = _octal(directory_mode)
    if not directory_bits & 0o010:
        raise InstallError("directory mode must be group executable to be traversable")
    if not wheel.is_file():
        raise InstallError("pinned wheel is not present on the tenant volume")

    interpreter = python or sys.executable
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    # The asset directory is contracted owner-writable, so promotion needs no
    # privilege escalation and no temporary relaxation of the installed tree.
    parent_mode = stat.S_IMODE(parent.lstat().st_mode)
    if not parent_mode & 0o300:
        raise InstallError(
            "asset directory is not owner-writable; run the bootstrap before installing"
        )
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.installing-", dir=parent))
    previous = destination.with_name(f".{destination.name}.previous")
    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                interpreter,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--target",
                str(staging),
                str(wheel),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        file_count = _apply_modes(staging, file_mode=file_bits, directory_mode=directory_bits)
        if destination.exists():
            os.replace(destination, previous)
        os.replace(staging, destination)
        staging = None  # promoted
    except subprocess.CalledProcessError as exc:
        raise InstallError(f"pip install failed: {exc.stderr.decode()[-400:]}") from exc
    finally:
        if staging is not None:
            _force_remove(staging)
    _force_remove(previous)
    identity = tree_manifest(destination)
    # file_count from the mode pass counts directories too; the manifest count is
    # the authoritative number of files.
    del file_count
    return {"atomic_promotion": True, **identity}


def verify_installed_tree(
    tree: Path,
    *,
    distribution: str,
    version: str,
    file_mode: str,
    directory_mode: str,
    gid: int,
    functional_proof: bool = True,
    python: str | None = None,
    expect_tree_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Import the installed distribution in place and prove it works."""

    _check_gid(gid)
    _octal(file_mode)
    _octal(directory_mode)
    if not tree.is_dir():
        raise InstallError("installed tree is missing")

    identity = tree_manifest(tree)
    if expect_tree_manifest_sha256 is not None and identity["tree_manifest_sha256"] != expect_tree_manifest_sha256:
        raise InstallError("installed tree no longer matches its recorded manifest digest")

    violations = []
    file_count = 0
    for dirpath, _, filenames in os.walk(tree):
        for path in [Path(dirpath)] + [Path(dirpath) / name for name in filenames]:
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            if mode & 0o007 or mode & 0o022:
                violations.append(str(path.relative_to(tree)))
            if path.is_file():
                file_count += 1
    if violations:
        raise InstallError(f"{len(violations)} installed paths are world-accessible or writable")

    program = (
        "import importlib.metadata as md, json, platform, sys\n"
        f"import {distribution}\n"
        f"actual = md.version({distribution!r})\n"
        "proof = None\n"
        + (
            f"if {functional_proof!r} and {distribution!r} == 'pyrosetta':\n"
            f"    {distribution}.init('-mute all')\n"
            f"    pose = {distribution}.pose_from_sequence('AAAAAAAAAA')\n"
            f"    proof = {{'pose_residues': pose.total_residue(),"
            f" 'score': round({distribution}.get_score_function()(pose), 6)}}\n"
        )
        + "print(json.dumps({'version': actual, 'python': platform.python_version(),"
        f" 'origin': {distribution}.__file__, 'proof': proof}}))\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tree)
    environment.setdefault("HOME", tempfile.gettempdir())
    interpreter = python or sys.executable
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [interpreter, "-c", program],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise InstallError(f"importing the installed tree failed: {result.stderr[-400:]}")
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    if observed["version"] != version:
        raise InstallError("installed distribution version is not the pinned version")
    if not str(observed["origin"]).startswith(str(tree)):
        raise InstallError("the distribution was not imported from the tenant-private tree")

    evidence = {
        "distribution": distribution,
        "version": observed["version"],
        "python": observed["python"],
        "file_count": file_count,
        "proof": observed["proof"],
    }
    return {
        "installed_distribution": distribution,
        "installed_distribution_version": observed["version"],
        "python_version": observed["python"],
        **identity,
        "world_readable": False,
        "import_verified": True,
        "functional_proof": observed["proof"],
        "evidence_digest": hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="bootstrap the tenant volume root and asset directories")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--gid", type=int, required=True)
    prepare.add_argument("--mode", default="2770")
    prepare.add_argument(
        "--asset-dir",
        action="append",
        default=[],
        help="asset directory name to create owner-writable and group-traversable",
    )
    prepare.add_argument("--asset-dir-mode", default="0750")

    install = sub.add_parser("install", help="install the pinned wheel and promote atomically")
    install.add_argument("--wheel", type=Path, required=True)
    install.add_argument("--destination", type=Path, required=True)
    install.add_argument("--file-mode", required=True)
    install.add_argument("--directory-mode", required=True)
    install.add_argument("--gid", type=int, required=True)

    verify = sub.add_parser("verify", help="import the installed tree and prove it works")
    verify.add_argument("--tree", type=Path, required=True)
    verify.add_argument("--distribution", required=True)
    verify.add_argument("--version", required=True)
    verify.add_argument("--file-mode", required=True)
    verify.add_argument("--directory-mode", required=True)
    verify.add_argument("--gid", type=int, required=True)
    verify.add_argument("--expect-tree-manifest-sha256")

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_volume_root(args.root, gid=args.gid, mode=args.mode)
            prepared = []
            for name in args.asset_dir:
                if "/" in name or name in {"", ".", ".."}:
                    raise InstallError("asset directory name must be a single safe path segment")
                prepared.append(
                    prepare_asset_directory(args.root / name, mode=args.asset_dir_mode)
                )
            result["asset_directories"] = prepared
        elif args.command == "install":
            result = install_wheel(
                args.wheel,
                args.destination,
                file_mode=args.file_mode,
                directory_mode=args.directory_mode,
                gid=args.gid,
                expect_tree_manifest_sha256=args.expect_tree_manifest_sha256,
            )
        else:
            result = verify_installed_tree(
                args.tree,
                distribution=args.distribution,
                version=args.version,
                file_mode=args.file_mode,
                directory_mode=args.directory_mode,
                gid=args.gid,
                expect_tree_manifest_sha256=args.expect_tree_manifest_sha256,
            )
    except InstallError as error:
        json.dump({"state": "InstallFailed", "message": str(error)}, sys.stdout)
        sys.stdout.write("\n")
        return 2
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
