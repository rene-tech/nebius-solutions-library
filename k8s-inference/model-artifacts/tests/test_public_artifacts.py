from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile

import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "catalog" / "runtime"))

from public_artifacts import (  # noqa: E402
    ContractError,
    MAX_DOWNLOAD_CONCURRENCY,
    _download,
    artifact_consumer_bindings,
    artifact_destination,
    artifact_runtime_handoffs,
    canonical_json,
    offline_smoke,
    readiness,
    load_json,
    materialize_protenix_source,
    protenix_localized_inventory,
    sha256_bytes,
    stage_artifact,
    validate_catalog,
)
from render_jobs import DEFAULT_IMAGE, render  # noqa: E402
from fs2_serve_catalog.artifacts import load_artifact_manifest  # noqa: E402


class RangeHandler(BaseHTTPRequestHandler):
    payload = b""
    ranges: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802
        requested = self.headers.get("Range")
        self.__class__.ranges.append(requested)
        start = 0
        if requested:
            start = int(requested.removeprefix("bytes=").removesuffix("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(self.payload)-1}/{len(self.payload)}")
        else:
            self.send_response(200)
        body = self.payload[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@contextmanager
def server(payload: bytes):
    RangeHandler.payload = payload
    RangeHandler.ranges = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://invalid.example/{httpd.server_port}/fixture.json", httpd.server_port
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def fixture(root: Path, url: str, payload: bytes, *, sha: str | None = None) -> tuple[Path, dict]:
    files = [{"path": "config.json", "bytes": len(payload), "sha256": sha or hashlib.sha256(payload).hexdigest()}]
    manifest = {
        "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
        "model_id": "fixture-model",
        "kind": "weights",
        "source": {"uri": "https://invalid.example/fixture", "revision": "fixture-v1"},
        "content": {"digest": sha256_bytes(canonical_json(files)), "expanded_bytes": len(payload), "files": files},
        "license": {"id": "MIT", "state": "verified"},
        "entitlement_state": "not-required",
        "owner": "fs2-tests",
        "retention": "retained-platform",
    }
    catalog = {
        "schema": "fs2-serve.nebius.ai/public-artifact-catalog/v1",
        "generated_at": "2026-09-02T00:00:00Z",
        "licenses": {"MIT": {"id": "MIT", "url": "https://opensource.org/license/mit", "commercial_use": "permitted", "redistribution": "permitted-with-notice"}},
        "artifacts": {
            "fixture": {
                "id": "fixture", "family": "fixture", "state": "available", "reason": None,
                "consumers": ["fixture-model"], "manifest": "fixture-manifest.json",
                "sources": [{"path": "config.json", "url": url, "bytes": len(payload), "sha256": sha or hashlib.sha256(payload).hexdigest()}],
                "offline_smoke": "checkpoint",
            },
            "private": {
                "id": "private", "family": "private", "state": "excluded-private",
                "reason": "private acceptance and storage are required", "consumers": ["private-model"],
            },
        },
        "consumers": {"fixture-model": ["fixture"], "private-model": ["private"]},
        "consumer_layouts": {
            "fixture-model": {
                "mount_root": "/models",
                "bindings": [
                    {
                        "artifact_id": "fixture",
                        "mount_path": "/models/fixture",
                        "read_only": True,
                    }
                ],
            }
        },
        "private_layouts": {},
        "reference_layouts": {},
        "runtime_constraints": {},
        "runtime_handoffs": {},
    }
    write_json(root / "fixture-manifest.json", manifest)
    write_json(root / "artifact-catalog.json", catalog)
    return root / "artifact-catalog.json", manifest


class PublicArtifactTests(unittest.TestCase):
    def test_public_export_excludes_private_nebius_resource_and_registry_identities(self) -> None:
        repository_root = ROOT.parent.parent
        tracked = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "k8s-inference/model-artifacts",
            ],
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        forbidden_fragments = ("/home" + "/tux",)
        resource_identity = (
            r"\b(?:project|[a-z][a-z0-9]*(?:filesystem|instance|nodegroup|registry|"
            r"capacityblock|endpoint|cluster|network|subnet|serviceaccount))"
            r"-e[0-9]{2}[0-9a-z]{3,}\b"
        )
        regional_registry = (
            r"\bcr\.[a-z0-9-]+\." + "nebius" + r"\.cloud/[^\s\"']+"
        )
        forbidden_patterns = (
            ("Nebius resource identity", re.compile(resource_identity)),
            ("Nebius regional registry path", re.compile(regional_registry)),
        )
        violations: list[str] = []
        for raw_path in tracked:
            if not raw_path:
                continue
            relative = raw_path.decode("utf-8")
            content = (repository_root / relative).read_text(
                encoding="utf-8", errors="replace"
            )
            for fragment in forbidden_fragments:
                if fragment in content:
                    violations.append(f"{relative}: private path {fragment}")
            for description, pattern in forbidden_patterns:
                if match := pattern.search(content):
                    violations.append(
                        f"{relative}: private {description} {match.group(0)}"
                    )
        self.assertEqual([], violations)

    def metadata(self) -> dict[str, object]:
        return {
            "project_id": "project-test", "region": "region-test", "cluster": "cluster-test",
            "filesystem_id": "computefilesystem-test", "filesystem_size_gib": 2048,
            "namespace": "fs2-reference-data", "local_queue": "reference-data",
            "cpu_pool_id": "computenodegroup-test", "cpu_pool_name": "reference-data-cpu",
            "cpu_pool_label": "reference-data", "shared_filesystem_host_path": "/mnt/reference-data",
            "cache_subpath": "model-artifacts/public/v1",
            "reference_plane_source_commit": "abcdef0123456789",
            "source_commit": "0123456789abcdef",
        }

    def test_resume_verify_atomic_publish_and_receipt(self) -> None:
        payload = b'{"model":"fixture","valid":true}\n'
        with tempfile.TemporaryDirectory() as temp, server(payload) as (_, port):
            root = Path(temp)
            url = f"http://127.0.0.1:{port}/fixture.json"
            catalog_path, manifest = fixture(root, "https://invalid.example/fixture.json", payload)
            document = json.loads(catalog_path.read_text())
            document["artifacts"]["fixture"]["sources"][0]["url"] = url.replace("http://", "https://")
            write_json(catalog_path, document)
            # Validation is deliberately HTTPS-only. Patch the already-validated URL for the local transport test.
            catalog = validate_catalog(json.loads(catalog_path.read_text()), catalog_path)
            catalog["artifacts"]["fixture"]["sources"][0]["url"] = url
            staging = root / "cache/.staging/fixture" / catalog["artifacts"]["fixture"]["_manifest_digest"] / "downloads"
            staging.mkdir(parents=True)
            (staging / "config.json.part").write_bytes(payload[:7])
            import public_artifacts
            original = public_artifacts.validate_catalog
            public_artifacts.validate_catalog = lambda *_args: catalog
            try:
                receipt = stage_artifact(catalog_path, "fixture", root / "cache", self.metadata())
            finally:
                public_artifacts.validate_catalog = original
            self.assertEqual(["bytes=7-"], RangeHandler.ranges)
            self.assertEqual("verified", receipt["state"])
            self.assertTrue(receipt["cache_uri"].startswith("nebius-sharedfs://computefilesystem-test/"))
            self.assertEqual(2048, receipt["storage"]["filesystem_size_gib"])
            self.assertEqual("/models/fixture", receipt["consumer_bindings"][0]["mount_path"])
            self.assertEqual([], receipt["runtime_handoffs"])
            Draft202012Validator(load_json(ROOT / "cache-receipt.schema.json")).validate(receipt)
            destination = artifact_destination(root / "cache", manifest)
            self.assertEqual(payload, (destination / "config.json").read_bytes())
            self.assertTrue((root / "cache/receipts/fixture").is_dir())

    def test_complete_verified_partial_is_reused_without_network(self) -> None:
        payload = b"already complete and verified"
        source = {
            "path": "model.bin",
            "url": "https://invalid.example/model.bin",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "model.bin.part"
            target.write_bytes(payload)
            import public_artifacts
            original = public_artifacts.request.urlopen
            public_artifacts.request.urlopen = lambda *_args, **_kwargs: self.fail(
                "a complete verified partial must not issue a network request"
            )
            try:
                _download(source, target)
            finally:
                public_artifacts.request.urlopen = original
            self.assertEqual(payload, target.read_bytes())

    def test_source_downloads_are_parallel_and_bounded(self) -> None:
        payloads = {
            "config.json": b'{"model":"fixture"}\n',
            "weights-1.bin": b"weights-one",
            "weights-2.bin": b"weights-two",
            "weights-3.bin": b"weights-three",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog_path, _ = fixture(
                root,
                "https://invalid.example/config.json",
                payloads["config.json"],
            )
            catalog = validate_catalog(load_json(catalog_path), catalog_path)
            files = [
                {
                    "path": path,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for path, payload in payloads.items()
            ]
            entry = catalog["artifacts"]["fixture"]
            entry["sources"] = [
                {**item, "url": f"https://invalid.example/{item['path']}"}
                for item in files
            ]
            entry["_manifest"]["content"] = {
                "digest": sha256_bytes(canonical_json(files)),
                "expanded_bytes": sum(item["bytes"] for item in files),
                "files": files,
            }
            guard = threading.Lock()
            pair_started = threading.Barrier(2)
            active = 0
            max_active = 0

            def fake_download(source: dict[str, object], target: Path) -> None:
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                pair_started.wait(timeout=5)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payloads[str(source["path"])])
                with guard:
                    active -= 1

            import public_artifacts
            original_validate = public_artifacts.validate_catalog
            original_download = public_artifacts._download
            public_artifacts.validate_catalog = lambda *_args: catalog
            public_artifacts._download = fake_download
            try:
                receipt = stage_artifact(
                    catalog_path,
                    "fixture",
                    root / "cache",
                    self.metadata(),
                    download_concurrency=2,
                )
            finally:
                public_artifacts._download = original_download
                public_artifacts.validate_catalog = original_validate
            self.assertEqual("verified", receipt["state"])
            self.assertEqual(2, max_active)

    def test_download_concurrency_is_strictly_bounded(self) -> None:
        payload = b'{"model":"fixture"}\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog_path, _ = fixture(
                root, "https://invalid.example/fixture.json", payload
            )
            for invalid in (False, 0, MAX_DOWNLOAD_CONCURRENCY + 1):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ContractError, "download concurrency"):
                        stage_artifact(
                            catalog_path,
                            "fixture",
                            root / "cache",
                            self.metadata(),
                            download_concurrency=invalid,
                        )

    def test_checksum_failure_never_publishes(self) -> None:
        payload = b'{"model":"fixture"}\n'
        with tempfile.TemporaryDirectory() as temp, server(payload) as (_, port):
            root = Path(temp)
            catalog_path, manifest = fixture(root, "https://invalid.example/fixture.json", payload, sha="0" * 64)
            catalog = validate_catalog(json.loads(catalog_path.read_text()), catalog_path)
            catalog["artifacts"]["fixture"]["sources"][0]["url"] = f"http://127.0.0.1:{port}/fixture.json"
            import public_artifacts
            original = public_artifacts.validate_catalog
            public_artifacts.validate_catalog = lambda *_args: catalog
            try:
                with self.assertRaisesRegex(ContractError, "SHA-256 mismatch"):
                    stage_artifact(catalog_path, "fixture", root / "cache", self.metadata())
            finally:
                public_artifacts.validate_catalog = original
            self.assertFalse(artifact_destination(root / "cache", manifest).exists())

    def test_mirror_provenance_is_bound_into_the_immutable_receipt(self) -> None:
        payload = b'{"model":"fixture"}\n'
        with tempfile.TemporaryDirectory() as temp, server(payload) as (_, port):
            root = Path(temp)
            catalog_path, manifest = fixture(root, "https://invalid.example/fixture.json", payload)
            revision = "a" * 40
            mirror_revision = "b" * 40
            inventory_digest = "c" * 64
            sha256 = hashlib.sha256(payload).hexdigest()
            md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
            manifest["source"] = {
                "uri": "https://invalid.example/composite",
                "revision": f"code-{revision}_checkpoint-{mirror_revision}",
            }
            write_json(root / "fixture-manifest.json", manifest)
            evidence = {
                "schema": "fs2-serve.nebius.ai/third-party-model-mirror-verification/v1",
                "artifact_id": "fixture",
                "conclusion": "mirror-verified-not-publisher-byte-compared",
                "byte_verification": {
                    "bytes": len(payload), "sha256": sha256, "md5": md5,
                    "all_expected_values_match": True,
                },
                "checkpoint_inspection": {
                    "load_mode": "weights-only-mmap-cpu", "root_type": "dict",
                    "state_key": "model", "state_key_count": 1, "tensor_count": 1,
                    "tensor_dtype_counts": {"torch.float32": 1},
                    "parameter_count": 1, "element_count": 1,
                    "key_shape_inventory_sha256": inventory_digest,
                },
                "pinned_runtime_image_inspection": {
                    "image_digest": f"sha256:{'d' * 64}",
                    "image_qualification_state": "unqualified-inspection-only",
                    "network_mode": "none", "torch_version": "2.7.1+cu126",
                    "load_mode": "weights-only-mmap-cpu", "root_type": "dict",
                    "top_level_keys": ["model"], "tensor_count": 1,
                    "tensor_dtype_counts": {"torch.float32": 1}, "element_count": 1,
                },
                "source_architecture": {
                    "state_key_count": 1, "parameter_count": 1,
                    "key_shape_inventory_sha256": inventory_digest,
                },
                "comparison": {
                    "missing_key_count": 0, "unexpected_key_count": 0,
                    "shape_mismatch_count": 0, "strict_key_shape_match": True,
                },
                "canonical_source": {
                    "official_uri": "https://invalid.example/fixture",
                    "source_revision": revision, "publisher_byte_compared": False,
                },
                "mirror": {
                    "relationship": "third-party-mirror", "repository": "example/mirror",
                    "repository_revision": mirror_revision, "lfs_oid_sha256": sha256,
                    "lfs_size": len(payload),
                },
            }
            write_json(root / "evidence/mirror.json", evidence)
            document = load_json(catalog_path)
            document["artifacts"]["fixture"]["provenance"] = {
                "state": "mirror-verified-not-publisher-byte-compared",
                "canonical_source": {
                    "publisher": "Fixture Publisher",
                    "uri": "https://invalid.example/fixture",
                    "source_revision": revision,
                    "publisher_bytes_reachable": False,
                    "publisher_digest_available": False,
                },
                "acquisition_source": {
                    "relationship": "third-party-mirror",
                    "repository": "example/mirror",
                    "repository_revision": mirror_revision,
                    "url": "https://invalid.example/fixture.json",
                    "lfs_oid_sha256": sha256,
                    "bytes": len(payload),
                },
                "verification": {
                    "evidence": "evidence/mirror.json",
                    "evidence_sha256": hashlib.sha256((root / "evidence/mirror.json").read_bytes()).hexdigest(),
                    "sha256": sha256, "md5": md5,
                    "safe_torch_load": "weights-only-mmap-cpu", "root_type": "dict",
                    "top_level_key": "model", "checkpoint_key_count": 1,
                    "checkpoint_tensor_count": 1,
                    "checkpoint_tensor_dtypes": {"torch.float32": 1},
                    "source_state_key_count": 1, "checkpoint_parameter_count": 1,
                    "checkpoint_element_count": 1, "source_parameter_count": 1,
                    "key_shape_inventory_sha256": inventory_digest,
                    "inspection_image_digest": f"sha256:{'d' * 64}",
                    "inspection_torch_version": "2.7.1+cu126",
                    "strict_key_shape_match": True, "publisher_byte_compared": False,
                },
            }
            write_json(catalog_path, document)
            catalog = validate_catalog(load_json(catalog_path), catalog_path)
            catalog["artifacts"]["fixture"]["sources"][0]["url"] = f"http://127.0.0.1:{port}/fixture.json"
            import public_artifacts
            original = public_artifacts.validate_catalog
            public_artifacts.validate_catalog = lambda *_args: catalog
            try:
                receipt = stage_artifact(catalog_path, "fixture", root / "cache", self.metadata())
            finally:
                public_artifacts.validate_catalog = original
            self.assertEqual(
                "mirror-verified-not-publisher-byte-compared", receipt["provenance"]["state"]
            )
            Draft202012Validator(load_json(ROOT / "cache-receipt.schema.json")).validate(receipt)

    def test_readiness_is_fail_closed_for_absent_and_private_receipts(self) -> None:
        payload = b'{"model":"fixture"}\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog_path, _ = fixture(root, "https://invalid.example/fixture.json", payload)
            result = readiness(catalog_path, root / "cache")
            self.assertFalse(result["consumers"]["fixture-model"]["ready"])
            self.assertFalse(result["consumers"]["private-model"]["ready"])
            self.assertIn("acceptance", result["consumers"]["private-model"]["artifacts"][0]["reason"])

    def test_readiness_requires_exact_primary_localization_receipt(self) -> None:
        payload = b'{"model":"fixture"}\n'
        payload_sha = hashlib.sha256(payload).hexdigest()
        tree_sha = "a" * 64
        with tempfile.TemporaryDirectory() as temp, server(payload) as (_, port):
            root = Path(temp)
            catalog_path, _ = fixture(root, "https://invalid.example/fixture.json", payload)
            document = load_json(catalog_path)
            document["artifacts"]["fixture"]["localization"] = {
                "receipt_schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
                "transform": "safe-extract-tar",
                "archive_sha256": payload_sha,
                "mount_paths": ["/models/fixture"],
                "tree": {
                    "entry_count": 2,
                    "total_bytes": 42,
                    "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                    "inventory_sha256": tree_sha,
                },
            }
            write_json(catalog_path, document)
            catalog = validate_catalog(load_json(catalog_path), catalog_path)
            catalog["artifacts"]["fixture"]["sources"][0]["url"] = (
                f"http://127.0.0.1:{port}/fixture.json"
            )
            import public_artifacts
            original = public_artifacts.validate_catalog
            public_artifacts.validate_catalog = lambda *_args: catalog
            try:
                stage_artifact(catalog_path, "fixture", root / "cache", self.metadata())
            finally:
                public_artifacts.validate_catalog = original

            missing = readiness(catalog_path, root / "cache")
            artifact = missing["consumers"]["fixture-model"]["artifacts"][0]
            self.assertFalse(artifact["ready"])
            self.assertIn("localization receipt is absent", artifact["reason"])

            localization_receipt = {
                "schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
                "artifact_id": "fixture",
                "observed_at": "2026-09-03T00:00:00Z",
                "mount_path": "/models/fixture",
                "state": "verified",
                "archive_provenance": {
                    "filename": "fixture.tar",
                    "bytes": len(payload),
                    "sha256": payload_sha,
                    "source_uri": "https://invalid.example/fixture",
                    "source_revision": "fixture-v1",
                    "license_id": "MIT",
                    "present_in_mount": False,
                },
                "tree_identity": {
                    "entry_count": 2,
                    "total_bytes": 42,
                    "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                    "inventory_sha256": tree_sha,
                    "probe_entries_verified": 2,
                },
            }
            write_json(
                root / "cache/localization-receipts/fixture" / f"{tree_sha}.json",
                localization_receipt,
            )
            ready = readiness(catalog_path, root / "cache")
            artifact = ready["consumers"]["fixture-model"]["artifacts"][0]
            self.assertTrue(artifact["ready"])
            self.assertRegex(artifact["localization_receipt_digest"], r"^[a-f0-9]{64}$")

            localization_receipt["tree_identity"]["inventory_sha256"] = "b" * 64
            write_json(
                root / "cache/localization-receipts/fixture" / f"{tree_sha}.json",
                localization_receipt,
            )
            tampered = readiness(catalog_path, root / "cache")
            artifact = tampered["consumers"]["fixture-model"]["artifacts"][0]
            self.assertFalse(artifact["ready"])
            self.assertIn("failed identity validation", artifact["reason"])

    def test_esm_offline_smoke_binds_index_tokenizer_and_all_shards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "trunk").mkdir()
            (root / "esmc").mkdir()
            write_json(root / "trunk/config.json", {"esmc_id": "biohub/ESMC-6B"})
            write_json(root / "esmc/config.json", {"model_type": "esmc"})
            write_json(root / "esmc/tokenizer.json", {"version": "1"})
            write_json(root / "esmc/model.safetensors.index.json", {"weight_map": {"x": "model-00001-of-00001.safetensors"}})
            header = json.dumps({"x": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
            (root / "esmc/model-00001-of-00001.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\0\0\0\0")
            checks = offline_smoke(root / "esmc", "hf-sharded-snapshot")
            self.assertIn("safetensors-index-all-shards", checks)

    def test_esmc_6b_smoke_requires_six_shards_and_all_tokenizer_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("config.json", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"):
                write_json(root / name, {"fixture": name})
            header = json.dumps({"x": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
            shard_names = [f"model-{index:05d}-of-00006.safetensors" for index in range(1, 7)]
            for shard in shard_names:
                (root / shard).write_bytes(struct.pack("<Q", len(header)) + header + b"\0\0\0\0")
            write_json(
                root / "model.safetensors.index.json",
                {"weight_map": {f"tensor-{index}": shard for index, shard in enumerate(shard_names)}},
            )
            checks = offline_smoke(root, "esmc-6b-snapshot")
            self.assertIn("esmc-6b-exact-file-set", checks)
            self.assertIn("esmc-6b-six-shards", checks)
            self.assertIn("esmc-6b-index-binds-all-six-shards", checks)
            self.assertIn("esmc-6b-tokenizer-support-files", checks)
            (root / "tokenizer_config.json").unlink()
            with self.assertRaisesRegex(ContractError, "does not exactly match"):
                offline_smoke(root, "esmc-6b-snapshot")

    def test_esmc_6b_smoke_rejects_incomplete_index_and_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("config.json", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"):
                write_json(root / name, {"fixture": name})
            header = json.dumps({"x": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
            shard_names = [f"model-{index:05d}-of-00006.safetensors" for index in range(1, 7)]
            for shard in shard_names:
                (root / shard).write_bytes(struct.pack("<Q", len(header)) + header + b"\0\0\0\0")
            index_path = root / "model.safetensors.index.json"
            write_json(
                index_path,
                {"weight_map": {f"tensor-{index}": shard for index, shard in enumerate(shard_names[:-1])}},
            )
            with self.assertRaisesRegex(ContractError, "does not bind exactly all six"):
                offline_smoke(root, "esmc-6b-snapshot")
            write_json(
                index_path,
                {"weight_map": {f"tensor-{index}": shard for index, shard in enumerate(shard_names)}},
            )
            write_json(root / "generation_config.json", {"extra": True})
            with self.assertRaisesRegex(ContractError, "does not exactly match"):
                offline_smoke(root, "esmc-6b-snapshot")

    def test_esmfold2_model_smoke_requires_exact_config_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "config.json", {"model_type": "esmfold2"})
            header = json.dumps({"x": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
            (root / "model.safetensors").write_bytes(
                struct.pack("<Q", len(header)) + header + b"\0\0\0\0"
            )
            checks = offline_smoke(root, "esmfold2-model-snapshot")
            self.assertIn("esmfold2-model-files-complete", checks)
            (root / "ccd.pkl").write_bytes(b"\x80\x04\x95\x00")
            with self.assertRaisesRegex(ContractError, "exactly config.json and model.safetensors"):
                offline_smoke(root, "esmfold2-model-snapshot")

    def test_ccd_smoke_requires_exact_standalone_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ccd.pkl").write_bytes(b"\x80\x04\x95\x00")
            checks = offline_smoke(root, "ccd-pickle")
            self.assertIn("esmfold2-ccd-complete", checks)
            (root / "extra.pkl").write_bytes(b"\x80\x04\x95\x00")
            with self.assertRaisesRegex(ContractError, "exactly ccd.pkl"):
                offline_smoke(root, "ccd-pickle")

    def test_openfold3_binarycif_smoke_requires_complete_ccd_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = (
                b"\x83\xaadataBlocks\x91\x82\xa6header\xaacomponents"
                b"\xaacategories\x93\xaa_chem_comp\xaf_chem_comp_atom"
                b"\xaf_chem_comp_bond\xa7encoder\xa7biotite\xa7version\xa50.3.0"
            )
            (root / "components.bcif").write_bytes(payload)
            checks = offline_smoke(root, "binarycif-ccd")
            self.assertIn("binarycif-ccd-required-categories", checks)
            self.assertIn("binarycif-biotite-0.3.0", checks)
            (root / "components.bcif").write_bytes(
                payload.replace(b"_chem_comp_bond", b"_chem_comp_link")
            )
            with self.assertRaisesRegex(ContractError, "lacks required BinaryCIF CCD markers"):
                offline_smoke(root, "binarycif-ccd")

    def test_protenix_materializer_creates_one_exact_composite_tree(self) -> None:
        payloads = {
            "checkpoint/protenix-v2.pt": b"PK\x03\x04fixture-checkpoint",
            "common/clusters-by-entity-40.txt": b"entity cluster\n",
            "common/components.cif": b"data_components\n_entry.id components\n",
            "common/components.cif.rdkit_mol.pkl": b"\x80\x04\x95fixture-pickle",
            "common/obsolete_release_date.csv": b"id,date\nold,2020-01-01\n",
        }
        files = sorted(
            [
                {
                    "path": path,
                    "bytes": len(value),
                    "sha256": hashlib.sha256(value).hexdigest(),
                }
                for path, value in payloads.items()
            ],
            key=lambda item: item["path"],
        )
        payload_manifest = {
            "content": {
                "digest": sha256_bytes(canonical_json(files)),
                "files": files,
            }
        }
        document, manifest_digest, localized_files, content_digest = (
            protenix_localized_inventory(files)
        )
        localization = {
            "source_content_digest_sha256": payload_manifest["content"]["digest"],
            "manifest_sha256": manifest_digest,
            "files": localized_files,
            "content_digest_sha256": content_digest,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            destination = root / "runtime/protenix-v2"
            for relative, value in payloads.items():
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            unsafe_parent = root / "linked-runtime"
            unsafe_parent.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ContractError, "parent is unsafe"):
                materialize_protenix_source(
                    source,
                    unsafe_parent / "protenix-v2",
                    payload_manifest,
                    localization,
                )
            self.assertEqual(
                content_digest,
                materialize_protenix_source(
                    source, destination, payload_manifest, localization
                ),
            )
            self.assertEqual(
                {item["path"] for item in localized_files},
                {
                    path.relative_to(destination).as_posix()
                    for path in destination.rglob("*")
                    if path.is_file()
                },
            )
            self.assertEqual(document, load_json(destination / "manifest.json"))
            self.assertEqual(
                manifest_digest,
                (destination / ".fs2-manifest-sha256").read_text().strip(),
            )
            destination.chmod(0o755)
            legacy = destination / "checkpoint/protenix-v2.pt.fs2.json"
            legacy.parent.chmod(0o755)
            legacy.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "contains extras"):
                materialize_protenix_source(
                    source, destination, payload_manifest, localization
                )

    def test_boltzgen_molecule_smoke_requires_exact_flat_archive(self) -> None:
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        def base36(value: int) -> str:
            digits = ""
            while value:
                value, remainder = divmod(value, 36)
                digits = alphabet[remainder] + digits
            return digits or "0"

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "mols.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                for index in range(45227):
                    archive.writestr(f"{base36(index)}.pkl", b"")
            checks = offline_smoke(root, "boltzgen-molecules-zip")
            self.assertIn("boltzgen-molecules-45227-flat-root-pkl-files", checks)
            archive_path.unlink()
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("nested/ABC.pkl", b"")
            with self.assertRaisesRegex(ContractError, "45,227 unique flat-root"):
                offline_smoke(root, "boltzgen-molecules-zip")

    def test_archive_smoke_supports_pinned_tar_gz_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "ColabDesign-revision.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                payload = b"pinned source\n"
                member = tarfile.TarInfo("source/README.md")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            self.assertEqual(
                ["tar-index:ColabDesign-revision.tar.gz"],
                offline_smoke(root, "archive"),
            )

    def test_renderer_is_cpu_only_digest_pinned_and_hardened(self) -> None:
        payload = b'{"model":"fixture"}\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog_path, _ = fixture(root, "https://invalid.example/fixture.json", payload)
            args = argparse.Namespace(
                catalog=catalog_path, artifact=["fixture"], namespace="fs2-reference-data",
                local_queue="reference-data", service_account="fs2-reference-data",
                shared_filesystem_host_path="/mnt/reference-data", cache_subpath="model-artifacts/public/v1",
                image=DEFAULT_IMAGE, project_id="project-test", region="region-test",
                cluster="cluster-test", filesystem_id="computefilesystem-test", filesystem_size_gib=2048,
                cpu_pool_id="computenodegroup-test", cpu_pool_name="reference-data-cpu",
                reference_plane_source_commit="abcdef0123456789", source_commit="0123456789abcdef",
                node_selector=json.dumps({
                    "workload.fs2.nebius/reference-data": "true",
                    "capacity.fs2.nebius/type": "regular",
                    "capacity.fs2.nebius/pool": "reference-data",
                    "storage.fs2.nebius/reference-data": "true",
                }),
                node_toleration=json.dumps({
                    "key": "workload.fs2.nebius/reference-data", "operator": "Equal",
                    "value": "true", "effect": "NoSchedule",
                }),
                active_deadline_seconds=3600, ttl_seconds=86400,
                download_concurrency=4,
            )
            result = render(args)
            job = next(item for item in result["items"] if item["kind"] == "Job")
            pod = job["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertNotIn("nvidia.com/gpu", json.dumps(container["resources"]))
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertRegex(container["image"], r"@sha256:[a-f0-9]{64}$")
            self.assertTrue(job["spec"]["suspend"])
            self.assertEqual("reference-data", job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"])
            self.assertEqual("reference-data", pod["nodeSelector"]["capacity.fs2.nebius/pool"])
            self.assertEqual("NoSchedule", pod["tolerations"][0]["effect"])
            self.assertEqual("fs2-reference-data", pod["serviceAccountName"])
            self.assertIn("hostPath", next(volume for volume in pod["volumes"] if volume["name"] == "reference-data"))
            self.assertNotIn("persistentVolumeClaim", json.dumps(pod["volumes"]))
            self.assertEqual(
                "public-source-staging",
                job["spec"]["template"]["metadata"]["labels"]["reference-data.fs2.nebius.ai/network-mode"],
            )
            self.assertIn("--download-concurrency", container["command"])
            self.assertEqual(
                "4",
                container["command"][container["command"].index("--download-concurrency") + 1],
            )
            config_map = next(item for item in result["items"] if item["kind"] == "ConfigMap")
            projected = {
                item["path"]: config_map["data"][item["key"]]
                for item in next(
                    volume["configMap"]
                    for volume in pod["volumes"]
                    if volume["name"] == "program"
                )["items"]
            }
            program_digest = sha256_bytes(canonical_json(projected))
            self.assertEqual(
                f"public-artifacts-{program_digest[:12]}",
                config_map["metadata"]["name"],
            )
            self.assertEqual(
                config_map["metadata"]["labels"]["fs2.nebius.ai/program-digest"],
                job["metadata"]["annotations"]["fs2.nebius.ai/program-digest"][:63],
            )

    def test_renderer_projects_catalog_provenance_evidence_at_declared_path(self) -> None:
        args = argparse.Namespace(
            catalog=ROOT / "artifact-catalog.json", artifact=["esmfold2-trunk"],
            namespace="fs2-reference-data", local_queue="reference-data",
            service_account="fs2-reference-data",
            shared_filesystem_host_path="/mnt/reference-data",
            cache_subpath="model-artifacts/public/v1", image=DEFAULT_IMAGE,
            project_id="project-test", region="region-test", cluster="cluster-test",
            filesystem_id="computefilesystem-test", filesystem_size_gib=2048,
            cpu_pool_id="computenodegroup-test", cpu_pool_name="reference-data-cpu",
            reference_plane_source_commit="abcdef0123456789",
            source_commit="0123456789abcdef",
            node_selector=json.dumps({
                "workload.fs2.nebius/reference-data": "true",
                "capacity.fs2.nebius/type": "regular",
                "capacity.fs2.nebius/pool": "reference-data",
                "storage.fs2.nebius/reference-data": "true",
            }),
            node_toleration=json.dumps({
                "key": "workload.fs2.nebius/reference-data", "operator": "Equal",
                "value": "true", "effect": "NoSchedule",
            }),
            active_deadline_seconds=3600, ttl_seconds=86400,
            download_concurrency=4,
        )
        rendered = render(args)
        config_map = next(item for item in rendered["items"] if item["kind"] == "ConfigMap")
        job = next(item for item in rendered["items"] if item["kind"] == "Job")
        program = next(
            volume["configMap"]
            for volume in job["spec"]["template"]["spec"]["volumes"]
            if volume["name"] == "program"
        )
        for support_path in (
            "evidence/protenix-v2-mirror-verification-20260902.json",
            "evidence/esm-af3-external-runtime-contract-20260902.json",
            "smoke/protenix-v2-minimal.json",
        ):
            support_item = next(
                item for item in program["items"] if item["path"] == support_path
            )
            self.assertIn(support_item["key"], config_map["data"])

    def test_renderer_rejects_old_shared_system_pool_and_small_filesystem(self) -> None:
        payload = b'{"model":"fixture"}\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            catalog_path, _ = fixture(root, "https://invalid.example/fixture.json", payload)
            base = dict(
                catalog=catalog_path, artifact=["fixture"], namespace="fs2-reference-data",
                local_queue="reference-data", service_account="fs2-reference-data",
                shared_filesystem_host_path="/mnt/reference-data", cache_subpath="model-artifacts/public/v1",
                image=DEFAULT_IMAGE, project_id="project-test", region="region-test",
                cluster="cluster-test", filesystem_id="computefilesystem-test", filesystem_size_gib=2048,
                cpu_pool_id="computenodegroup-test", cpu_pool_name="reference-data-cpu",
                reference_plane_source_commit="abcdef0123456789", source_commit="0123456789abcdef",
                node_toleration=json.dumps({
                    "key": "workload.fs2.nebius/reference-data", "operator": "Equal",
                    "value": "true", "effect": "NoSchedule",
                }),
                active_deadline_seconds=3600, ttl_seconds=86400,
                download_concurrency=4,
            )
            base["node_selector"] = json.dumps({
                "workload.fs2.nebius/reference-data": "true",
                "capacity.fs2.nebius/type": "regular",
                "capacity.fs2.nebius/pool": "system",
                "storage.fs2.nebius/reference-data": "true",
            })
            with self.assertRaisesRegex(ContractError, "shared system pool"):
                render(argparse.Namespace(**base))
            base["node_selector"] = json.dumps({
                "workload.fs2.nebius/reference-data": "true",
                "capacity.fs2.nebius/type": "regular",
                "capacity.fs2.nebius/pool": "reference-data",
                "storage.fs2.nebius/reference-data": "true",
            })
            base["filesystem_size_gib"] = 128
            with self.assertRaisesRegex(ContractError, "at least 2048 GiB"):
                render(argparse.Namespace(**base))
            base["filesystem_size_gib"] = 2048
            base["namespace"] = "fs2-data"
            with self.assertRaisesRegex(ContractError, "isolated fs2-reference-data namespace"):
                render(argparse.Namespace(**base))
            base["namespace"] = "fs2-reference-data"
            base["node_toleration"] = json.dumps({
                "key": "workload.fs2.nebius/reference-data", "operator": "Equal",
                "value": "true", "effect": "PreferNoSchedule",
            })
            with self.assertRaisesRegex(ContractError, "exactly match"):
                render(argparse.Namespace(**base))

    def test_production_catalog_covers_required_families_and_exclusions(self) -> None:
        catalog_path = ROOT / "artifact-catalog.json"
        if not catalog_path.exists():
            self.skipTest("production catalog is generated later in the task")
        catalog = validate_catalog(load_json(catalog_path), catalog_path)
        Draft202012Validator(load_json(ROOT / "artifact-catalog.schema.json")).validate(
            load_json(catalog_path)
        )
        available = {key for key, value in catalog["artifacts"].items() if value["state"] == "available"}
        for required in {
            "complexa-protein", "complexa-ligand", "complexa-ame", "boltzgen-checkpoints",
            "mosaic-components", "rfdiffusion-checkpoints", "proteinmpnn-checkpoints",
            "esmfold2-trunk", "esmfold2-fast-trunk", "esmfold2-ccd", "esmc-6b",
            "openfold3-openbind-0", "openfold3-components-bcif",
            "protenix-v2", "alphafold2-params",
            "alphafold2-params-bindcraft", "colabdesign-mpnn-weights-soluble",
            "colabdesign-mpnn-weights-vanilla",
            "rosettafold3-checkpoint", "boltzgen-inference-molecules",
        }:
            self.assertIn(required, available)
        self.assertNotIn("protenix-v1-substitute", catalog["artifacts"])
        protenix_v2 = catalog["artifacts"]["protenix-v2"]
        self.assertEqual(
            "mirror-verified-not-publisher-byte-compared",
            protenix_v2["provenance"]["state"],
        )
        self.assertEqual(
            "https://github.com/rene-tech/nebius-solutions-library",
            protenix_v2["_manifest"]["source"]["uri"],
        )
        self.assertEqual("third-party-mirror", protenix_v2["provenance"]["acquisition_source"]["relationship"])
        self.assertNotEqual(
            protenix_v2["_manifest"]["source"]["uri"],
            protenix_v2["sources"][0]["url"],
        )
        self.assertFalse(protenix_v2["provenance"]["verification"]["publisher_byte_compared"])
        self.assertEqual(4174, protenix_v2["provenance"]["verification"]["checkpoint_key_count"])
        self.assertEqual(4174, protenix_v2["provenance"]["verification"]["checkpoint_tensor_count"])
        self.assertEqual(
            {"torch.float32": 4174},
            protenix_v2["provenance"]["verification"]["checkpoint_tensor_dtypes"],
        )
        self.assertEqual(464442431, protenix_v2["provenance"]["verification"]["checkpoint_parameter_count"])
        self.assertEqual(
            "checkpoint/protenix-v2.pt",
            protenix_v2["_manifest"]["content"]["files"][0]["path"],
        )
        protenix_layout = catalog["consumer_layouts"]["protenix-v2"]
        self.assertEqual(
            {"checkpoint": "/models/protenix-v2/checkpoint/protenix-v2.pt"},
            protenix_layout["runtime_paths"],
        )
        protenix_handoff = catalog["runtime_handoffs"]["protenix-v2"]
        self.assertEqual("upstream-v2-0-0", protenix_handoff["variant_id"])
        self.assertEqual(
            "mirror-verified-not-publisher-byte-compared",
            protenix_handoff["checkpoint"]["provenance_state"],
        )
        localization = protenix_handoff["localization"]
        self.assertEqual("/models/protenix-v2", localization["mount_path"])
        self.assertEqual(
            "/models/protenix-v2/.fs2-manifest-sha256",
            localization["ready_marker_path"],
        )
        self.assertEqual(
            "8e14bb809d37db806159b7d277577abc692aec81d8899fbc84915d23ebe12eca",
            localization["source_content_digest_sha256"],
        )
        self.assertEqual(
            "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48",
            localization["content_digest_sha256"],
        )
        self.assertEqual(
            "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7",
            localization["manifest_sha256"],
        )
        self.assertEqual(
            [item["path"] for item in localization["files"]],
            localization["required_files"],
        )
        self.assertEqual(
            ["protenix-v1-substitute"],
            protenix_handoff["adapter"]["forbidden_artifact_ids"],
        )
        self.assertEqual(
            "required-not-yet-qualified", protenix_handoff["semantic_smoke"]["state"]
        )
        self.assertEqual(
            "${PROJECT_ID}",
            protenix_handoff["semantic_smoke"]["target"]["project_id"],
        )
        projected_handoffs = artifact_runtime_handoffs(catalog, "protenix-v2")
        self.assertEqual(1, len(projected_handoffs))
        self.assertEqual(localization, projected_handoffs[0]["localization"])

        esmc = catalog["artifacts"]["esmc-6b"]
        self.assertEqual(
            "45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a",
            esmc["_manifest"]["source"]["revision"],
        )
        self.assertEqual("esmc-6b-snapshot", esmc["offline_smoke"])
        self.assertEqual(
            {
                "config.json",
                "model.safetensors.index.json",
                "special_tokens_map.json",
                "tokenizer.json",
                "tokenizer_config.json",
                *(f"model-{index:05d}-of-00006.safetensors" for index in range(1, 7)),
            },
            {item["path"] for item in esmc["_manifest"]["content"]["files"]},
        )
        self.assertEqual(
            ["esmc-6b", "esmfold2-ccd", "esmfold2-trunk"],
            catalog["consumers"]["esmfold2"],
        )
        self.assertEqual(
            ["esmc-6b", "esmfold2-ccd", "esmfold2-fast-trunk"],
            catalog["consumers"]["esmfold2-fast"],
        )
        self.assertEqual(
            ["/models/esmc-6b", "/databases/esmfold2", "/models/esmfold2"],
            [binding["mount_path"] for binding in catalog["consumer_layouts"]["esmfold2"]["bindings"]],
        )
        self.assertEqual(
            ["/models/esmc-6b", "/databases/esmfold2", "/models/esmfold2-fast"],
            [binding["mount_path"] for binding in catalog["consumer_layouts"]["esmfold2-fast"]["bindings"]],
        )
        self.assertEqual(
            ["esmfold2", "esmfold2-fast"],
            catalog["artifacts"]["esmfold2-ccd"]["consumers"],
        )
        for consumer_id in ("esmfold2", "esmfold2-fast"):
            constraint = catalog["runtime_constraints"][consumer_id]
            self.assertEqual(
                "candidate-hopper-sm90-cubin-no-ptx",
                constraint["binary_compatibility"],
            )
            self.assertEqual(["Hopper"], constraint["candidate_accelerator_families"])
            self.assertEqual(["sm90"], constraint["candidate_cuda_architectures"])
            self.assertEqual(
                "pending-exact-image-h100-semantic-test",
                constraint["qualification_state"],
            )
            self.assertEqual("blocked", constraint["blackwell_state"])
            self.assertEqual(
                ["qualified-sdpa-fallback-image", "target-aware-blackwell-image"],
                constraint["blackwell_unblock_requires_one_of"],
            )
            self.assertEqual(
                ["esmc-6b", "esmfold2-ccd"],
                constraint["external_immutable_artifacts"],
            )
            self.assertEqual(["sm80", "sm90"], constraint["binary_evidence"]["native_cubins"])
            self.assertFalse(constraint["binary_evidence"]["ptx_present"])
            self.assertEqual(
                "827ec128e4cdaf80f7d6f95fb367a08980b34918",
                constraint["binary_evidence"]["source_revision"],
            )
        self.assertEqual(
            {"config.json", "model.safetensors"},
            {
                item["path"]
                for item in catalog["artifacts"]["esmfold2-trunk"]["_manifest"]["content"]["files"]
            },
        )
        self.assertEqual(
            "136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c",
            catalog["artifacts"]["esmfold2-trunk"]["_manifest"]["content"]["digest"],
        )
        self.assertEqual(
            {
                "model_dir": "/models/esmfold2",
                "esmc_dir": "/models/esmc-6b",
                "ccd_path": "/databases/esmfold2/ccd.pkl",
            },
            catalog["consumer_layouts"]["esmfold2"]["runtime_paths"],
        )
        openfold_components = catalog["artifacts"]["openfold3-components-bcif"]
        self.assertEqual("snapshot", openfold_components["_manifest"]["kind"])
        self.assertEqual("binarycif-ccd", openfold_components["offline_smoke"])
        self.assertEqual(
            "https://openfold3-data.s3.us-west-2.amazonaws.com/components.bcif",
            openfold_components["_manifest"]["source"]["uri"],
        )
        self.assertEqual(
            "s3://openfold3-data/components.bcif#etag-b251a30629b9c30d077a5b91aeefecb2-4",
            openfold_components["_manifest"]["source"]["revision"],
        )
        self.assertEqual(63393643, openfold_components["sources"][0]["bytes"])
        self.assertEqual(
            "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c",
            openfold_components["sources"][0]["sha256"],
        )
        openfold_evidence = load_json(
            ROOT / "evidence/openfold3-components-verification-20260902.json"
        )
        self.assertEqual("openfold3-components-bcif", openfold_evidence["artifact_id"])
        self.assertEqual(
            openfold_components["sources"][0]["bytes"],
            openfold_evidence["object"]["bytes"],
        )
        self.assertEqual(
            openfold_components["sources"][0]["sha256"],
            openfold_evidence["object"]["sha256"],
        )
        self.assertEqual(
            "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86",
            openfold_evidence["upstream_source"]["revision"],
        )
        self.assertEqual(
            ["_chem_comp", "_chem_comp_atom", "_chem_comp_bond"],
            [
                category["name"]
                for category in openfold_evidence["binarycif_inspection"]["blocks"][0][
                    "categories"
                ]
            ],
        )
        self.assertEqual(
            ["openfold3-components-bcif", "openfold3-openbind-0"],
            catalog["consumers"]["openfold3"],
        )
        openfold_layout = catalog["consumer_layouts"]["openfold3"]
        self.assertEqual(
            [
                {
                    "artifact_id": "openfold3-components-bcif",
                    "mount_root": "/databases",
                    "mount_path": "/databases/openfold3",
                    "read_only": True,
                },
                {
                    "artifact_id": "openfold3-openbind-0",
                    "mount_path": "/models/openfold3",
                    "read_only": True,
                },
            ],
            openfold_layout["bindings"],
        )
        self.assertEqual(
            "f954e2f2e3d0bdba297ac8009f6d590b3e2c28ca2985742c9bbd8167f276f6b5",
            catalog["artifacts"]["openfold3-openbind-0"]["_manifest"]["content"]["digest"],
        )
        boltz_molecules = catalog["artifacts"]["boltzgen-inference-molecules"]
        self.assertEqual("available", boltz_molecules["state"])
        self.assertEqual("MIT", boltz_molecules["_manifest"]["license"]["id"])
        self.assertEqual(391401102, boltz_molecules["sources"][0]["bytes"])
        self.assertEqual(
            "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53",
            boltz_molecules["sources"][0]["sha256"],
        )
        self.assertEqual(
            "/opt/fs2/artifacts/boltzgen-inference-molecules",
            catalog["consumer_layouts"]["boltzgen"]["bindings"][1]["mount_path"],
        )
        self.assertEqual(
            {
                "checkpoint": "/models/openfold3/of3-ob-2025-06-30-174k.pt",
                "components_bcif": "/databases/openfold3/components.bcif",
            },
            openfold_layout["runtime_paths"],
        )
        self.assertEqual(
            [
                {
                    "consumer_id": "openfold3",
                    "mount_root": "/databases",
                    "mount_path": "/databases/openfold3",
                    "read_only": True,
                }
            ],
            artifact_consumer_bindings(catalog, "openfold3-components-bcif"),
        )
        self.assertEqual(
            {
                "bundle_id": "alphafold3-public-databases-v3.0",
                "bundle_revision": "v3.0-paper-snapshot-2022-09-28",
                "source_plane": "reference-data",
                "source_revision": "231efc9bb9c13b45cc59e43f7107869084ee9624",
                "mount_path": "/databases",
                "read_only": True,
                "runtime_argument": "--db_dir=/databases",
            },
            catalog["reference_layouts"]["alphafold3"],
        )
        reference_bundle = load_json(ROOT.parent / "reference-data/source-catalog.json")[
            "bundles"
        ]["alphafold3-public-databases-v3.0"]
        self.assertEqual(
            reference_bundle["revision"],
            catalog["reference_layouts"]["alphafold3"]["bundle_revision"],
        )
        self.assertEqual(
            reference_bundle["upstream"]["revision"],
            catalog["reference_layouts"]["alphafold3"]["source_revision"],
        )
        self.assertEqual("excluded-private", catalog["artifacts"]["alphafold3-private"]["state"])
        self.assertEqual("excluded-private", catalog["artifacts"]["pyrosetta-private"]["state"])
        self.assertNotIn("alphafold3", catalog["consumer_layouts"])
        self.assertNotIn("bindcraft-pyrosetta", catalog["consumer_layouts"])
        self.assertEqual(
            {
                "artifact_id": "alphafold3-private",
                "source_plane": "academic-assets",
                "cache_scope": "tenant-private",
                "general_shared_cache_allowed": False,
                "embed_in_image": False,
                "source_url": "https://storage.googleapis.com/alphafold3/af3.bin.zst",
                "source_revision": "gs://alphafold3/af3.bin.zst#1780568696389861",
                "generation": "1780568696389861",
                "last_modified": "2026-06-04T10:24:56Z",
                "filename": "af3.bin.zst",
                "bytes": 1020545840,
                "sha256": "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff",
                "mount_path": "/models",
                "file_path": "/models/af3.bin.zst",
                "read_only": True,
                "runtime_argument": "--model_dir=/models",
                "evidence": "evidence/esm-af3-external-runtime-contract-20260902.json",
            },
            catalog["private_layouts"]["alphafold3"],
        )
        with tempfile.TemporaryDirectory() as temp:
            projection = readiness(catalog_path, Path(temp))
        Draft202012Validator(load_json(ROOT / "readiness.schema.json")).validate(projection)
        self.assertEqual(
            catalog["runtime_constraints"]["esmfold2"],
            projection["consumers"]["esmfold2"]["runtime_constraint"],
        )
        self.assertEqual(
            catalog["runtime_handoffs"]["protenix-v2"],
            projection["consumers"]["protenix-v2"]["runtime_handoff"],
        )
        self.assertEqual(
            catalog["private_layouts"]["alphafold3"],
            projection["consumers"]["alphafold3"]["private_layout"],
        )
        self.assertEqual(
            catalog["reference_layouts"]["alphafold3"],
            projection["consumers"]["alphafold3"]["reference_layout"],
        )
        self.assertFalse(projection["consumers"]["alphafold3"]["ready"])
        readiness_validator = Draft202012Validator(load_json(ROOT / "readiness.schema.json"))
        projection["consumers"]["esmfold2"]["runtime_constraint"]["binary_evidence"][
            "ptx_present"
        ] = True
        self.assertTrue(list(readiness_validator.iter_errors(projection)))
        for entry in catalog["artifacts"].values():
            if entry["state"] == "available":
                manifest = load_artifact_manifest(catalog_path.parent / entry["manifest"])
                self.assertEqual(entry["_manifest_digest"], manifest.digest)

    def test_all_proteina_complexa_variants_use_huggingface_host(self) -> None:
        qualification = load_json(
            ROOT.parent / "models/cancer-immunotherapy/model-source-qualification.json"
        )
        complexa = qualification["models"]["proteina-complexa"]
        self.assertEqual("Proteina-Complexa", complexa["identity"]["canonical_name"])
        expected_locators = {
            "proteina-complexa-protein-target-160m-v1":
                "nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1",
            "proteina-complexa-ligand-target-160m-v1":
                "nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1",
            "proteina-complexa-ame-160m-v1":
                "nvidia/NV-Proteina-Complexa-AME-160M-v1",
        }
        self.assertEqual(expected_locators, {
            weight["artifact_id"]: weight["locator"]
            for weight in complexa["weights"]
        })
        self.assertEqual(
            {artifact_id: "huggingface" for artifact_id in expected_locators},
            {
                weight["artifact_id"]: weight["host"]
                for weight in complexa["weights"]
            },
        )

    def test_academic_poc_qualification_is_current_without_exporting_licensed_bytes(self) -> None:
        qualification = load_json(
            ROOT.parent / "models/cancer-immunotherapy/model-source-qualification.json"
        )
        bindcraft = qualification["models"]["bindcraft"]
        alphafold3 = qualification["models"]["alphafold3"]
        self.assertFalse(bindcraft["access_gates"][0]["blocking"])
        self.assertIn(
            "Operational academic-PoC authorization is recorded",
            bindcraft["access_gates"][0]["action"],
        )
        self.assertIn(
            "tenant-private academic-assets plane",
            bindcraft["access_gates"][0]["action"],
        )
        self.assertFalse(alphafold3["access_gates"][0]["blocking"])
        self.assertIn(
            "sha256:0cde199e8473a2d069c896c4f8d67a58b31e00bfb87c3660aed154693699e03e",
            alphafold3["access_gates"][0]["action"],
        )
        self.assertTrue(
            all("cannot run until" not in blocker.lower() for blocker in bindcraft["blockers"])
        )
        self.assertIn(
            "excluded from the public cache",
            alphafold3["access_gates"][0]["action"],
        )

    def test_protenix_manifest_and_catalog_acquisition_provenance_agree(self) -> None:
        catalog_path = ROOT / "artifact-catalog.json"
        catalog = validate_catalog(load_json(catalog_path), catalog_path)
        artifact = catalog["artifacts"]["protenix-v2"]
        manifest = artifact["_manifest"]
        provenance = artifact["provenance"]
        sources = {source["path"]: source for source in artifact["sources"]}
        manifest_files = {item["path"]: item for item in manifest["content"]["files"]}

        self.assertEqual(manifest_files, {
            path: {key: source[key] for key in ("path", "bytes", "sha256")}
            for path, source in sources.items()
        })
        self.assertEqual(
            {
                "uri": "https://github.com/rene-tech/nebius-solutions-library",
                "revision": (
                    "code-2475421477ab414b571149ad4a875c390ff8a35d_"
                    "checkpoint-653edab28103133512575365130916e3fd23ecc3_"
                    "common-2026-01-29"
                ),
            },
            manifest["source"],
        )
        canonical = provenance["canonical_source"]
        self.assertFalse(canonical["publisher_bytes_reachable"])
        self.assertFalse(canonical["publisher_digest_available"])
        self.assertNotEqual(canonical["uri"], manifest["source"]["uri"])

        checkpoint = sources["checkpoint/protenix-v2.pt"]
        mirror = provenance["acquisition_source"]
        self.assertEqual(mirror["url"], checkpoint["url"])
        self.assertEqual(mirror["bytes"], checkpoint["bytes"])
        self.assertEqual(mirror["lfs_oid_sha256"], checkpoint["sha256"])
        self.assertEqual(
            "mirror-verified-not-publisher-byte-compared",
            provenance["state"],
        )
        self.assertTrue(all(
            source["url"].startswith("https://protenix.tos-cn-beijing.volces.com/common/")
            for path, source in sources.items()
            if path.startswith("common/")
        ))

        qualification = load_json(
            ROOT.parent / "models/cancer-immunotherapy/model-source-qualification.json"
        )
        qualified = next(
            weight
            for weight in qualification["models"]["protenix-v2"]["weights"]
            if weight["artifact_id"] == "protenix-v2"
        )
        self.assertEqual("huggingface", qualified["host"])
        self.assertEqual("TMF001/protenix-v2-weights", qualified["locator"])
        self.assertEqual(mirror["repository_revision"], qualified["revision"])

    def test_production_layouts_fail_closed_on_runtime_or_reference_path_drift(self) -> None:
        catalog_path = ROOT / "artifact-catalog.json"
        document = load_json(catalog_path)
        document["consumer_layouts"]["openfold3"]["runtime_paths"][
            "components_bcif"
        ] = "/databases/openfold3/missing.bcif"
        with self.assertRaisesRegex(ContractError, "does not resolve to a declared artifact file"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["reference_layouts"]["alphafold3"]["runtime_argument"] = (
            "--db_dir=/reference-data"
        )
        with self.assertRaisesRegex(ContractError, "must be --db_dir=/databases"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["runtime_constraints"]["esmfold2-fast"]["binary_evidence"][
            "ptx_present"
        ] = True
        with self.assertRaisesRegex(ContractError, "exact binary-candidate contract"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["runtime_constraints"]["esmfold2"]["blackwell_state"] = "qualified"
        with self.assertRaisesRegex(ContractError, "exact binary-candidate contract"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["consumer_layouts"]["esmfold2"]["bindings"] = [
            binding
            for binding in document["consumer_layouts"]["esmfold2"]["bindings"]
            if binding["artifact_id"] != "esmc-6b"
        ]
        with self.assertRaisesRegex(ContractError, "do not exactly cover"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["private_layouts"]["alphafold3"]["general_shared_cache_allowed"] = True
        with self.assertRaisesRegex(ContractError, "exact private identity and /models path"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["private_layouts"]["alphafold3"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "exact private identity and /models path"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["private_layouts"]["alphafold3"]["mount_path"] = "/models/alphafold3"
        with self.assertRaisesRegex(ContractError, "exact private identity and /models path"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["artifacts"]["alphafold2-params"]["localization"][
            "archive_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(ContractError, "archive identity conflicts"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["artifacts"]["alphafold2-params"]["localization"][
            "mount_paths"
        ] = ["/models/alphafold2-params"]
        with self.assertRaisesRegex(ContractError, "paths conflict with consumer bindings"):
            validate_catalog(document, catalog_path)

    def test_runtime_integration_projection_is_exact_and_self_consistent(self) -> None:
        catalog_path = ROOT / "artifact-catalog.json"
        catalog = validate_catalog(load_json(catalog_path), catalog_path)
        integration = load_json(ROOT / "runtime-integration.json")
        self.assertEqual(
            "fs2-serve.nebius.ai/public-artifact-runtime-integration/v1",
            integration["schema"],
        )
        consumers = integration["consumers"]
        for consumer_id, trunk in (
            ("esmfold2", "esmfold2-trunk"),
            ("esmfold2-fast", "esmfold2-fast-trunk"),
        ):
            projected = consumers[consumer_id]
            self.assertEqual(
                catalog["consumer_layouts"][consumer_id]["runtime_paths"],
                projected["runtime_paths"],
            )
            self.assertEqual(
                {trunk, "esmc-6b", "esmfold2-ccd"},
                set(projected["artifacts"]),
            )
            for artifact_id, artifact in projected["artifacts"].items():
                self.assertEqual(
                    catalog["artifacts"][artifact_id]["_manifest"]["content"]["digest"],
                    artifact["content_digest_sha256"],
                )
            self.assertEqual(
                "binary-compatible-hopper-candidate-sm90-no-ptx",
                projected["accelerator_compatibility"],
            )
            self.assertEqual(
                "pending-exact-image-h100-semantic-test",
                projected["qualification_state"],
            )

        protenix = consumers["protenix-v2"]
        localization = catalog["runtime_handoffs"]["protenix-v2"]["localization"]
        self.assertEqual(localization["content_digest_sha256"], protenix["localized_content_digest_sha256"])
        self.assertEqual(localization["manifest_sha256"], protenix["localization_manifest_sha256"])
        self.assertEqual(localization["required_files"], protenix["required_files"])
        self.assertEqual(
            {"protenix-v1-substitute", "protenix-v2-inference-data-2026-01-29"},
            set(protenix["forbidden_legacy_artifacts"]),
        )
        localization_smoke = load_json(
            ROOT / "evidence/protenix-v2-localization-smoke-20260902.json"
        )
        self.assertEqual("pass", localization_smoke["result"])
        self.assertEqual(
            protenix["localized_content_digest_sha256"],
            localization_smoke["localized_content_digest_sha256"],
        )
        self.assertFalse(localization_smoke["cluster_mutation"])

        openfold = consumers["openfold3"]
        self.assertEqual(
            {"openfold3-openbind-0", "openfold3-components-bcif"},
            set(openfold["artifacts"]),
        )
        self.assertEqual(
            catalog["consumer_layouts"]["openfold3"]["runtime_paths"],
            openfold["runtime_paths"],
        )

        boltz = consumers["boltzgen"]
        self.assertEqual("boltzgen-inference-molecules", boltz["artifact_id"])
        self.assertEqual(
            catalog["artifacts"][boltz["artifact_id"]]["_manifest"]["content"]["digest"],
            boltz["content_digest_sha256"],
        )
        self.assertEqual(45227, boltz["archive"]["entry_count"])
        self.assertEqual(1820698819, boltz["archive"]["expanded_bytes"])
        self.assertEqual(
            "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc",
            boltz["archive"]["expanded_inventory_sha256"],
        )
        evidence = load_json(ROOT / "evidence/boltzgen-molecules-verification-20260902.json")
        self.assertEqual(
            boltz["archive"]["expanded_inventory_sha256"],
            evidence["zip_central_directory"]["expanded_inventory_sha256"],
        )
        self.assertEqual(
            catalog["private_layouts"]["alphafold3"],
            consumers["alphafold3"]["private_parameters"],
        )
        self.assertEqual(
            catalog["reference_layouts"]["alphafold3"],
            consumers["alphafold3"]["public_databases"],
        )
        self.assertFalse(consumers["alphafold3"]["general_cache_parameters_allowed"])

        proteina = consumers["proteina-complexa"]
        self.assertEqual("alphafold2-params", proteina["artifact_id"])
        self.assertEqual(
            "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4",
            proteina["localization"]["tree_inventory_sha256"],
        )
        self.assertEqual(
            "AF2_DIR=/opt/fs2/artifacts/alphafold2-params",
            proteina["localization"]["runtime_binding"],
        )

        bindcraft = consumers["bindcraft"]
        bindcraft_artifacts = bindcraft["artifacts"]
        self.assertEqual(
            {
                "alphafold2-params-bindcraft",
                "colabdesign-mpnn-weights-soluble",
                "colabdesign-mpnn-weights-vanilla",
            },
            set(bindcraft_artifacts),
        )
        self.assertEqual(
            "9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f",
            bindcraft_artifacts["alphafold2-params-bindcraft"]["tree_inventory_sha256"],
        )
        self.assertNotEqual(
            proteina["localization"]["tree_inventory_sha256"],
            bindcraft_artifacts["alphafold2-params-bindcraft"]["tree_inventory_sha256"],
        )
        self.assertEqual(
            proteina["source_content_digest_sha256"],
            bindcraft_artifacts["alphafold2-params-bindcraft"]["source_content_digest_sha256"],
        )
        vanilla = bindcraft_artifacts["colabdesign-mpnn-weights-vanilla"]
        soluble = bindcraft_artifacts["colabdesign-mpnn-weights-soluble"]
        self.assertEqual(vanilla["source_content_digest_sha256"], soluble["source_content_digest_sha256"])
        self.assertEqual(vanilla["archive_sha256"], soluble["archive_sha256"])
        self.assertNotEqual(vanilla["member_prefix"], soluble["member_prefix"])
        self.assertNotEqual(vanilla["mount_path"], soluble["mount_path"])
        self.assertNotEqual(vanilla["tree_inventory_sha256"], soluble["tree_inventory_sha256"])
        self.assertEqual(
            {
                "alphafold2-params-bindcraft": "/models/alphafold2",
                "colabdesign-mpnn-weights-soluble": (
                    "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble"
                ),
                "colabdesign-mpnn-weights-vanilla": (
                    "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights"
                ),
            },
            {
                binding["artifact_id"]: binding["mount_path"]
                for binding in catalog["consumer_layouts"]["bindcraft"]["bindings"]
            },
        )
        private_artifact = bindcraft["private_artifact"]
        academic_contract = load_json(
            ROOT.parent / "academic-assets/contracts/academic-assets.json"
        )["assets"]["pyrosetta-bindcraft"]
        academic_binding = academic_contract["delivery"]["runtime_binding"]
        academic_tree = load_json(
            ROOT.parent / "academic-assets/evidence/live-acceptance-state.json"
        )["semantic_evidence"]["installed_tree"]
        self.assertEqual(
            "bindcraft-pyrosetta-installed-tree", private_artifact["artifact_id"]
        )
        self.assertEqual("bindcraft-pyrosetta", private_artifact["source_artifact_id"])
        self.assertEqual(academic_binding["artifact_id"], private_artifact["artifact_id"])
        self.assertEqual(
            academic_binding["source_artifact_id"],
            private_artifact["source_artifact_id"],
        )
        self.assertEqual(
            academic_binding["consumer_path"], private_artifact["mount_path"]
        )
        self.assertEqual(
            academic_contract["delivery"]["runtime_consumption"]["pythonpath"],
            private_artifact["pythonpath"],
        )
        self.assertEqual(
            academic_tree["tree_manifest_sha256"],
            private_artifact["tree_manifest_sha256"],
        )
        self.assertEqual(
            academic_tree["tree_total_bytes"], private_artifact["tree_total_bytes"]
        )
        self.assertEqual(
            academic_binding["source_artifact"]["sha256"],
            private_artifact["source_archive_sha256"],
        )
        self.assertNotEqual(
            private_artifact["source_archive_sha256"],
            private_artifact["tree_manifest_sha256"],
        )
        self.assertEqual("tenant-private", private_artifact["cache_scope"])
        self.assertFalse(private_artifact["general_shared_cache_allowed"])
        self.assertFalse(private_artifact["embed_in_image"])

    def test_protenix_v2_mirror_provenance_fails_closed_on_overclaim(self) -> None:
        catalog_path = ROOT / "artifact-catalog.json"
        document = load_json(catalog_path)
        document["artifacts"]["protenix-v2"]["provenance"]["verification"][
            "publisher_byte_compared"
        ] = True
        with self.assertRaisesRegex(ContractError, "must not claim publisher-byte comparison"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["runtime_handoffs"]["protenix-v2"]["adapter"]["forbidden_artifact_ids"] = []
        with self.assertRaisesRegex(ContractError, "v1 substitute"):
            validate_catalog(document, catalog_path)

        document = load_json(catalog_path)
        document["runtime_handoffs"]["protenix-v2"]["semantic_smoke"]["target"][
            "cluster_context"
        ] = "some-other-cluster"
        with self.assertRaisesRegex(ContractError, "provider-neutral project input"):
            validate_catalog(document, catalog_path)


if __name__ == "__main__":
    unittest.main()
