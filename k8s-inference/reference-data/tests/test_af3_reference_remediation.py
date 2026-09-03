"""Contract tests for AlphaFold 3 reference-data resumability and routing.

The cases here cover the three properties the AlphaFold 3 path depends on: an
interrupted multi-hundred-gigabyte staging run resumes instead of restarting,
the terminal handoff stays bounded and content-addressed, and raw input is
placed on a CPU pool that can actually admit it while inference is placed
independently by accelerator flavour.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker


REFERENCE_DATA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REFERENCE_DATA))

import reference_data  # noqa: E402
import render_job  # noqa: E402


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stage_worker(catalog_path: str, root: str, queue: object) -> None:  # pragma: no cover - subprocess
    try:
        _manifest, digest = reference_data.stage_bundle(Path(catalog_path), "fixture", Path(root))
        queue.put(("ok", digest))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent process
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


class Af3ReferenceDataRemediationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    # ---------------------------------------------------------------- helpers

    def _object(
        self,
        object_id: str,
        source: Path,
        *,
        target: str,
        transform: str = "none",
        algorithm: str = "sha256",
    ) -> dict[str, object]:
        payload = source.read_bytes()
        digest = _sha256(payload) if algorithm == "sha256" else hashlib.md5(payload).hexdigest()  # noqa: S324
        return {
            "id": object_id,
            "source": {"url": source.resolve().as_uri()},
            "target": target,
            "transform": transform,
            "source_bytes": len(payload),
            "source_integrity": {
                "algorithm": algorithm,
                "digest": digest,
                "cryptographic": algorithm == "sha256",
            },
            "license_component": "fixture",
        }

    def _catalog(self, objects: list[dict[str, object]]) -> dict[str, object]:
        compressed = sum(int(item["source_bytes"]) for item in objects)
        return {
            "schema": reference_data.CATALOG_SCHEMA,
            "generated_at": "2026-09-03T00:00:00Z",
            "bundles": {
                "fixture": {
                    "id": "fixture",
                    "revision": "fixture-2026-09-03",
                    "description": "Multi-object offline staging fixture.",
                    "upstream": {
                        "project": "fixture/reference-data",
                        "revision": "0" * 40,
                        "source_url": "https://example.invalid/source",
                        "source_sha256": "1" * 64,
                    },
                    "access": {
                        "state": "public",
                        "redistribution": "review-required",
                        "staging_policy": "automatic-public",
                        "terms": [{
                            "component": "fixture",
                            "license": "test-only",
                            "url": "https://example.invalid/terms",
                            "verification": "upstream-terms-review-required",
                        }],
                    },
                    "sizing": {
                        "compressed_bytes": compressed,
                        "expanded_bytes": compressed,
                        "expanded_bytes_kind": "exact",
                    },
                    "update_policy": {
                        "cadence": "immutable test fixture",
                        "mutable_aliases_allowed": False,
                        "promotion": "new-revision-after-offline-validation",
                    },
                    "objects": objects,
                }
            },
        }

    def _write_catalog(self, catalog: dict[str, object]) -> Path:
        path = self.work / f"catalog-{len(list(self.work.glob('catalog-*.json')))}.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        return path

    def _multi_object_catalog(self) -> tuple[Path, list[Path]]:
        """Three independent objects: a plain file, a gzip file and a tar."""
        plain = self.work / "plain.txt"
        plain.write_text("AF3-PLAIN\n", encoding="utf-8")

        import gzip

        raw = b"AF3-GENETIC-DATABASE\n" * 64
        gzipped = self.work / "genetic.fa.gz"
        with gzip.open(gzipped, "wb") as handle:
            handle.write(raw)

        archive_root = self.work / "tarsource"
        (archive_root / "mmcif").mkdir(parents=True)
        for index in range(3):
            (archive_root / "mmcif" / f"{index}.cif").write_text(f"CIF-{index}\n", encoding="utf-8")
        tarred = self.work / "structures.tar"
        with tarfile.open(tarred, "w") as handle:
            handle.add(archive_root / "mmcif", arcname="mmcif")

        catalog = self._catalog([
            self._object("plain-object", plain, target="plain.txt"),
            self._object("genetic-object", gzipped, target="genetic.fa", transform="gzip", algorithm="md5"),
            self._object("structures-object", tarred, target=".", transform="tar.gz"),
        ])
        return self._write_catalog(catalog), [plain, gzipped, tarred]

    # ------------------------------------------------- resumability / adoption

    def test_already_localized_objects_are_never_downloaded_again(self) -> None:
        catalog_path, sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]

        blob, digest, disposition = reference_data.localize_source(
            root, bundle, bundle["objects"][0], downloads
        )
        self.assertEqual("downloaded", disposition)
        self.assertTrue(blob.is_file())

        # Removing the upstream source proves the second pass never reads it.
        sources[0].unlink()
        _blob, second_digest, second_disposition = reference_data.localize_source(
            root, bundle, bundle["objects"][0], downloads
        )
        self.assertEqual(digest, second_digest)
        self.assertEqual("reused", second_disposition)

    def test_a_verified_blob_without_a_record_is_adopted_not_refetched(self) -> None:
        catalog_path, sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]
        item = bundle["objects"][1]

        reference_data.localize_source(root, bundle, item, downloads)
        index = root / "sources" / "fixture" / bundle["revision"] / f"{item['id']}.json"
        self.assertTrue(index.is_file())
        index.unlink()
        sources[1].unlink()

        blob, _digest, disposition = reference_data.localize_source(root, bundle, item, downloads)
        self.assertEqual("adopted", disposition)
        self.assertTrue(blob.is_file())
        self.assertTrue(index.is_file())

    def test_a_recorded_object_whose_catalog_identity_changed_fails_closed(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]
        item = bundle["objects"][0]
        reference_data.localize_source(root, bundle, item, downloads)

        mutated = copy.deepcopy(item)
        mutated["source_bytes"] = int(mutated["source_bytes"]) + 1
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.read_source_index(root, bundle, mutated)
        self.assertIn("differs from the catalog", str(raised.exception))

    def test_an_interrupted_partial_download_resumes_from_its_bytes(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]
        item = bundle["objects"][0]

        downloads.mkdir(parents=True, exist_ok=True)
        source = Path(item["source"]["url"].removeprefix("file://"))
        payload = source.read_bytes()
        partial = downloads / f"{item['id']}.part"
        partial.write_bytes(payload[:4])

        blob, digest, disposition = reference_data.localize_source(root, bundle, item, downloads)
        self.assertEqual("downloaded", disposition)
        self.assertEqual(_sha256(payload), digest)
        self.assertEqual(payload, blob.read_bytes())

    def test_objects_sharing_a_byte_count_each_localize_to_their_own_blob(self) -> None:
        first = self.work / "first.bin"
        second = self.work / "second.bin"
        first.write_bytes(b"A" * 4096)
        second.write_bytes(b"B" * 4096)
        catalog_path = self._write_catalog(self._catalog([
            self._object("first-object", first, target="database/first.bin"),
            self._object("second-object", second, target="database/second.bin"),
        ]))
        root = self.work / "store"

        manifest, _digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        digests = {item["id"]: item["source_sha256"] for item in manifest["source_objects"]}

        self.assertEqual(2, len(set(digests.values())))
        self.assertEqual(_sha256(b"A" * 4096), digests["first-object"])
        self.assertEqual(_sha256(b"B" * 4096), digests["second-object"])
        published = root / manifest["storage"]["dataset_sub_path"]
        self.assertEqual(b"A" * 4096, (published / "database" / "first.bin").read_bytes())
        self.assertEqual(b"B" * 4096, (published / "database" / "second.bin").read_bytes())

    def test_adoption_ignores_a_same_sized_blob_another_object_already_claimed(self) -> None:
        first = self.work / "first.bin"
        second = self.work / "second.bin"
        first.write_bytes(b"A" * 4096)
        second.write_bytes(b"B" * 4096)
        catalog_path = self._write_catalog(self._catalog([
            self._object("first-object", first, target="database/first.bin"),
            self._object("second-object", second, target="database/second.bin"),
        ]))
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]

        reference_data.localize_source(root, bundle, bundle["objects"][0], downloads)
        _blob, digest, disposition = reference_data.localize_source(
            root, bundle, bundle["objects"][1], downloads
        )
        self.assertEqual("downloaded", disposition)
        self.assertEqual(_sha256(b"B" * 4096), digest)

    def test_the_plan_reports_adoptable_blobs_left_by_an_earlier_run(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]
        item = bundle["objects"][0]

        reference_data.localize_source(root, bundle, item, downloads)
        plan = reference_data.localization_plan(catalog_path, "fixture", root)
        localized = {entry["id"]: entry for entry in plan["objects"]}[item["id"]]
        self.assertEqual("localized", localized["state"])
        self.assertIs(True, localized["digest_verified"])
        self.assertEqual(1, plan["totals"]["localized_objects"])

        # An earlier run that predates the index leaves the blob but no record.
        (root / "sources" / "fixture" / bundle["revision"] / f"{item['id']}.json").unlink()
        plan = reference_data.localization_plan(catalog_path, "fixture", root)
        adoptable = {entry["id"]: entry for entry in plan["objects"]}[item["id"]]

        self.assertEqual("adoptable", adoptable["state"])
        self.assertIs(False, adoptable["digest_verified"])
        self.assertIs(False, adoptable["indexed"])
        self.assertIsNotNone(adoptable["adoptable_blob_sha256"])
        self.assertEqual(1, plan["totals"]["adoptable_objects"])
        self.assertEqual(int(item["source_bytes"]), plan["totals"]["adoptable_bytes"])
        self.assertEqual(
            plan["totals"]["source_bytes"] - int(item["source_bytes"]),
            plan["totals"]["remaining_bytes"],
        )

    def test_the_plan_reports_a_partial_download_without_mutating_anything(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]
        downloads.mkdir(parents=True)
        (downloads / "plain-object.part").write_bytes(b"AF3")

        before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        plan = reference_data.localization_plan(catalog_path, "fixture", root)
        after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

        self.assertEqual(before, after)
        partial = {entry["id"]: entry for entry in plan["objects"]}["plain-object"]
        self.assertEqual("partial", partial["state"])
        self.assertEqual(3, partial["partial_bytes"])
        self.assertIs(False, plan["published"]["ready"])

    # -------------------------------------------------- parallel object claims

    def test_two_workers_share_objects_and_publish_one_identical_revision(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        workers = [
            context.Process(target=_stage_worker, args=(str(catalog_path), str(root), queue))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=180)
        results = [queue.get(timeout=30) for _ in workers]

        self.assertTrue(all(status == "ok" for status, _payload in results), results)
        digests = {payload for _status, payload in results}
        self.assertEqual(1, len(digests), results)
        status = reference_data.load_json(root / "status" / "fixture.json")
        self.assertIs(True, status["ready"])
        self.assertEqual(digests.pop(), status["manifest_sha256"])

    def test_a_peer_owned_object_is_skipped_rather_than_waited_on(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]
        held = bundle["objects"][0]["id"]

        import fcntl

        lock_path = root / "locks" / "fixture" / bundle["revision"] / f"{held}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # The peer's object is expanded here so the loop can still finish.
            reference_data.expand_object(root, bundle, bundle["objects"][0], downloads)
            receipts, dispositions = reference_data.localize_and_expand_bundle(
                root, bundle, downloads
            )

        self.assertEqual({item["id"] for item in bundle["objects"]}, set(receipts))
        self.assertEqual("expanded", dispositions[held])

    def test_per_object_expansion_and_a_whole_tree_walk_agree_exactly(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        catalog = reference_data.validate_catalog(reference_data.load_json(catalog_path))
        bundle = catalog["bundles"]["fixture"]
        downloads = root / "downloads" / "fixture" / bundle["revision"]
        receipts, _dispositions = reference_data.localize_and_expand_bundle(
            root, bundle, downloads
        )
        staging = self.work / "assembled"
        staging.mkdir()
        merged_files, merged_tree, merged_bytes = reference_data.assemble_bundle_tree(
            root, bundle, receipts, staging
        )
        walked_files, walked_tree, walked_bytes = reference_data.tree_inventory(staging)

        self.assertEqual(walked_files, merged_files)
        self.assertEqual(walked_tree, merged_tree)
        self.assertEqual(walked_bytes, merged_bytes)

    # ------------------------------------------------- bounded terminal handoff

    def test_a_large_tree_publishes_a_bounded_handoff_with_an_external_inventory(self) -> None:
        count = reference_data.MAX_INLINE_INVENTORY_FILES + 1
        archive_root = self.work / "many"
        archive_root.mkdir()
        for index in range(count):
            (archive_root / f"file-{index:05d}.txt").write_text(f"{index}\n", encoding="utf-8")
        tarred = self.work / "many.tar"
        with tarfile.open(tarred, "w") as handle:
            for child in sorted(archive_root.iterdir()):
                handle.add(child, arcname=child.name)
        catalog_path = self._write_catalog(self._catalog([
            self._object("many-object", tarred, target=".", transform="tar.gz"),
        ]))
        root = self.work / "store"

        manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        content = manifest["content"]

        self.assertEqual(count, content["file_count"])
        self.assertNotIn("files", content)
        inventory_path = root / "inventories" / "sha256" / f"{content['inventory_sha256']}.json"
        self.assertTrue(inventory_path.is_file())
        inventory = reference_data.load_json(inventory_path)
        self.assertEqual(count, len(inventory["files"]))

        receipt = reference_data.validate_terminal_receipt(
            reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")
        )
        self.assertIs(False, receipt["content"]["inline_inventory"])
        self.assertEqual(digest, receipt["content"]["manifest_sha256"])
        self.assertNotEqual(receipt["content"]["manifest_sha256"], receipt["content"]["tree_sha256"])
        self.assertEqual(
            f"datasets/fixture/fixture-2026-09-03/sha256/{content['tree_sha256']}",
            receipt["storage"]["dataset_sub_path"],
        )
        Draft202012Validator(
            reference_data.load_json(REFERENCE_DATA / "handoff-receipt.schema.json"),
            format_checker=FormatChecker(),
        ).validate(receipt)
        # A bounded handoff is verifiable without enumerating the database.
        reference_data.verify_manifest(
            root / "manifests" / "sha256" / f"{digest}.json", verify_tree=False
        )

    def test_a_small_tree_keeps_its_inline_inventory(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, _digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        self.assertIn("files", manifest["content"])
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")
        self.assertIs(True, receipt["content"]["inline_inventory"])

    def test_the_handoff_exposes_the_canonical_host_root_and_dataset_path(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, _digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")

        self.assertEqual("/mnt/fs2-reference-data/data", receipt["storage"]["host_root"])
        self.assertEqual("/mnt/fs2-reference-data/data", manifest["storage"]["host_root"])
        self.assertEqual(
            manifest["storage"]["dataset_sub_path"], receipt["storage"]["dataset_sub_path"]
        )
        self.assertEqual("/reference-data", receipt["storage"]["mount_path"])
        self.assertIs(True, receipt["storage"]["read_only"])
        self.assertEqual("cpu", receipt["placement"]["resource_class"])
        self.assertNotIn("accelerator", receipt["placement"])
        self.assertNotIn("workload.fs2.nebius/gpu", receipt["placement"]["node_selector"])
        self.assertEqual(
            "workload.fs2.nebius/reference-data", receipt["placement"]["tolerations"][0]["key"]
        )

    def test_invented_handoff_field_names_are_rejected_with_the_actual_field(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")

        drafted = copy.deepcopy(receipt)
        drafted["content"]["published_manifest_sha256"] = drafted["content"]["manifest_sha256"]
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_terminal_receipt(drafted)
        self.assertIn("content.manifest_sha256", str(raised.exception))

        drafted = copy.deepcopy(receipt)
        drafted["storage"]["source_sub_path"] = drafted["storage"]["dataset_sub_path"]
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_terminal_receipt(drafted)
        self.assertIn("storage.dataset_sub_path", str(raised.exception))

    def test_a_dataset_path_that_does_not_bind_the_tree_digest_is_rejected(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")
        receipt["storage"]["dataset_sub_path"] = f"datasets/fixture/fixture-2026-09-03/sha256/{'0' * 64}"
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_terminal_receipt(receipt)
        self.assertIn("exact aggregate tree digest", str(raised.exception))

    # ---------------------------------------- producer fixture and transform

    def test_the_mounted_tree_is_a_full_sha_directory_with_its_marker(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")
        tree = manifest["content"]["tree_sha256"]

        published = root / "datasets" / "fixture" / "fixture-2026-09-03" / "sha256" / tree
        self.assertTrue(published.is_dir())
        self.assertEqual(tree, published.name)
        self.assertEqual(64, len(published.name))
        marker = published / receipt["content"]["inventory_marker"]
        self.assertTrue(marker.is_file())
        self.assertEqual(digest, marker.read_text(encoding="utf-8").strip())
        self.assertEqual(
            published, root / receipt["storage"]["dataset_sub_path"]
        )
        self.assertEqual(
            published.resolve().as_uri(), manifest["storage"]["shared_filesystem_uri"]
        )

    def test_the_receipt_is_the_producer_fixture_with_exactly_its_published_keys(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")

        self.assertEqual(
            {"schema", "bundle_id", "revision", "created_at", "storage", "content", "placement"},
            set(receipt),
        )
        # The publisher never invents a manifest location for a consumer.
        self.assertNotIn("manifest_uri", receipt["storage"])
        self.assertNotIn("manifest_uri", receipt["content"])

    def test_the_consumer_transform_produces_exactly_the_four_key_reference_block(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")
        manifest_uri = f"s3://private-reference-data/reference-data/manifests/sha256/{digest}.json"

        block = reference_data.derive_preprocess_reference_data(receipt, manifest_uri=manifest_uri)
        self.assertEqual(
            {"bundle_id": "fixture", "revision": "fixture-2026-09-03",
             "manifest_uri": manifest_uri, "manifest_sha256": digest},
            block,
        )

        database_root = reference_data.derive_database_root(receipt)
        self.assertEqual(
            f"/reference-data/datasets/fixture/fixture-2026-09-03/sha256/"
            f"{manifest['content']['tree_sha256']}",
            database_root,
        )
        # The derived pair satisfies the live request contract unchanged.
        document = json.loads(self._af3_request().read_text(encoding="utf-8"))
        document["reference_data"] = block
        document["backend"]["database_root"] = database_root
        validated = reference_data.validate_preprocess_request(document)
        self.assertEqual(block, validated["reference_data"])

    def test_a_manifest_uri_that_names_another_digest_is_rejected(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.derive_preprocess_reference_data(
                receipt,
                manifest_uri=f"s3://private-reference-data/manifests/sha256/{'0' * 64}.json",
            )
        self.assertIn("must name the published manifest digest", str(raised.exception))

    def test_the_published_example_is_built_by_the_publishers_own_builder(self) -> None:
        sys.path.insert(0, str(REFERENCE_DATA / "scripts"))
        import generate_handoff_example  # noqa: PLC0415

        checked_in = reference_data.load_json(
            REFERENCE_DATA / "examples" / "af3-terminal-handoff.example.json"
        )
        self.assertEqual(generate_handoff_example.build(), checked_in)
        reference_data.validate_terminal_receipt(checked_in)
        Draft202012Validator(
            reference_data.load_json(REFERENCE_DATA / "handoff-receipt.schema.json"),
            format_checker=FormatChecker(),
        ).validate(checked_in)

    def test_a_consumer_can_mount_and_bind_from_the_example_alone(self) -> None:
        receipt = reference_data.load_json(
            REFERENCE_DATA / "examples" / "af3-terminal-handoff.example.json"
        )
        storage = receipt["storage"]
        content = receipt["content"]

        self.assertEqual("/mnt/fs2-reference-data/data", storage["host_root"])
        self.assertEqual("/reference-data", storage["mount_path"])
        self.assertIs(True, storage["read_only"])
        self.assertEqual(
            f"{storage['mount_path']}/{storage['dataset_sub_path']}",
            reference_data.derive_database_root(receipt),
        )
        self.assertTrue(storage["dataset_sub_path"].endswith(f"/sha256/{content['tree_sha256']}"))
        self.assertNotEqual(content["manifest_sha256"], content["tree_sha256"])
        self.assertEqual(".fs2-manifest-sha256", content["inventory_marker"])
        self.assertIs(False, content["inline_inventory"])

        manifest_uri = (
            "s3://private-reference-data/reference-data/manifests/sha256/"
            f"{content['manifest_sha256']}.json"
        )
        block = reference_data.derive_preprocess_reference_data(receipt, manifest_uri=manifest_uri)
        self.assertEqual(
            {"bundle_id", "revision", "manifest_uri", "manifest_sha256"}, set(block)
        )
        self.assertEqual(content["manifest_sha256"], block["manifest_sha256"])

    def test_the_producer_builder_and_a_real_publication_agree_on_shape(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        published = reference_data.load_json(
            root / "receipts" / "fixture" / "fixture-2026-09-03.json"
        )
        rebuilt = reference_data.build_terminal_receipt(
            bundle_id="fixture",
            revision="fixture-2026-09-03",
            tree_sha256=manifest["content"]["tree_sha256"],
            manifest_sha256=digest,
            inventory_sha256=manifest["content"]["inventory_sha256"],
            file_count=manifest["content"]["file_count"],
            expanded_bytes=manifest["content"]["expanded_bytes"],
            created_at=published["created_at"],
        )
        self.assertEqual(published, rebuilt)

    def test_the_receipt_never_reintroduces_the_manifest_filesystem_uri(self) -> None:
        receipt = reference_data.load_json(
            REFERENCE_DATA / "examples" / "af3-terminal-handoff.example.json"
        )
        self.assertNotIn("shared_filesystem_uri", receipt["storage"])
        drafted = copy.deepcopy(receipt)
        drafted["storage"]["shared_filesystem_uri"] = "file:///reference-data/datasets"
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_terminal_receipt(drafted)
        self.assertIn("unknown fields", str(raised.exception))

    # -------------------------------------------- validating a live publication

    def _validator(self):
        sys.path.insert(0, str(REFERENCE_DATA / "scripts"))
        import validate_published_revision  # noqa: PLC0415

        return validate_published_revision

    def test_validation_accepts_a_bounded_publication_and_writes_nothing(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)

        before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
        report = self._validator().validate(root, "fixture", deep=True, host_root="/mnt/fs2-reference-data/data")
        after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

        self.assertEqual(before, after)
        self.assertIs(True, report["valid"], report["findings"])
        self.assertIs(True, report["published"])
        self.assertIs(True, report["bounded_contract"])
        self.assertEqual("published", report["receipt_source"])
        self.assertEqual(digest, report["manifest"]["sha256"])
        self.assertEqual(manifest["content"]["tree_sha256"], report["manifest"]["tree_sha256"])
        self.assertEqual(
            f"/mnt/fs2-reference-data/data/{report['mount']['dataset_sub_path']}",
            report["mount"]["host_path"],
        )
        self.assertEqual("0o555", report["mount"]["directory_mode"])

    def test_validation_derives_the_receipt_for_a_legacy_publication(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        _manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        self._downgrade_to_legacy_publication(root, digest)

        report = self._validator().validate(root, "fixture", deep=False, host_root="/mnt/fs2-reference-data/data")

        self.assertIs(False, report["bounded_contract"])
        self.assertEqual("derived-not-written", report["receipt_source"])
        receipt = reference_data.validate_terminal_receipt(report["receipt"])
        self.assertEqual(report["manifest"]["sha256"], receipt["content"]["manifest_sha256"])
        self.assertTrue(
            receipt["storage"]["dataset_sub_path"].endswith(
                f"/sha256/{report['manifest']['tree_sha256']}"
            )
        )
        self.assertFalse((root / "receipts" / "fixture" / "fixture-2026-09-03.json").exists())

    def test_validation_reports_a_tampered_tree_rather_than_passing(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, _digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        published = Path(manifest["storage"]["shared_filesystem_uri"].removeprefix("file://"))

        mode = published.stat().st_mode & 0o7777
        published.chmod(0o755)
        extra = published / "unexpected.txt"
        extra.write_text("EXTRA\n", encoding="utf-8")
        published.chmod(mode)

        report = self._validator().validate(root, "fixture", deep=False, host_root="/mnt/fs2-reference-data/data")
        self.assertIs(False, report["valid"])
        failed = {item["check"] for item in report["findings"] if not item["ok"]}
        self.assertIn("tree file count matches the manifest", failed)

    def test_validation_of_an_unpublished_root_says_so(self) -> None:
        root = self.work / "empty"
        root.mkdir()
        report = self._validator().validate(root, "fixture", deep=False, host_root="/mnt/fs2-reference-data/data")
        self.assertIs(False, report["published"])
        self.assertIs(False, report["valid"])

    # ------------------------------------------- upgrading a legacy publication

    def _downgrade_to_legacy_publication(self, root: Path, manifest_sha256: str) -> dict[str, Any]:
        """Rewrite a publication into the pre-bounded shape a older stager wrote."""
        manifest_path = root / "manifests" / "sha256" / f"{manifest_sha256}.json"
        manifest = reference_data.load_json(manifest_path)
        inventory = reference_data.load_json(
            root / "inventories" / "sha256" / f"{manifest['content']['inventory_sha256']}.json"
        )
        legacy = {
            **manifest,
            "content": {
                "tree_sha256": manifest["content"]["tree_sha256"],
                "expanded_bytes": manifest["content"]["expanded_bytes"],
                "file_count": manifest["content"]["file_count"],
                "files": inventory["files"],
            },
            "storage": {
                "shared_filesystem_uri": manifest["storage"]["shared_filesystem_uri"],
                "object_manifest_prefix": manifest["storage"]["object_manifest_prefix"],
            },
        }
        legacy_sha256 = reference_data.sha256_bytes(reference_data.canonical_json(legacy))
        legacy_path = root / "manifests" / "sha256" / f"{legacy_sha256}.json"
        reference_data.atomic_json(legacy_path, legacy)
        manifest_path.chmod(0o644)
        manifest_path.unlink()

        published = Path(legacy["storage"]["shared_filesystem_uri"].removeprefix("file://"))
        mode = published.stat().st_mode & 0o7777
        published.chmod(0o755)
        marker = published / ".fs2-manifest-sha256"
        marker.unlink()
        reference_data.atomic_text(marker, legacy_sha256 + "\n")
        published.chmod(mode)

        (root / "receipts" / "fixture" / "fixture-2026-09-03.json").unlink()
        reference_data.atomic_json(root / "status" / "fixture.json", {
            "schema": "fs2-serve.nebius.ai/reference-data-status/v1",
            "bundle_id": "fixture",
            "revision": "fixture-2026-09-03",
            "ready": True,
            "manifest_sha256": legacy_sha256,
            "tree_sha256": legacy["content"]["tree_sha256"],
            "expanded_bytes": legacy["content"]["expanded_bytes"],
            "file_count": legacy["content"]["file_count"],
            "updated_at": "2026-09-03T00:00:00Z",
        })
        return legacy

    def test_a_legacy_publication_is_named_exactly_not_reported_as_stale(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        _manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        self._downgrade_to_legacy_publication(root, digest)

        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.stage_bundle(catalog_path, "fixture", root)
        message = str(raised.exception)
        self.assertIn("published before the bounded handoff contract", message)
        self.assertIn("upgrade-publication", message)

    def test_upgrading_republishes_the_existing_tree_without_restaging_it(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        original, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        legacy = self._downgrade_to_legacy_publication(root, digest)
        published = Path(legacy["storage"]["shared_filesystem_uri"].removeprefix("file://"))
        before = sorted(
            (path.relative_to(published).as_posix(), path.stat().st_size)
            for path in published.rglob("*") if path.is_file()
        )

        manifest, upgraded = reference_data.upgrade_publication(catalog_path, "fixture", root)

        after = sorted(
            (path.relative_to(published).as_posix(), path.stat().st_size)
            for path in published.rglob("*") if path.is_file()
        )
        self.assertEqual(before, after)
        self.assertEqual(original["content"]["tree_sha256"], manifest["content"]["tree_sha256"])
        self.assertEqual(original["created_at"], manifest["created_at"])
        self.assertEqual(original["source_objects"], manifest["source_objects"])
        self.assertIn("inventory_sha256", manifest["content"])
        self.assertEqual("/mnt/fs2-reference-data/data", manifest["storage"]["host_root"])

        marker = published / ".fs2-manifest-sha256"
        self.assertEqual(upgraded, marker.read_text(encoding="utf-8").strip())
        receipt = reference_data.validate_terminal_receipt(
            reference_data.load_json(root / "receipts" / "fixture" / "fixture-2026-09-03.json")
        )
        self.assertEqual(upgraded, receipt["content"]["manifest_sha256"])
        status = reference_data.load_json(root / "status" / "fixture.json")
        self.assertEqual(upgraded, status["manifest_sha256"])

        # The upgraded revision is now consumable by the normal path.
        again, again_digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        self.assertEqual(upgraded, again_digest)
        self.assertEqual(manifest, again)

    def test_upgrading_refuses_a_tree_whose_content_changed(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        _manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        legacy = self._downgrade_to_legacy_publication(root, digest)
        published = Path(legacy["storage"]["shared_filesystem_uri"].removeprefix("file://"))

        mode = published.stat().st_mode & 0o7777
        published.chmod(0o755)
        victim = next(path for path in sorted(published.rglob("*")) if path.is_file()
                      and not path.name.startswith(".fs2-"))
        victim.chmod(0o644)
        victim.write_text("TAMPERED\n", encoding="utf-8")
        published.chmod(mode)

        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.upgrade_publication(catalog_path, "fixture", root)
        self.assertIn("refusing to upgrade", str(raised.exception))

    def test_upgrading_without_a_published_revision_fails_closed(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        root.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.upgrade_publication(catalog_path, "fixture", root)
        self.assertIn("no published revision to upgrade", str(raised.exception))

    # --------------------------------------------------------- stale handoffs

    def test_a_changed_host_root_fails_closed_instead_of_serving_a_stale_handoff(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        reference_data.stage_bundle(catalog_path, "fixture", root)
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.stage_bundle(
                catalog_path, "fixture", root, host_root="/mnt/other-reference-data/data"
            )
        self.assertIn("stale handoff", str(raised.exception))

    def test_a_changed_tfvars_placement_regenerates_the_published_handoff(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt_path = root / "receipts" / "fixture" / "fixture-2026-09-03.json"
        first = reference_data.load_json(receipt_path)

        override = {
            "schema": reference_data.PLACEMENT_SCHEMA,
            "generated_at": "2026-09-03T01:00:00Z",
            "pools": {
                "reference-cpu": {
                    "resource_class": "cpu",
                    "node_selector": {
                        "capacity.fs2.nebius/pool": "reference-data",
                        "workload.fs2.nebius/reference-data": "true",
                    },
                    "tolerations": [{
                        "key": "workload.fs2.nebius/reference-data",
                        "operator": "Equal",
                        "value": "true",
                        "effect": "NoSchedule",
                    }],
                    "schedulable_capacity": {
                        "cpu_millicores": 31000,
                        "memory_mib": 122880,
                        "ephemeral_storage_mib": 114688,
                    },
                    "queue": {
                        "local_queue": "reference-data",
                        "cluster_queue": "reference-data-cpu",
                        "nominal_cpu": "30",
                        "nominal_memory": "120Gi",
                        "nominal_accelerator": None,
                    },
                }
            },
            "stages": {},
        }
        override_path = self.work / "placement-override.json"
        override_path.write_text(json.dumps(override), encoding="utf-8")

        reference_data.stage_bundle(catalog_path, "fixture", root, placement_path=override_path)
        second = reference_data.load_json(receipt_path)

        self.assertEqual(first["created_at"], second["created_at"])
        self.assertNotEqual(first["placement"]["node_selector"], second["placement"]["node_selector"])
        self.assertEqual(
            {"capacity.fs2.nebius/pool": "reference-data", "workload.fs2.nebius/reference-data": "true"},
            second["placement"]["node_selector"],
        )
        reference_data.validate_terminal_receipt(second)

    # ------------------------------------------------------- object-store path

    def test_the_configured_object_store_publishes_blobs_inventory_and_manifest(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        fake_bin = self.work / "bin"
        fake_bin.mkdir()
        calls = self.work / "aws-calls.log"
        (fake_bin / "aws").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(calls) + "\nexit 0\n",
            encoding="utf-8",
        )
        (fake_bin / "aws").chmod(0o755)
        original = os.environ["PATH"]
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", original))

        manifest, digest = reference_data.stage_bundle(
            catalog_path,
            "fixture",
            root,
            object_store_prefix="s3://fixture-bucket/reference-data/",
        )
        recorded = calls.read_text(encoding="utf-8").splitlines()

        # three blobs, one inventory, one manifest
        self.assertEqual(5, len(recorded), recorded)
        for item in manifest["source_objects"]:
            self.assertTrue(
                any(f"blobs/sha256/{item['source_sha256']}" in line for line in recorded),
                item["id"],
            )
        self.assertTrue(any(
            f"inventories/sha256/{manifest['content']['inventory_sha256']}.json" in line
            for line in recorded
        ))
        self.assertTrue(any(f"manifests/sha256/{digest}.json" in line for line in recorded))
        self.assertEqual(
            "s3://fixture-bucket/reference-data/manifests/sha256",
            manifest["storage"]["object_manifest_prefix"],
        )

    def test_object_store_publication_without_the_cli_fails_closed(self) -> None:
        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        empty_bin = self.work / "empty-bin"
        empty_bin.mkdir()
        original = os.environ["PATH"]
        os.environ["PATH"] = str(empty_bin)
        self.addCleanup(lambda: os.environ.__setitem__("PATH", original))
        if shutil.which("aws") is not None:  # pragma: no cover - defensive
            self.skipTest("aws remains resolvable")
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.stage_bundle(
                catalog_path, "fixture", root, object_store_prefix="s3://fixture-bucket/reference-data"
            )
        self.assertIn("aws CLI is required", str(raised.exception))

    # ------------------------------------------------ placement and admission

    def test_the_checked_in_placement_contract_matches_its_schema(self) -> None:
        schema = reference_data.load_json(REFERENCE_DATA / "placement-contract.schema.json")
        Draft202012Validator.check_schema(schema)
        contract = reference_data.load_json(REFERENCE_DATA / "placement-contract.json")
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(contract)
        validated = reference_data.validate_placement_contract(contract)
        self.assertEqual({"staging", "raw-input", "inference"}, set(validated["stages"]))

    def test_a_request_larger_than_its_pool_is_rejected_before_a_job_exists(self) -> None:
        contract = reference_data.load_placement_contract(self._fitting_placement())
        for field, value, expected in (
            ("cpu", "64", "CPU request exceeds"),
            ("memory", "256Gi", "memory request exceeds"),
            ("ephemeral_storage", "4096Gi", "ephemeral storage request exceeds"),
        ):
            execution = dict(contract["stages"]["raw-input"]["defaults"])
            execution[field] = value
            with self.assertRaises(reference_data.ContractError) as raised:
                reference_data.check_execution_fits(execution, contract, "raw-input")
            self.assertIn(expected, str(raised.exception))
            self.assertIn("terraform.tfvars", str(raised.exception))

    def test_the_kueue_nominal_quota_bounds_a_request_that_fits_the_node(self) -> None:
        contract = copy.deepcopy(reference_data.load_placement_contract(self._fitting_placement()))
        contract["pools"]["reference-cpu"]["schedulable_capacity"]["cpu_millicores"] = 64000
        execution = dict(contract["stages"]["raw-input"]["defaults"])
        execution["cpu"] = "40"
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.check_execution_fits(execution, contract, "raw-input")
        self.assertIn("Kueue nominal quota", str(raised.exception))

    def test_placement_never_accepts_a_node_identity_or_hardware_label_key(self) -> None:
        for selector, expected in (
            ({"kubernetes.io/hostname": "worker-node-example"}, "must not pin a node identity"),
            ({"nebius.com/h100-node": "true"}, "not a stable portable label"),
        ):
            contract = copy.deepcopy(reference_data.load_placement_contract())
            contract["pools"]["reference-cpu"]["node_selector"] = selector
            with self.assertRaises(reference_data.ContractError) as raised:
                reference_data.validate_placement_contract(contract)
            self.assertIn(expected, str(raised.exception))

    def test_a_cpu_pool_may_not_select_or_reserve_an_accelerator(self) -> None:
        contract = copy.deepcopy(reference_data.load_placement_contract())
        contract["pools"]["reference-cpu"]["node_selector"]["workload.fs2.nebius/gpu"] = "true"
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_placement_contract(contract)
        self.assertIn("must not select an accelerator node", str(raised.exception))

        contract = copy.deepcopy(reference_data.load_placement_contract())
        contract["pools"]["reference-cpu"]["accelerator"] = {"resource_name": "nvidia.com/gpu", "count": 1}
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_placement_contract(contract)
        self.assertIn("must not reserve accelerators", str(raised.exception))

    def test_the_accelerator_pool_may_not_route_work_to_the_reference_pool(self) -> None:
        contract = copy.deepcopy(reference_data.load_placement_contract())
        contract["pools"]["accelerator"]["node_selector"]["capacity.fs2.nebius/pool"] = "reference-data"
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_placement_contract(contract)
        self.assertIn("must not route accelerator work", str(raised.exception))

    # ----------------------------------------------------------- raw-input route

    def _fitting_placement(self) -> Path:
        """A reference pool sized for the AlphaFold 3 data pipeline."""
        contract = copy.deepcopy(reference_data.load_placement_contract())
        pool = contract["pools"]["reference-cpu"]
        pool["schedulable_capacity"] = {
            "cpu_millicores": 31000,
            "memory_mib": 122880,
            "ephemeral_storage_mib": 460800,
        }
        pool["queue"]["nominal_cpu"] = "30"
        pool["queue"]["nominal_memory"] = "120Gi"
        path = self.work / "fitting-placement.json"
        path.write_text(json.dumps({
            "schema": reference_data.PLACEMENT_SCHEMA,
            "generated_at": contract["generated_at"],
            "pools": {"reference-cpu": pool},
            "stages": {},
        }), encoding="utf-8")
        return path

    def _route_args(self, request_path: Path, placement: Path | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            request=request_path,
            allow_public_msa=False,
            namespace="fs2-reference-data",
            queue="reference-data",
            tools_config_map="fs2-reference-data-tools-123456789abc",
            shared_host_path="/mnt/fs2-reference-data/data",
            credentials_secret=None,
            object_storage_endpoint=None,
            placement=placement,
        )

    def _af3_request(self, **execution: object) -> Path:
        tree = "c" * 64
        document = {
            "schema": reference_data.REQUEST_SCHEMA,
            "request_id": "af3-raw-input-001",
            "tenant_id": "tenant-cancer-immunotherapy",
            "workload_id": "workload-af3-001",
            "input": {
                "uri": f"s3://private-inputs/inputs/sha256/{'a' * 64}.json",
                "sha256": "a" * 64,
                "bytes": 512,
                "media_type": "application/json",
            },
            "reference_data": {
                "bundle_id": "alphafold3-public-databases-v3.0",
                "revision": "v3.0-paper-snapshot-2022-09-28",
                "manifest_uri": f"s3://private-reference-data/reference-data/manifests/sha256/{'b' * 64}.json",
                "manifest_sha256": "b" * 64,
            },
            "backend": {
                "kind": "alphafold3-data",
                "database_root": (
                    "/reference-data/datasets/alphafold3-public-databases-v3.0/"
                    f"v3.0-paper-snapshot-2022-09-28/sha256/{tree}"
                ),
                "output_format": "alphafold3-json",
                "threads": 16,
            },
            "privacy": {
                "network_mode": "private-only",
                "public_msa_opt_in": False,
                "log_sequence_content": False,
            },
            "output": {
                "prefix_uri": "s3://private-results/preprocessing/tenant-cancer-immunotherapy/af3-001",
                "retention_days": 30,
            },
            "execution": {
                "image": f"registry.example.invalid/alphafold3@sha256:{'d' * 64}",
                "cpu": "16",
                "memory": "64Gi",
                "ephemeral_storage": "32Gi",
                "active_deadline_seconds": 21600,
                "backoff_limit": 2,
                **execution,
            },
        }
        path = self.work / "af3-request.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_raw_input_routes_to_the_cpu_pool_and_inference_to_the_accelerator(self) -> None:
        route = render_job.render_route(
            self._route_args(self._af3_request(), self._fitting_placement())
        )
        raw_input, inference = route["stages"]

        self.assertEqual("raw-input", raw_input["id"])
        self.assertEqual("cpu", raw_input["resource_class"])
        self.assertNotIn("accelerator", raw_input)
        self.assertEqual("reference-data", raw_input["node_selector"]["capacity.fs2.nebius/pool"])
        self.assertEqual(
            "workload.fs2.nebius/reference-data", raw_input["tolerations"][0]["key"]
        )
        self.assertEqual("reference-data-cpu", raw_input["queue"]["cluster_queue"])

        self.assertEqual("inference", inference["id"])
        self.assertEqual("gpu", inference["resource_class"])
        self.assertEqual(["raw-input"], inference["needs"])
        self.assertEqual("nvidia.com/gpu", inference["accelerator"]["resource_name"])
        self.assertEqual("true", inference["node_selector"]["workload.fs2.nebius/gpu"])
        self.assertNotIn("capacity.fs2.nebius/pool", inference["node_selector"])
        self.assertEqual("dedicated", inference["tolerations"][0]["key"])
        self.assertEqual("prohibited", inference["consumes"]["reference_database_download"])
        self.assertIn("storage.dataset_sub_path", inference["consumes"]["binds"])
        self.assertEqual(
            "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1",
            inference["consumes"]["handoff_schema"],
        )

    def test_the_rendered_raw_input_job_carries_no_accelerator_request(self) -> None:
        route = render_job.render_route(
            self._route_args(self._af3_request(), self._fitting_placement())
        )
        job = route["resources"]["items"][1]
        rendered = json.dumps(job)
        self.assertNotIn("nvidia.com/gpu", rendered)
        self.assertNotIn("workload.fs2.nebius/gpu", rendered)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual("true", pod["nodeSelector"]["workload.fs2.nebius/reference-data"])
        self.assertEqual("NoSchedule", pod["tolerations"][0]["effect"])
        self.assertTrue(job["spec"]["suspend"])

    def test_the_data_pipeline_lane_is_not_runnable_on_the_staging_pool(self) -> None:
        # The pool that stages the bulk databases cannot run the data pipeline.
        with self.assertRaises(reference_data.ContractError) as raised:
            render_job.render_route(self._route_args(self._af3_request()))
        message = str(raised.exception)
        self.assertIn("CPU request exceeds", message)
        self.assertIn("terraform.tfvars", message)

    def test_a_request_below_the_declared_model_requirement_is_refused(self) -> None:
        for override, expected in (
            ({"cpu": "6"}, "at least 16 CPU"),
            ({"memory": "24Gi"}, "at least 64Gi memory"),
            ({"ephemeral_storage": "8Gi"}, "at least 32Gi ephemeral storage"),
        ):
            path = self._af3_request(**override)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["backend"]["threads"] = min(
                document["backend"]["threads"], int(document["execution"]["cpu"])
            )
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(reference_data.ContractError) as raised:
                reference_data.validate_preprocess_request(reference_data.load_json(path))
            self.assertIn(expected, str(raised.exception))

    def test_the_declared_capacity_report_separates_the_stager_from_the_pipeline(self) -> None:
        contract = reference_data.load_placement_contract()
        required = reference_data.model_preprocessing_capacity("alphafold3")
        self.assertEqual({"cpu": "16", "memory": "64Gi"},
                         {k: required[k] for k in ("cpu", "memory")})

        staging = reference_data.stage_admissibility(contract, "staging")
        raw_input = reference_data.stage_admissibility(contract, "raw-input")

        self.assertIs(True, staging["runnable"])
        self.assertEqual({"cpu": "6", "memory": "24Gi", "ephemeral_storage": "2Gi"},
                         staging["requested"])
        self.assertIs(False, raw_input["runnable"])
        self.assertEqual({"cpu": "16", "memory": "64Gi", "ephemeral_storage": "32Gi"},
                         raw_input["requested"])
        self.assertTrue(any("CPU request exceeds" in reason for reason in raw_input["reasons"]))

        fitting = reference_data.load_placement_contract(self._fitting_placement())
        self.assertIs(True, reference_data.stage_admissibility(fitting, "raw-input")["runnable"])
        self.assertIs(True, reference_data.stage_admissibility(fitting, "staging")["runnable"])

    def test_a_thread_budget_above_the_cpu_request_is_rejected(self) -> None:
        path = self._af3_request()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["backend"]["threads"] = 32
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_preprocess_request(reference_data.load_json(path))
        self.assertIn("oversubscribe", str(raised.exception))

    def test_the_acceptance_request_is_derived_only_from_a_real_receipt(self) -> None:
        sys.path.insert(0, str(REFERENCE_DATA / "scripts"))
        import render_af3_raw_input_acceptance as renderer  # noqa: PLC0415

        catalog_path, _sources = self._multi_object_catalog()
        root = self.work / "store"
        manifest, digest = reference_data.stage_bundle(catalog_path, "fixture", root)
        receipt = reference_data.load_json(
            root / "receipts" / "fixture" / "fixture-2026-09-03.json"
        )
        document = renderer.build_request(
            receipt,
            request_id="af3-acceptance",
            tenant_id="tenant-example",
            workload_id="workload-example",
            input_uri=f"file:///reference-data/acceptance/inputs/sha256/{'a' * 64}.json",
            input_sha256="a" * 64,
            input_bytes=512,
            manifest_uri=f"file:///reference-data/manifests/sha256/{digest}.json",
            image=f"registry.example.invalid/alphafold3@sha256:{'d' * 64}",
            interpreter="/alphafold3_venv/bin/python3",
            script="/app/alphafold/run_alphafold.py",
            output_prefix_uri="file:///reference-data/acceptance/results",
            placement_path=self._fitting_placement(),
        )

        self.assertEqual(digest, document["reference_data"]["manifest_sha256"])
        self.assertEqual(
            f"/reference-data/{receipt['storage']['dataset_sub_path']}",
            document["backend"]["database_root"],
        )
        self.assertTrue(
            document["backend"]["database_root"].endswith(
                f"/sha256/{manifest['content']['tree_sha256']}"
            )
        )
        self.assertEqual("alphafold3-data", document["backend"]["kind"])
        self.assertNotIn("nvidia.com/gpu", json.dumps(document))
        reference_data.check_execution_fits(
            document["execution"],
            reference_data.load_placement_contract(self._fitting_placement()),
            "raw-input",
        )

    def test_the_alphafold3_entrypoint_is_declared_not_assumed(self) -> None:
        path = self._af3_request()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["backend"]["entrypoint"] = {
            "interpreter": "/alphafold3_venv/bin/python3",
            "script": "/app/alphafold/run_alphafold.py",
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        validated = reference_data.validate_preprocess_request(reference_data.load_json(path))
        self.assertEqual(
            "/alphafold3_venv/bin/python3", validated["backend"]["entrypoint"]["interpreter"]
        )
        Draft202012Validator(
            reference_data.load_json(REFERENCE_DATA / "preprocess-request.schema.json"),
            format_checker=FormatChecker(),
        ).validate(validated)

    def test_an_entrypoint_outside_an_absolute_path_is_rejected(self) -> None:
        for entrypoint, expected in (
            ({"interpreter": "python3", "script": "/app/run.py"}, "absolute path"),
            ({"interpreter": "/bin/python3", "script": "/app/../etc/run.py"}, "absolute path"),
            ({"interpreter": "/bin/python3; rm -rf /", "script": "/app/run.py"}, "unsupported characters"),
        ):
            path = self._af3_request()
            document = json.loads(path.read_text(encoding="utf-8"))
            document["backend"]["entrypoint"] = entrypoint
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(reference_data.ContractError) as raised:
                reference_data.validate_preprocess_request(reference_data.load_json(path))
            self.assertIn(expected, str(raised.exception))

    def test_only_the_alphafold3_backend_takes_an_explicit_entrypoint(self) -> None:
        request_path = REFERENCE_DATA / "examples" / "private-msa-request.json"
        document = reference_data.load_json(request_path)
        document["backend"]["entrypoint"] = {
            "interpreter": "/bin/python3",
            "script": "/app/run.py",
        }
        with self.assertRaises(reference_data.ContractError) as raised:
            reference_data.validate_preprocess_request(document)
        self.assertIn("only the alphafold3-data backend", str(raised.exception))

    def test_the_alphafold3_data_pipeline_declares_its_thread_budget(self) -> None:
        source = (REFERENCE_DATA / "reference_data.py").read_text(encoding="utf-8")
        self.assertIn('f"--jackhmmer_n_cpu={backend[\'threads\']}"', source)
        self.assertIn('f"--nhmmer_n_cpu={backend[\'threads\']}"', source)
        self.assertIn('"--norun_inference"', source)


if __name__ == "__main__":
    unittest.main()
