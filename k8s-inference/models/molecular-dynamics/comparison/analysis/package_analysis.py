#!/usr/bin/env python3
"""Package runnable analysis beside existing, hash-verified delivery raw files.

The parent owns copying ALL raw workflow files into runs/<engine>. This helper
only verifies the dependencies used by analysis and packages small provenance
files/code. It never rewrites trajectories or claims scientific acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

from spec_paths import map_paths, resolve_spec

CODE = ("compare.py", "native.py", "thermo.py", "geometry.py", "render.py", "spec_paths.py")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def package(spec_file, bundle_root, mappings):
    spec_file = Path(spec_file).resolve(strict=True)
    bundle_root = Path(bundle_root).resolve(strict=True)
    if not bundle_root.is_dir():
        raise ValueError("bundle root must be an existing directory")
    destination = bundle_root / "analysis-inputs"
    if destination.exists():
        raise ValueError("analysis-inputs already exists; preserve earlier evidence")
    bindings = []
    for source, relative in mappings:
        source = Path(source).resolve(strict=True)
        target = (bundle_root / relative).resolve(strict=True)
        if not source.is_dir() or not target.is_dir() or not target.is_relative_to(bundle_root):
            raise ValueError("map an existing source directory to a directory inside the bundle")
        bindings.append((source, target))
    bindings.sort(key=lambda pair: len(pair[0].parts), reverse=True)
    resolved = resolve_spec(json.loads(spec_file.read_text()), spec_file)
    destination.mkdir()
    records, copied = {}, {}

    def verify(source, target, kind):
        if not target.resolve(strict=True).is_relative_to(bundle_root):
            raise ValueError("bundle dependency resolves outside the delivered bundle")
        source_hash = digest(source)
        if not target.is_file() or target.stat().st_size != source.stat().st_size or digest(target) != source_hash:
            raise ValueError(f"bundle dependency differs from source: {target}")
        records[str(target.relative_to(bundle_root))] = {"path": str(target.relative_to(bundle_root)), "source": str(source), "sha256": source_hash, "bytes": target.stat().st_size, "kind": kind}

    def rebase(value):
        source = Path(value).resolve(strict=True)
        for original, target_root in bindings:
            if source.is_relative_to(original):
                target = target_root / source.relative_to(original)
                if source.is_dir():
                    manifest = json.loads((source / "master-manifest.json").read_text())
                    for entry in manifest["files"]:
                        rel = Path(entry["path"])
                        if rel.is_absolute() or ".." in rel.parts:
                            raise ValueError("master manifest path leaves master directory")
                        if digest(source / rel) != entry["sha256"]:
                            raise ValueError(f"source master hash mismatch: {rel}")
                        verify(source / rel, target / rel, "existing-master")
                    for filename in ("master-manifest.json", "protocol.json"):
                        verify(source / filename, target / filename, "existing-master")
                else:
                    verify(source, target, "existing-native-or-provenance")
                return os.path.relpath(target, destination)
        if not source.is_file():
            raise ValueError("master directory must have an explicit bundle mapping")
        # Only unmatched evidence files are copied, never implicitly entire roots.
        source_hash = digest(source)
        if source_hash not in copied:
            target = destination / "provenance" / f"{source_hash[:16]}-{source.name}"
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            verify(source, target, "copied-provenance")
            copied[source_hash] = target
        return os.path.relpath(copied[source_hash], destination)

    try:
        # Every native input MUST already be materialized under a declared map.
        # Otherwise a missing map could quietly duplicate hundreds of MB.
        for run in resolved["runs"]:
            native = [run["trajectory"], run["native_topology"], run["production_log"], run["thermo"]["path"]]
            for item in native:
                for value in item if isinstance(item, list) else [item]:
                    if not any(Path(value).is_relative_to(original) for original, _ in bindings):
                        raise ValueError("native analysis dependency needs an explicit source-to-bundle map")
        portable = map_paths(resolved, rebase)
        portable["path_base"] = "spec-directory"
        (destination / "spec.json").write_text(json.dumps(portable, indent=2) + "\n")
        code = destination / "code"
        code.mkdir()
        source_code = Path(__file__).resolve().parent
        for filename in CODE:
            shutil.copyfile(source_code / filename, code / filename)
            verify(source_code / filename, code / filename, "analysis-code")
        for filename, target_name in (("regenerate.py", "regenerate.py"), ("requirements.txt", "requirements.txt"), ("BUNDLE_README.md", "README.md")):
            shutil.copyfile(source_code / filename, destination / target_name)
            verify(source_code / filename, destination / target_name, "regeneration-code-or-instructions")
        receipt = {"status": "transport-verified; scientific verdict remains in source analysis receipts", "raw_files_duplicated": False, "source_spec_sha256": digest(spec_file), "portable_spec_sha256": digest(destination / "spec.json"), "path_base": "spec-directory", "files": sorted(records.values(), key=lambda entry: entry["path"]), "all_workflow_outputs_included": "parent materializer responsibility; this helper verifies only explicit analysis dependencies"}
        (destination / "packaging-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return destination
    except Exception as exc:
        (destination / "packaging-failure.json").write_text(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, indent=2) + "\n")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--map", action="append", required=True, metavar="SOURCE=RELATIVE_BUNDLE_DIRECTORY")
    args = parser.parse_args()
    mappings = [value.split("=", 1) for value in args.map]
    if any(len(value) != 2 for value in mappings):
        parser.error("each map requires SOURCE=RELATIVE_BUNDLE_DIRECTORY")
    print(package(args.spec, args.bundle_root, mappings))


if __name__ == "__main__":
    main()
