#!/usr/bin/env python3
"""Refuse to ship licensed bytes, credentials or signed URLs from a directory.

Kept in its own file so the patterns can never match the checker that runs them,
and walking the working tree rather than the git index so an untracked artifact
left behind by a converging run is still caught.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LICENSED_SUFFIXES = {".whl", ".conda", ".bin", ".zst", ".pkl", ".pt", ".safetensors"}
SKIP_DIRECTORIES = {".git", "__pycache__", "private-state", ".pytest_cache", "node_modules"}
# These files carry deliberate probe values that prove the detectors fire.
DETECTOR_FIXTURES = {"tests/test_public_evidence.py", "tests/public_identity_classes.py"}
MAX_SCAN_BYTES = 2 * 1024 * 1024

# One definition of what must never be exported, shared with the evidence tests
# and applied across the whole component: contracts, evidence, manifests, scripts
# and fixtures alike. Unredacted deployment binding lives only in the ignored
# owner-private state.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_identity_classes import IDENTITY_VALUE_PATTERNS as EXPORT_PATTERNS  # noqa: E402

SECRET_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("signed URL signature", re.compile(r"[?&]X-Goog-Signature=")),
    ("presigned AWS URL", re.compile(r"[?&]X-Amz-Signature=")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token literal", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{24,}={0,2}")),
    ("registry auth blob", re.compile(r'"auths"\s*:\s*\{[^}]*"auth"\s*:\s*"[A-Za-z0-9+/=]{16,}"')),
)


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if path.suffix.lower() in LICENSED_SUFFIXES:
            findings.append(f"{relative}: licensed or model artifact must never be committed")
            continue
        if path.stat().st_size > MAX_SCAN_BYTES:
            findings.append(f"{relative}: unexpectedly large file for a contract directory")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if relative.as_posix() in DETECTOR_FIXTURES:
            continue
        for label, pattern in SECRET_PATTERNS + EXPORT_PATTERNS:
            if pattern.search(text):
                findings.append(f"{relative}: {label}")
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    findings = scan(root)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"{len(findings)} confidentiality violation(s)", file=sys.stderr)
        return 1
    print(f"scanned {root.name}: no licensed bytes, credentials or signed URLs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
