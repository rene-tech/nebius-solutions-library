"""Relocate validated WHAM references to the per-window archive layout.

Only paths change. Scientific values and every native-content hash are retained.
The original manifest is copied as provenance; no native file is rewritten.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tarfile


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export(source, destination):
    value = json.loads(source.read_text())
    result = copy.deepcopy(value)
    if len(value["windows"]) != 24 or {w["id"] for w in value["windows"]} != {
            f"window-{i:02d}" for i in range(24)}:
        raise ValueError("Require the exact complete 24-window cohort")
    if not destination.is_dir() or (destination / "windows.json").exists():
        raise ValueError("Use an existing delivery directory with a new manifest")
    for row in result["windows"]:
        archive = destination / (row["id"] + ".tar.gz")
        with tarfile.open(archive, "r:gz") as tar:
            for field in ("tpr", "pullx", "native_result"):
                old = Path(row[field])
                if (old.is_absolute() or ".." in old.parts or len(old.parts) < 3
                        or old.parts[1] != row["id"]):
                    raise ValueError("Unexpected native layout or cross-window path")
                relative = Path(*old.parts[1:]).as_posix()
                actual = (source.parent / old).resolve(strict=True)
                if digest(actual) != row[field + "_sha256"]:
                    raise ValueError("Native reference changed before export")
                member = tar.getmember(relative)
                if not member.isfile() or member.size != actual.stat().st_size:
                    raise ValueError("Archive does not contain the exact native reference")
                with tar.extractfile(member) as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != row[field + "_sha256"]:
                        raise ValueError("Archive reference digest mismatch")
                row[field] = relative
    unbiased = Path(value["unbiased"]["frames_csv"])
    if not unbiased.is_absolute():
        unbiased = source.parent / unbiased
    if digest(unbiased) != value["unbiased"]["frames_csv_sha256"]:
        raise ValueError("Original unbiased overlay changed")
    overlay_name = "unbiased-gromacs-frames.csv"
    for target, data in ((destination / overlay_name, unbiased.read_bytes()),
                         (destination / "windows-original.json", source.read_bytes())):
        with target.open("xb") as stream:
            stream.write(data)
    result["unbiased"]["frames_csv"] = overlay_name
    result["publication_provenance"] = {
        "source_manifest_sha256": digest(source),
        "scientific_values_changed": False,
        "native_bytes_changed": False,
        "layout": "Extract each window-NN.tar.gz beside this manifest.",
        "archive_references_checked": 72,
    }
    target = destination / "windows.json"
    with target.open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return {"manifest": str(target), "sha256": digest(target),
            "source_manifest_sha256": digest(source), "windows": 24,
            "native_archive_references_verified": 72}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.source.resolve(), args.destination.resolve())))


if __name__ == "__main__":
    main()
