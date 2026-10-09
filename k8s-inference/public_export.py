#!/usr/bin/env python3
"""Build the reusable solution separately from immutable operator-run records.

Historical records stay in Git, unchanged. The reviewed inventory pins their
bytes and omits them from the portable source export, never from preservation.
Unknown private references or changed inventory entries fail export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPORT_PATHS = (
    "k8s-inference",
    "modules/device-plugin",
    "modules/gpu-operator",
    "modules/network-operator",
    "README.md",
    ".github/workflows/k8s-inference.yml",
)
HISTORY_POLICY = "docs/scientific-ai/public-export-history.json"
FORBIDDEN_REFERENCES = (
    ("absolute developer home", re.compile("/" + r"(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)")),
    ("absolute Windows developer home", re.compile(r"\b[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+(?:[\\/]|\b)", re.I)),
    ("legacy private source layout", re.compile(r"(?<![\w-])" + "platform/" + r"fs2-serve(?:/|\b)")),
    ("legacy private wrapper", re.compile("fs2-" + r"stack\b")),
    (
        "private source repository",
        re.compile(r"github\.com/" + "rene-tech/" + r"nim-fast-start-platform(?:[/.]|\b)", re.I),
    ),
    (
        "opaque Nebius resource ID",
        re.compile(
            r"\b(?:project|tenant|mk8scluster|mk8snodegroup|vpcnetwork|vpcsubnet|"
            r"computeinstance|serviceaccount|containerregistry)-e[0-9a-z]{15,}\b",
            re.I,
        ),
    ),
)


def source_files(root: Path) -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise OSError("git is required to enumerate the source checkout")
    result = subprocess.run(  # noqa: S603 - fixed read-only argv; no shell interpolation
        [git, "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *EXPORT_PATHS],
        check=True,
        capture_output=True,
    )
    return sorted({root / os.fsdecode(p) for p in result.stdout.split(b"\0") if p})


def source_bytes(path: Path) -> bytes:
    return os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()


def text_content(path: Path) -> str | None:
    raw = source_bytes(path)
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def findings(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root).as_posix()
    source = text_content(path) or ""
    results = []
    for label, pattern in FORBIDDEN_REFERENCES:
        for match in pattern.finditer(relative + "\n" + source):
            # This is a deliberate container account, not a developer checkout.
            if (
                label == "absolute developer home"
                and match.group().rstrip("/") == "/home/" + "fs2"
                and path.name.startswith(("Dockerfile", "Containerfile"))
            ):
                continue
            results.append(f"{relative}:{(relative + chr(10) + source).count(chr(10), 0, match.start())}: {label}")
    return results


def is_operator_record(relative: str) -> bool:
    """Only operator history/tools can be omitted; never runtime/foundation code."""
    parts = Path(relative).parts
    if not parts or parts[0] != "k8s-inference":
        return False
    if "src" in parts or "schema" in parts or "contracts" in parts:
        return False
    if "acceptance" in parts or "evidence" in parts or "qualification" in parts:
        return True
    if "runtime" in parts:
        return False
    if parts[1:2] in (("docs",), ("operations",)):
        return relative.endswith(".md")
    if parts[1:2] == ("models",):
        if relative == "k8s-inference/models/general-media/cosmos-transfer25/test_runtime_manifest.py":
            return True  # Test of the paired, fixed September operator snapshot.
        return (
            "qualification" in parts
            or "evidence" in parts
            or "comparison" in parts
            or any(
                word in Path(relative).name.lower()
                for word in (
                    "qualification",
                    "preflight",
                    "precache",
                    "publication",
                    "debug-failure",
                    "access-",
                    "custody",
                    "canary-",
                    "runtime-",
                    "startup-failure",
                    "existing-pool",
                )
            )
        )
    return "acceptance" in parts or relative.endswith("/docs/operator-runbook.md")


def export_files(root: Path = REPOSITORY_ROOT) -> list[Path]:
    policy = json.loads((root / HISTORY_POLICY).read_text())
    if policy.get("schema") != "scientific-ai/public-export-history/v1":
        raise ValueError("unrecognized public export inventory")
    historical = policy["files"]
    if not isinstance(historical, dict):
        raise ValueError("public export inventory must map exact files to digests")
    tracked = source_files(root)
    relative_paths = {p.relative_to(root).as_posix() for p in tracked}
    for relative, entry in historical.items():
        path = root / relative
        if relative not in relative_paths or not is_operator_record(relative):
            raise ValueError(f"invalid operator-history exclusion: {relative}")
        if not entry.get("reason") or hashlib.sha256(source_bytes(path)).hexdigest() != entry["sha256"]:
            raise ValueError(f"operator history changed; review before export: {relative}")
    return [p for p in tracked if p.relative_to(root).as_posix() not in historical and (p.exists() or p.is_symlink())]


def validate(root: Path = REPOSITORY_ROOT) -> list[Path]:
    paths = export_files(root)
    errors = [error for path in paths for error in findings(path, root)]
    if errors:
        raise ValueError("Non-portable references in reusable export:\n" + "\n".join(errors))
    return paths


def write_export(root: Path, output: Path) -> dict[str, str]:
    paths = validate(root)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for source in paths:
        relative = source.relative_to(root)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)
        manifest[relative.as_posix()] = hashlib.sha256(source_bytes(source)).hexdigest()
    for target in output.rglob("*"):
        if target.is_symlink() and (not target.exists() or not target.resolve().is_relative_to(output.resolve())):
            raise ValueError(f"export has an unresolved/outside symlink: {target.relative_to(output)}")
    (output / "public-export-manifest.json").write_text(
        json.dumps(
            {"schema": "scientific-ai/public-export/v1", "files": manifest},
            indent=2,
        )
        + "\n"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new directory for the portable source tree")
    args = parser.parse_args()
    try:
        paths = write_export(REPOSITORY_ROOT, args.output) if args.output else validate()
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(f"Validated {len(paths)} reusable files; original operator records remain unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
