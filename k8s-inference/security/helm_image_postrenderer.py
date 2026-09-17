#!/usr/bin/env python3
"""Rewrite every rendered Helm image to its reviewed digest or fail closed.

Helm invokes post-renderers with the complete rendered release on stdin. This
tool deliberately does not trust an inventory-complete flag: every scalar
``image:`` value in the actual render is either already digest-bound or must
have an exact tag-to-digest mapping with registry-resolution provenance.
Unknown, tag-only, variable, or malformed references abort the release.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

if __name__ == "__main__" or __package__ != "security":
    print(
        "Helm image post-rendering is an import-only external-capsule payload",
        file=sys.stderr,
    )
    raise SystemExit(1)

from .execution_toolchain import (  # noqa: E402
    ToolchainError,
    validate_current_python,
    validate_source_file,
)
from .image_security_evidence import (  # noqa: E402
    DIGEST_REFERENCE,
    EvidenceError,
    validate_first_party_inventory,
    validate_image_gate_authorization,
    validate_inventory,
    validate_workload_registry_auth_receipt,
)
from .semantic_yaml_images import (  # noqa: E402
    SemanticYamlError,
    independently_validated_image_scalars,
)
from .yaml_image_references import rewrite_image_scalars  # noqa: E402


def _capsule_source_root() -> Path:
    value = os.environ.get("FS2_CAPSULE_SOURCE_ROOT", "")
    if not value and __name__ != "__main__":
        return Path(__file__).resolve().parent.parent
    if not value or not Path(value).is_absolute():
        raise EvidenceError("capsule read-only source root is absent")
    return Path(value).resolve()


def rewrite(
    rendered: str,
    lock_path: Path,
    first_party_lock_path: Path,
    trust_path: Path,
) -> tuple[str, set[str]]:
    images = validate_inventory(lock_path, trust_path)
    first_party_images = validate_first_party_inventory(
        first_party_lock_path,
        trust_path,
        source_root=_capsule_source_root(),
    )
    mappings = {
        image["source_reference"]: image["digest_reference"]
        for image in images
    }
    accepted_digests = {
        image["digest_reference"] for image in images + first_party_images
    }
    subjects: set[str] = set()
    replacements: dict[tuple[int, int], str] = {}
    try:
        scalars = independently_validated_image_scalars(rendered)
    except SemanticYamlError as exc:
        raise EvidenceError(str(exc)) from exc
    for scalar in scalars:
        reference = scalar.reference
        if DIGEST_REFERENCE.fullmatch(reference):
            digest_reference = reference
        else:
            digest_reference = mappings.get(reference)
            if digest_reference is None:
                raise EvidenceError(
                    f"render line {scalar.line}: image {reference!r} has no reviewed "
                    "tag-to-digest mapping"
                )
        if digest_reference not in accepted_digests:
            raise EvidenceError(
                f"render line {scalar.line}: digest {digest_reference!r} is absent "
                "from the reviewed scan inventory"
            )
        subjects.add(digest_reference)
        replacements[(scalar.start, scalar.end)] = digest_reference
    if not scalars:
        raise EvidenceError("render contains no image fields; refusing empty closure")
    return rewrite_image_scalars(rendered, replacements), subjects


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if os.environ.get("FS2_EXTERNAL_CAPSULE_ACTIVE") != "1":
        print("image post-render gate requires the external capsule", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--first-party-lock", required=True, type=Path)
    parser.add_argument("--trust", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--toolchain", required=True, type=Path)
    parser.add_argument("--surfaces", type=Path)
    parser.add_argument("--surface-id")
    parser.add_argument("--release-name")
    parser.add_argument("--namespace")
    parser.add_argument("--mode", choices=("install", "upgrade"))
    parser.add_argument("--chart-path", type=Path)
    parser.add_argument("--values-file", action="append", default=[], type=Path)
    parser.add_argument("--registry-auth-receipt", type=Path)
    args = parser.parse_args()
    try:
        lock = Path(os.environ.get("FS2_THIRD_PARTY_IMAGE_LOCK", args.lock)).resolve()
        first_party_lock = Path(
            os.environ.get("FS2_FIRST_PARTY_IMAGE_LOCK", args.first_party_lock)
        ).resolve()
        trust = args.trust.resolve()
        toolchain = args.toolchain.resolve()
        os.environ["FS2_IMAGE_GATE_TOOLCHAIN"] = str(toolchain)
        os.environ["FS2_IMAGE_GATE_TRUST"] = str(trust)
        validate_current_python(lock_path=toolchain, trust_path=trust)
        source_root = _capsule_source_root()
        for relative in (
            "security/execution_toolchain.py",
            "security/helm_image_postrenderer.py",
            "security/image_security_evidence.py",
            "security/release_image_closure.py",
            "security/semantic_yaml_images.py",
            "security/yaml_image_references.py",
        ):
            validate_source_file(
                relative,
                source_root=source_root,
                lock_path=toolchain,
                trust_path=trust,
            )
        authorization = Path(
            os.environ.get("FS2_IMAGE_GATE_AUTHORIZATION", args.authorization)
        ).resolve()
        authorized = validate_image_gate_authorization(
            authorization,
            trust,
            source_root,
        )
        if _sha256(lock) != authorized["inventory_sha256"]:
            raise EvidenceError(
                "third-party inventory differs from signed image-gate authority"
            )
        if (
            _sha256(first_party_lock)
            != authorized["first_party_inventory_sha256"]
        ):
            raise EvidenceError(
                "first-party inventory differs from signed image-gate authority"
            )
        rendered = sys.stdin.read()
        rewritten, subjects = rewrite(rendered, lock, first_party_lock, trust)
        trust_document = __import__("json").loads(trust.read_text(encoding="utf-8"))
        workload_policy = trust_document.get("workload_registry_authentication", {})
        private_registries = set(workload_policy.get("allowed_registries", []))
        private_subjects = {
            subject
            for subject in subjects
            if subject.split("/", 1)[0] in private_registries
        }
        if private_subjects:
            if args.registry_auth_receipt is None:
                raise EvidenceError(
                    "private rendered images require a signed short-lived workload pull receipt"
                )
            closure_private_subjects = {
                subject
                for subject in authorized["subjects"]
                if subject.split("/", 1)[0] in private_registries
            }
            if not private_subjects.issubset(closure_private_subjects):
                raise EvidenceError(
                    "private rendered images are absent from the signed release closure"
                )
            validate_workload_registry_auth_receipt(
                args.registry_auth_receipt.resolve(),
                trust,
                private_subjects,
                closure_private_subjects,
            )
        direct_values = (
            args.surfaces,
            args.surface_id,
            args.release_name,
            args.namespace,
            args.mode,
            args.chart_path,
        )
        if any(value is not None for value in direct_values):
            if any(value is None for value in direct_values):
                raise EvidenceError("direct release equality arguments are incomplete")
            from .release_image_closure import verify_direct_invocation
            verify_direct_invocation(
                root=source_root,
                manifest_path=args.surfaces.resolve(),
                trust_path=trust,
                closure_path=authorization,
                surface_id=args.surface_id,
                values_paths=args.values_file,
                release_name=args.release_name,
                namespace=args.namespace,
                mode=args.mode,
                chart_path=args.chart_path,
                rendered_manifest=rewritten.encode("utf-8"),
            )
    except (EvidenceError, ToolchainError) as exc:
        print(f"image post-render gate: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(rewritten)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
