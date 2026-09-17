"""Build a provenance-bound public projection without changing source evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

EXPORT_SCHEMA: Final = "nebius-solutions-library.public-export/v1"
EXPORT_PATHS: Final = (
    "k8s-inference",
    "README.md",
    ".github/workflows/k8s-inference.yml",
)
_GIT = shutil.which("git")
if _GIT is None:
    raise RuntimeError("git is required to build a public export")
GIT: Final[str] = _GIT


@dataclass(frozen=True)
class RedactionRule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


REDACTION_RULES: Final = (
    RedactionRule(
        "absolute-developer-home",
        re.compile("/" + r"(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)"),
        "[redacted-developer-home]/",
    ),
    RedactionRule(
        "absolute-windows-developer-home",
        re.compile(
            r"\b[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+(?:[\\/]|\b)",
            re.IGNORECASE,
        ),
        "[redacted-developer-home]/",
    ),
    RedactionRule(
        "legacy-private-source-layout",
        re.compile("platform/" + "fs2-serve" + r"(?:/|\b)"),
        "[redacted-private-source]/",
    ),
    RedactionRule(
        "legacy-private-wrapper",
        re.compile("fs2-" + r"stack\b"),
        "[redacted-private-wrapper]",
    ),
    RedactionRule(
        "private-source-repository",
        re.compile(
            r"github\.com/" + "rene-tech/" + "nim-fast-start-platform" + r"(?:[/.]|\b)",
            re.IGNORECASE,
        ),
        "github.com/[redacted-private-source]",
    ),
    RedactionRule(
        "opaque-nebius-resource-id",
        re.compile(
            r"\b(?:project|tenant|mk8scluster|mk8snodegroup|vpcnetwork|"
            r"vpcsubnet|computeinstance|serviceaccount|containerregistry)"
            r"-e[0-9a-z]{15,}\b",
            re.IGNORECASE,
        ),
        "[redacted-nebius-resource-id]",
    ),
)


@dataclass(frozen=True)
class ProjectedFile:
    source_path: str
    export_path: str
    mode: str
    source_sha256: str
    export_sha256: str
    content: bytes
    redactions: tuple[tuple[str, int], ...]

    def manifest_record(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "export_path": self.export_path,
            "mode": self.mode,
            "source_sha256": self.source_sha256,
            "export_sha256": self.export_sha256,
            "redactions": [{"rule": name, "count": count} for name, count in self.redactions],
        }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def forbidden_findings(text: str) -> list[str]:
    """Return rule names only, so a failure log cannot repeat private values."""

    return [rule.name for rule in REDACTION_RULES if rule.pattern.search(text)]


def redact_text(text: str) -> tuple[str, tuple[tuple[str, int], ...]]:
    projected = text
    counts: list[tuple[str, int]] = []
    for rule in REDACTION_RULES:
        projected, count = rule.pattern.subn(rule.replacement, projected)
        if count:
            counts.append((rule.name, count))
    return projected, tuple(counts)


def project_file(source_path: str, mode: str, content: bytes) -> ProjectedFile:
    projected_path, path_redactions = redact_text(source_path)
    relative = PurePosixPath(projected_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("public export path escapes its root")

    projected = content
    content_redactions: tuple[tuple[str, int], ...] = ()
    if b"\0" not in content:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            redacted, content_redactions = redact_text(text)
            projected = redacted.encode("utf-8")

    combined: dict[str, int] = {}
    for name, count in (*path_redactions, *content_redactions):
        combined[name] = combined.get(name, 0) + count
    return ProjectedFile(
        source_path=source_path if not path_redactions else f"sha256:{_sha256(source_path.encode())}",
        export_path=relative.as_posix(),
        mode=mode,
        source_sha256=_sha256(content),
        export_sha256=_sha256(projected),
        content=projected,
        redactions=tuple(sorted(combined.items())),
    )


def working_tree_export_files(repository_root: Path, *, include_untracked: bool) -> list[Path]:
    arguments = [GIT, "-C", str(repository_root), "ls-files", "-z", "--cached"]
    if include_untracked:
        arguments.extend(("--others", "--exclude-standard"))
    arguments.extend(("--", *EXPORT_PATHS))
    result = subprocess.run(arguments, check=True, capture_output=True)  # noqa: S603 - fixed Git operation.
    return [
        repository_root / os.fsdecode(relative)
        for relative in result.stdout.split(b"\0")
        if relative
    ]


def project_working_tree_file(repository_root: Path, path: Path) -> ProjectedFile:
    relative = path.relative_to(repository_root).as_posix()
    if path.is_symlink():
        return project_file(relative, "120000", os.readlink(path).encode())
    mode = "100755" if path.stat().st_mode & 0o111 else "100644"
    return project_file(relative, mode, path.read_bytes())


def _git(repository_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed Git operation over an operator-selected local repository.
        [GIT, "-C", str(repository_root), *arguments],
        check=True,
        capture_output=True,
    )
    return result.stdout


def committed_projection(repository_root: Path, source_ref: str) -> tuple[str, list[ProjectedFile]]:
    commit = _git(
        repository_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{source_ref}^{{commit}}",
    ).decode().strip()
    listing = _git(repository_root, "ls-tree", "-rz", commit, "--", *EXPORT_PATHS)
    projected: list[ProjectedFile] = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, encoded_path = entry.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode().split(" ")
        if object_type != "blob":
            raise ValueError("public export contains a non-blob tree entry")
        source_path = os.fsdecode(encoded_path)
        content = _git(repository_root, "cat-file", "blob", object_id)
        projected.append(project_file(source_path, mode, content))
    return commit, projected


def validate_public_projection(files: list[ProjectedFile]) -> None:
    findings: list[str] = []
    for item in files:
        try:
            content = item.content.decode("utf-8")
        except UnicodeDecodeError:
            content = ""
        findings.extend(
            f"{item.export_path}: {rule}"
            for rule in forbidden_findings(f"{item.export_path}\n{content}")
        )
    if findings:
        raise ValueError("public export retains forbidden reference classes: " + ", ".join(findings))


def write_public_export(repository_root: Path, destination: Path, *, source_ref: str) -> dict[str, object]:
    repository_root = repository_root.resolve()
    destination = destination.resolve()
    if destination == repository_root or repository_root in destination.parents:
        raise ValueError("public export destination must be outside the source repository")
    commit, files = committed_projection(repository_root, source_ref)
    if len({item.export_path for item in files}) != len(files):
        raise ValueError("redaction caused colliding public export paths")
    validate_public_projection(files)

    destination.mkdir(parents=True, exist_ok=False)
    for item in files:
        target = destination / item.export_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.mode == "120000":
            link_target = item.content.decode("utf-8")
            if Path(link_target).is_absolute() or ".." in PurePosixPath(link_target).parts:
                raise ValueError("public export symlink escapes its root")
            target.symlink_to(link_target)
        else:
            target.write_bytes(item.content)
            target.chmod(0o755 if item.mode == "100755" else 0o644)

    manifest: dict[str, object] = {
        "schema": EXPORT_SCHEMA,
        "source_commit": commit,
        "files": [item.manifest_record() for item in files],
    }
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (destination / "PUBLIC_EXPORT_PROVENANCE.json").write_bytes(payload)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source-ref", default="HEAD")
    arguments = parser.parse_args()
    manifest = write_public_export(arguments.repository_root, arguments.destination, source_ref=arguments.source_ref)
    files = manifest["files"]
    if not isinstance(files, list):
        raise TypeError("public export manifest files field is invalid")
    print(json.dumps({"source_commit": manifest["source_commit"], "files": len(files)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
