#!/usr/bin/env python3
"""Build the deterministic SAI-07 v4 zipapp without replacing any artifact.

The output is opened with O_EXCL and never renamed over an existing file.  A
reviewed release pipeline must compare its digest with the capsule contract and
sign the separate runtime attestation.  This script is source-only here and was
not executed by this task.
"""

from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "scripts/audit_sai07_effective_authority.py",
    "scripts/audit_sai07_effective_authority_v2.py",
    "scripts/collect_sai07_authoritative_custody_evidence.py",
    "scripts/collect_sai07_secret_metadata.py",
    "scripts/run_sai07_external_execution_v3.py",
    "scripts/run_sai07_retained_state_custody_v3.py",
    "scripts/sai07_authoritative_evidence.py",
    "scripts/sai07_custody_state_semantics.py",
    "scripts/sai07_epoch_admission_v4.py",
    "scripts/sai07_execution_bundle_main_v4.py",
    "scripts/sai07_owner_secret_transport_v3.py",
    "scripts/sai07_saved_plan_contract.py",
    "scripts/verify_pod_security_receipts.py",
    "scripts/verify_sai07_custody_manifest_bundle.py",
    "scripts/verify_sai07_custody_manifest_bundle_v2.py",
    "scripts/verify_sai07_custody_manifest_bundle_v3.py",
    "scripts/verify_sai07_custody_trust.py",
    "scripts/verify_sai07_custody_trust_v3.py",
    "scripts/verify_sai07_external_execution_ack_v3.py",
    "scripts/verify_sai07_external_handoff.py",
    "stages/pod-security-custody/sai07-execution-interface-v5.fixture.json",
)
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def archive_name(source: str) -> str:
    name = Path(source).name
    return "__main__.py" if name == "sai07_execution_bundle_main_v4.py" else name


def build(output: Path) -> None:
    if not output.is_absolute() or ".." in output.parts:
        raise ValueError("output must be an absolute traversal-free path")
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o444,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for source in SOURCES:
                    payload = (ROOT / source).read_bytes()
                    info = zipfile.ZipInfo(archive_name(source), FIXED_TIMESTAMP)
                    info.external_attr = 0o100444 << 16
                    info.create_system = 3
                    archive.writestr(info, payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(output.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
