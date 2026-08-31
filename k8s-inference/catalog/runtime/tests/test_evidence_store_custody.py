from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fs2_serve_catalog import evidence as evidence_module
from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import (
    create_signed_attestation,
    public_key_id,
    public_key_value,
)
from fs2_serve_catalog.evidence import EvidenceStore
from fs2_serve_catalog.loader import CatalogError


SCHEMA = "fs2-serve.nebius.ai/test-custodied-receipt/v1"
KIND = "custodied-receipts"
RAW_KIND = "custodied-raw-objects"
MODEL_ID = "proteinmpnn"
SESSION_ID = hashlib.sha256(b"evidence-store-custody-session").hexdigest()
VALIDATION_TIME = datetime(2026, 8, 27, 22, 22, tzinfo=timezone.utc)


class EvidenceStoreCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        public_key = self.private_key.public_key()
        self.trust = {public_key_id(public_key): public_key_value(public_key)}

    def build_evidence(self, root: Path) -> tuple[str, bytes]:
        unsigned = {"schema": SCHEMA, "status": "PASS", "value": "custodied"}
        digest = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
        subject = {**unsigned, "receipt_digest": digest}
        subject_raw = json.dumps(subject, sort_keys=True).encode() + b"\n"
        subject_directory = root / KIND
        subject_directory.mkdir(parents=True)
        (subject_directory / f"{digest}.json").write_bytes(subject_raw)

        attestation = create_signed_attestation(
            private_key=self.private_key,
            session_id=SESSION_ID,
            nonce=hashlib.sha256(b"evidence-store-custody-nonce").hexdigest(),
            issued_at="2026-08-27T22:20:00Z",
            expires_at="2026-08-27T23:00:00Z",
            kind=KIND,
            subject_schema=SCHEMA,
            subject_digest=digest,
            model_id=MODEL_ID,
            claims={"custody": "single-descriptor-root-dirfd"},
        )
        attestation_directory = root / "attestations" / KIND
        attestation_directory.mkdir(parents=True)
        (attestation_directory / f"{digest}.json").write_text(
            json.dumps(attestation) + "\n"
        )
        root.chmod(0o700)
        for path in root.rglob("*"):
            path.chmod(0o750 if path.is_dir() else 0o640)
        return digest, subject_raw

    def build_raw_object(self, root: Path) -> tuple[str, bytes, Path]:
        value = {"schema": SCHEMA, "value": "verified-raw-object"}
        raw = json.dumps(value, sort_keys=True).encode() + b"\n"
        digest = hashlib.sha256(raw).hexdigest()
        directory = root / RAW_KIND
        directory.mkdir(parents=True)
        subject = directory / f"{digest}.json"
        subject.write_bytes(raw)
        attestation = create_signed_attestation(
            private_key=self.private_key,
            session_id=SESSION_ID,
            nonce=hashlib.sha256(b"raw-object-custody-nonce").hexdigest(),
            issued_at="2026-08-27T22:20:00Z",
            expires_at="2026-08-27T23:00:00Z",
            kind=RAW_KIND,
            subject_schema=SCHEMA,
            subject_digest=digest,
            model_id=MODEL_ID,
            claims={"custody": "single-descriptor-raw-object"},
        )
        attestation_directory = root / "attestations" / RAW_KIND
        attestation_directory.mkdir(parents=True)
        (attestation_directory / f"{digest}.json").write_text(
            json.dumps(attestation) + "\n"
        )
        root.chmod(0o700)
        for path in root.rglob("*"):
            path.chmod(0o750 if path.is_dir() else 0o640)
        return digest, raw, subject

    def store(self, root: Path) -> EvidenceStore:
        return EvidenceStore(
            root,
            session_id=SESSION_ID,
            trusted_attestors=self.trust,
            validation_time=VALIDATION_TIME,
        )

    @staticmethod
    def changed_stat(info: os.stat_result, **changes: int) -> SimpleNamespace:
        values = {
            name: getattr(info, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_uid",
                "st_gid",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_root_descriptor_remains_authoritative_after_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "evidence"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            with self.store(root) as store:
                original = workspace / "opened-root"
                root.rename(original)
                root.mkdir()
                malicious = root / KIND
                malicious.mkdir()
                (malicious / f"{digest}.json").write_text('{"substituted":true}\n')
                value = store.receipt(KIND, digest, SCHEMA, MODEL_ID)
            self.assertEqual("custodied", value["value"])

    def test_leaf_and_intermediate_symlinks_are_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            real_parent = workspace / "real-parent"
            real_parent.mkdir()
            root = real_parent / "root-evidence"
            root.mkdir()
            self.build_evidence(root)
            (workspace / "root-alias").symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(CatalogError, "root cannot be opened"):
                self.store(workspace / "root-alias" / "root-evidence")

            root = workspace / "leaf-evidence"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            leaf = root / KIND / f"{digest}.json"
            external = workspace / "external-receipt.json"
            leaf.rename(external)
            leaf.symlink_to(external)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "absent or unsafe"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

            root = workspace / "intermediate-evidence"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            external_directory = workspace / "external-kind"
            (root / KIND).rename(external_directory)
            (root / KIND).symlink_to(external_directory, target_is_directory=True)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "absent or unsafe"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

    def test_leaf_atomic_replacement_is_detected_after_single_fd_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir()
            digest, subject_raw = self.build_evidence(root)
            subject = root / KIND / f"{digest}.json"
            replacement = root / KIND / "replacement.json"
            replacement.write_bytes(subject_raw)
            replacement.chmod(0o640)
            original_read = os.read
            replaced = False

            def replace_after_read(descriptor: int, count: int) -> bytes:
                nonlocal replaced
                value = original_read(descriptor, count)
                if not replaced:
                    replaced = True
                    os.replace(replacement, subject)
                return value

            with self.store(root) as store:
                with patch(
                    "fs2_serve_catalog.evidence.os.read", side_effect=replace_after_read
                ):
                    with self.assertRaisesRegex(CatalogError, "changed during"):
                        store.receipt(KIND, digest, SCHEMA, MODEL_ID)

    def test_intermediate_atomic_replacement_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "evidence"
            root.mkdir()
            digest, subject_raw = self.build_evidence(root)
            replacement = root / "replacement-kind"
            replacement.mkdir()
            (replacement / f"{digest}.json").write_bytes(subject_raw)
            replacement.chmod(0o750)
            (replacement / f"{digest}.json").chmod(0o640)
            original = root / "opened-kind"
            original_read = os.read
            replaced = False

            def replace_directory_after_read(descriptor: int, count: int) -> bytes:
                nonlocal replaced
                value = original_read(descriptor, count)
                if not replaced:
                    replaced = True
                    (root / KIND).rename(original)
                    replacement.rename(root / KIND)
                return value

            with self.store(root) as store:
                with patch(
                    "fs2_serve_catalog.evidence.os.read",
                    side_effect=replace_directory_after_read,
                ):
                    with self.assertRaisesRegex(CatalogError, "directory changed"):
                        store.receipt(KIND, digest, SCHEMA, MODEL_ID)

    def test_attestation_intermediate_symlinks_are_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)

            root = workspace / "attestations-parent-evidence"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            external = workspace / "external-attestations"
            (root / "attestations").rename(external)
            (root / "attestations").symlink_to(external, target_is_directory=True)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "absent or unsafe"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

            root = workspace / "attestation-kind-evidence"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            external = workspace / "external-attestation-kind"
            (root / "attestations" / KIND).rename(external)
            (root / "attestations" / KIND).symlink_to(
                external, target_is_directory=True
            )
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "absent or unsafe"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

    def test_attestation_intermediate_atomic_replacement_is_detected(self) -> None:
        for component in ("attestations", f"attestations/{KIND}"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                root.mkdir()
                digest, _ = self.build_evidence(root)
                attestation = root / "attestations" / KIND / f"{digest}.json"
                attestation_inode = attestation.stat().st_ino
                target = root / component
                original = target.with_name(f"opened-{target.name}")
                replacement = target.with_name(f"replacement-{target.name}")
                if component == "attestations":
                    replacement_leaf = replacement / KIND / f"{digest}.json"
                else:
                    replacement_leaf = replacement / f"{digest}.json"
                replacement_leaf.parent.mkdir(parents=True)
                replacement_leaf.write_bytes(attestation.read_bytes())
                replacement_leaf.chmod(0o640)
                for directory in (replacement, replacement_leaf.parent):
                    directory.chmod(0o750)

                original_read = os.read
                replaced = False

                def replace_directory_after_attestation_read(
                    descriptor: int, count: int
                ) -> bytes:
                    nonlocal replaced
                    value = original_read(descriptor, count)
                    if (
                        not replaced
                        and value
                        and os.fstat(descriptor).st_ino == attestation_inode
                    ):
                        replaced = True
                        target.rename(original)
                        replacement.rename(target)
                    return value

                with self.store(root) as store:
                    with patch(
                        "fs2_serve_catalog.evidence.os.read",
                        side_effect=replace_directory_after_attestation_read,
                    ):
                        with self.assertRaisesRegex(CatalogError, "directory changed"):
                            store.receipt(KIND, digest, SCHEMA, MODEL_ID)
                self.assertTrue(replaced)

    def test_pinned_root_metadata_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            original_read = os.read
            changed = False

            def make_root_writable_after_read(descriptor: int, count: int) -> bytes:
                nonlocal changed
                value = original_read(descriptor, count)
                if not changed:
                    changed = True
                    root.chmod(0o770)
                return value

            with self.store(root) as store:
                with patch(
                    "fs2_serve_catalog.evidence.os.read",
                    side_effect=make_root_writable_after_read,
                ):
                    with self.assertRaisesRegex(
                        CatalogError, "root is group/world writable|root changed"
                    ):
                        store.receipt(KIND, digest, SCHEMA, MODEL_ID)

    def test_owner_mode_and_regular_file_link_count_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)

            root = workspace / "writable-root"
            root.mkdir()
            self.build_evidence(root)
            root.chmod(0o770)
            with self.assertRaisesRegex(CatalogError, "root is group/world writable"):
                self.store(root)

            root = workspace / "writable-directory"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            (root / KIND).chmod(0o770)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "directory.*writable"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

            root = workspace / "writable-leaf"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            leaf = root / KIND / f"{digest}.json"
            leaf.chmod(0o660)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "subject is group/world writable"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

            root = workspace / "hardlinked-leaf"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            leaf = root / KIND / f"{digest}.json"
            (root / KIND / "second-name.json").hardlink_to(leaf)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "unsafe link count"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

            root = workspace / "foreign-owner"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            original_fstat = os.fstat
            targets = (
                ("root-uid", root.stat().st_ino, "st_uid", True),
                ("directory-gid", (root / KIND).stat().st_ino, "st_gid", False),
                (
                    "leaf-uid",
                    (root / KIND / f"{digest}.json").stat().st_ino,
                    "st_uid",
                    False,
                ),
                (
                    "leaf-gid",
                    (root / KIND / f"{digest}.json").stat().st_ino,
                    "st_gid",
                    False,
                ),
            )
            for case, inode, field, fails_at_open in targets:
                with self.subTest(case=case):

                    def foreign_owner(descriptor: int) -> object:
                        info = original_fstat(descriptor)
                        if info.st_ino != inode:
                            return info
                        return self.changed_stat(
                            info, **{field: getattr(info, field) + 1}
                        )

                    with patch(
                        "fs2_serve_catalog.evidence.os.fstat",
                        side_effect=foreign_owner,
                    ):
                        if fails_at_open:
                            with self.assertRaisesRegex(CatalogError, "unexpected owner"):
                                self.store(root)
                        else:
                            with self.store(root) as store:
                                with self.assertRaisesRegex(
                                    CatalogError, "unexpected owner"
                                ):
                                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

            link_targets = (
                ("root", root.stat().st_ino, True),
                ("directory", (root / KIND).stat().st_ino, False),
            )
            for case, inode, fails_at_open in link_targets:
                with self.subTest(case=f"{case}-link-count"):

                    def unsafe_directory_link_count(descriptor: int) -> object:
                        info = original_fstat(descriptor)
                        if info.st_ino == inode:
                            return self.changed_stat(info, st_nlink=1)
                        return info

                    with patch(
                        "fs2_serve_catalog.evidence.os.fstat",
                        side_effect=unsafe_directory_link_count,
                    ):
                        if fails_at_open:
                            with self.assertRaisesRegex(CatalogError, "unsafe link count"):
                                self.store(root)
                        else:
                            with self.store(root) as store:
                                with self.assertRaisesRegex(
                                    CatalogError, "unsafe link count"
                                ):
                                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

    def test_bounded_strict_parser_normalizes_receipt_failures(self) -> None:
        node_heavy = (
            b"{"
            + b",".join(
                f'"k{index}":'.encode() + b"[0,0,0,0,0,0,0,0,0,0]"
                for index in range(10_000)
            )
            + b"}\n"
        )
        cases = (
            (
                b'{"value":' + b"[" * 65 + b"0" + b"]" * 65 + b"}\n",
                "nesting exceeds",
            ),
            (b'{"value":' + b"9" * 1000 + b"}\n", "integer exceeds"),
            (b'{"value":NaN}\n', "constant is forbidden"),
            (b'{"value":Infinity}\n', "constant is forbidden"),
            (b'{"value":-Infinity}\n', "constant is forbidden"),
            (b'{"value":1e999}\n', "number exceeds"),
            (b'{"value":1e19}\n', "number exceeds"),
            (b'{"value":1,"value":2}\n', "duplicate JSON key"),
            (b'{"value":[}\n', "delimiters are unbalanced"),
            (b'{"value":"\xff"}\n', "not bounded strict UTF-8 JSON"),
            (node_heavy, "node count exceeds"),
            (
                b'{"value":[' + b",".join([b"0"] * 10_001) + b"]}\n",
                "collection size exceeds",
            ),
            (
                b'{"value":"' + b"x" * (1024 * 1024 + 1) + b'"}\n',
                "string exceeds",
            ),
        )
        for raw, pattern in cases:
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                root.mkdir()
                digest, _ = self.build_evidence(root)
                subject = root / KIND / f"{digest}.json"
                subject.write_bytes(raw)
                subject.chmod(0o640)
                with self.store(root) as store:
                    with self.assertRaisesRegex(CatalogError, pattern):
                        store.receipt(KIND, digest, SCHEMA, MODEL_ID)

    def test_strict_parser_covers_attestations_manifests_and_raw_objects(self) -> None:
        deep = b'{"value":' + b"[" * 65 + b"0" + b"]" * 65 + b"}\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "attestation-evidence"
            root.mkdir()
            digest, _ = self.build_evidence(root)
            attestation = root / "attestations" / KIND / f"{digest}.json"
            attestation.write_bytes(deep)
            attestation.chmod(0o640)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "nesting exceeds"):
                    store.receipt(KIND, digest, SCHEMA, MODEL_ID)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "manifest-evidence"
            root.mkdir(mode=0o700)
            manifest_digest = hashlib.sha256(b"malformed-manifest").hexdigest()
            directory = root / "artifacts"
            directory.mkdir(mode=0o750)
            manifest = directory / f"{manifest_digest}.json"
            manifest.write_bytes(deep)
            manifest.chmod(0o640)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "nesting exceeds"):
                    store.artifact(manifest_digest, MODEL_ID)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "raw-object-evidence"
            root.mkdir(mode=0o700)
            raw_digest = hashlib.sha256(deep).hexdigest()
            directory = root / "variant-supply-objects"
            directory.mkdir(mode=0o750)
            raw_object = directory / f"{raw_digest}.json"
            raw_object.write_bytes(deep)
            raw_object.chmod(0o640)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "nesting exceeds"):
                    store.raw_object(
                        "variant-supply-objects", raw_digest, SCHEMA, MODEL_ID
                    )

    def test_raw_object_atomic_swap_during_descriptor_read_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir()
            digest, raw, subject = self.build_raw_object(root)
            replacement = root / RAW_KIND / "replacement.json"
            replacement.write_text(json.dumps({"schema": SCHEMA, "value": "evil"}))
            replacement.chmod(0o640)
            original_read = os.read
            replaced = False

            def replace_after_read(descriptor: int, count: int) -> bytes:
                nonlocal replaced
                value = original_read(descriptor, count)
                if not replaced:
                    replaced = True
                    os.replace(replacement, subject)
                return value

            with self.store(root) as store:
                with patch(
                    "fs2_serve_catalog.evidence.os.read", side_effect=replace_after_read
                ):
                    with self.assertRaisesRegex(CatalogError, "changed during"):
                        store.raw_object(RAW_KIND, digest, SCHEMA, MODEL_ID)
            self.assertNotEqual(raw, subject.read_bytes())

    def test_raw_object_digest_is_verified_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir()
            digest, _, subject = self.build_raw_object(root)
            subject.write_text(json.dumps({"schema": SCHEMA, "value": "evil"}))
            subject.chmod(0o640)
            with self.store(root) as store:
                with patch(
                    "fs2_serve_catalog.evidence._strict_evidence_json_object",
                    side_effect=AssertionError(
                        "unverified raw-object bytes reached the parser"
                    ),
                ):
                    with self.assertRaisesRegex(
                        CatalogError, "raw object filename/digest binding failed"
                    ):
                        store.raw_object(RAW_KIND, digest, SCHEMA, MODEL_ID)

    def test_raw_object_swap_after_read_parses_verified_bytes_without_path_reopen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir()
            digest, raw, subject = self.build_raw_object(root)
            replacement = root / RAW_KIND / "replacement.json"
            replacement.write_text(json.dumps({"schema": SCHEMA, "value": "evil"}))
            replacement.chmod(0o640)

            strict_parser = evidence_module._strict_evidence_json_object
            replaced = False

            def replace_before_parse(value: bytes, label: str) -> dict[str, object]:
                nonlocal replaced
                if not replaced:
                    replaced = True
                    os.replace(replacement, subject)
                return strict_parser(value, label)

            with self.store(root) as store:
                with (
                    patch(
                        "fs2_serve_catalog.evidence._strict_evidence_json_object",
                        side_effect=replace_before_parse,
                    ),
                    patch.object(
                        Path,
                        "read_bytes",
                        side_effect=AssertionError("pathname was reopened"),
                    ),
                ):
                    value, returned_raw = store.raw_object(
                        RAW_KIND, digest, SCHEMA, MODEL_ID
                    )
            self.assertEqual("verified-raw-object", value["value"])
            self.assertEqual(raw, returned_raw)
            self.assertEqual("evil", json.loads(subject.read_text())["value"])

    def test_raw_object_leaf_and_intermediate_symlinks_return_no_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "leaf-evidence"
            root.mkdir()
            digest, _, subject = self.build_raw_object(root)
            external = workspace / "external-raw-object.json"
            subject.rename(external)
            subject.symlink_to(external)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "absent or unsafe"):
                    store.raw_object(RAW_KIND, digest, SCHEMA, MODEL_ID)

            root = workspace / "intermediate-evidence"
            root.mkdir()
            digest, _, _ = self.build_raw_object(root)
            external_directory = workspace / "external-raw-kind"
            (root / RAW_KIND).rename(external_directory)
            (root / RAW_KIND).symlink_to(external_directory, target_is_directory=True)
            with self.store(root) as store:
                with self.assertRaisesRegex(CatalogError, "absent or unsafe"):
                    store.raw_object(RAW_KIND, digest, SCHEMA, MODEL_ID)


if __name__ == "__main__":
    unittest.main()
