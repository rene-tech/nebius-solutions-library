"""Prevent private checkout details from returning to the public solution."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPORT_PATHS = (
    "k8s-inference",
    "README.md",
    ".github/workflows/k8s-inference.yml",
)
FORBIDDEN_REFERENCES = (
    (
        "absolute developer home",
        re.compile("/" + r"(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)"),
    ),
    (
        "absolute Windows developer home",
        re.compile(
            r"\b[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+(?:[\\/]|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "legacy private source layout",
        re.compile("platform/" + "fs2-serve" + r"(?:/|\b)"),
    ),
    (
        "legacy private wrapper",
        re.compile("fs2-" + r"stack\b"),
    ),
    (
        "private source repository",
        re.compile(
            r"github\.com/" + "rene-tech/" + "nim-fast-start-platform" + r"(?:[/.]|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "opaque Nebius resource ID",
        re.compile(
            r"\b(?:project|tenant|mk8scluster|mk8snodegroup|vpcnetwork|"
            r"vpcsubnet|computeinstance|serviceaccount|containerregistry)"
            r"-e[0-9a-z]{15,}\b",
            re.IGNORECASE,
        ),
    ),
)


def export_files() -> list[Path]:
    """Return checked-in and not-yet-added public export files."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *EXPORT_PATHS,
        ],
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / os.fsdecode(relative)
        for relative in result.stdout.split(b"\0")
        if relative
    ]


def text_content(path: Path) -> str | None:
    """Read source text without following binary/generated content."""
    if path.is_symlink():
        return os.readlink(path)
    content = path.read_bytes()
    if b"\0" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


class PublicExportTests(unittest.TestCase):
    def test_export_contains_no_private_references(self) -> None:
        findings: list[str] = []
        for path in export_files():
            relative = path.relative_to(REPOSITORY_ROOT)
            if not path.exists() and not path.is_symlink():
                continue
            source = text_content(path)
            searchable = f"{relative.as_posix()}\n{source or ''}"
            for label, pattern in FORBIDDEN_REFERENCES:
                for match in pattern.finditer(searchable):
                    line = searchable.count("\n", 0, match.start())
                    findings.append(f"{relative}:{line}: {label}: {match.group(0)!r}")

        self.assertEqual(
            [],
            findings,
            "Public k8s-inference export contains private references:\n"
            + "\n".join(findings),
        )


if __name__ == "__main__":
    unittest.main()
