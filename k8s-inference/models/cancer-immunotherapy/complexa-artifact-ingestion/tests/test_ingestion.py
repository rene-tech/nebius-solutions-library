"""Contract, transport, and promotion tests for the Complexa artifact ingestion.

The transport tests run against a real loopback HTTP server that speaks ranges,
truncates streams, and lies about content, because the failures worth catching
here are all transport failures and a mocked ``urlopen`` cannot exhibit them.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fetch_artifacts as fa  # noqa: E402
import promote_generations as pg  # noqa: E402
import reclaim_staging as rs  # noqa: E402
import render_ingestion_jobs as rij  # noqa: E402

REPO = ROOT.parents[2]
ADAPTERS = REPO / "components/control-plane/src/fs2_serve/scientific_batch/adapters"


def localization_adapters() -> Path | None:
    """Where the reviewed successor's verifier lives, if it is available here.

    The promoter is deliberately not vendored: it belongs to the localization
    remediation task. Until that lands on this branch the promotion tests skip
    rather than test a copy, because a copy is exactly the thing the content
    addressing exists to prevent.
    """

    override = os.environ.get("FS2_LOCALIZATION_ADAPTERS")
    candidates = [Path(override)] if override else []
    candidates.append(ADAPTERS)
    for candidate in candidates:
        if (candidate / "localization.py").is_file() and (candidate / "primitives.py").is_file():
            return candidate
    return None


def build_package(parent: Path, adapters: Path) -> Path:
    package = parent / "fs2_localization"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('"""Localization verifier delivered to the cluster."""\n')
    for name in ("localization.py", "primitives.py"):
        shutil.copy2(adapters / name, package / name)
    return parent


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""
    truncate_after: int | None = None
    accept_ranges = True
    requests: list[str] = []

    def log_message(self, *args: object) -> None:  # keep the test output readable
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        header = self.headers.get("Range", "")
        type(self).requests.append(header)
        total = len(self.payload)
        start = 0
        if header and self.accept_ranges:
            start = int(header.removeprefix("bytes=").rstrip("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        else:
            self.send_response(200)
        body = self.payload[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        limit = self.truncate_after
        self.wfile.write(body if limit is None else body[:limit])


class ServerCase(unittest.TestCase):
    def serve(self, payload: bytes, *, truncate_after: int | None = None, accept_ranges: bool = True) -> str:
        _Handler.payload = payload
        _Handler.truncate_after = truncate_after
        _Handler.accept_ranges = accept_ranges
        _Handler.requests = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def quiet(_message: str) -> None:
        return


def contract_document(files: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    total = sum(int(entry["bytes"]) for entry in files)
    artifact: dict[str, object] = {
        "artifact_id": "demo",
        "transform": "fetch-files",
        "model_id": "demo-model",
        "source": {
            "uri": "https://example.invalid/pub/",
            "revision": "rev-1",
            "resolver": "direct-https",
            "license_id": "BSD-3-Clause",
            "entitlement_state": "not-required",
        },
        "files": files,
        "tree": {
            "mount_paths": ["/opt/fs2/artifacts/demo"],
            "entry_count": len(files),
            "total_bytes": total,
            "entry_path_pattern": r"^[a-z0-9_.]+$",
            "inventory_algorithm": "fs2-tree-inventory/v2",
        },
        "consumers": [],
    }
    artifact.update(overrides)
    return {"schema": fa.SCHEMA, "artifacts": [artifact]}


def blob(size: int, seed: int = 0) -> bytes:
    return bytes((index * 7 + seed) % 251 for index in range(size))


def entry_for(name: str, payload: bytes) -> dict[str, object]:
    return {"path": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


class ContractTests(ServerCase):
    def write(self, document: dict[str, object]) -> Path:
        path = self.tmp / "contract.json"
        path.write_text(json.dumps(document))
        return path

    def test_accepts_the_shipped_contract(self) -> None:
        document = fa.load_contract(ROOT / "ingestion-contract.json")
        ids = [artifact["artifact_id"] for artifact in document["artifacts"]]
        self.assertEqual(ids, ["complexa-protein", "complexa-ligand", "complexa-ame", "rosettafold3-checkpoint"])
        total = sum(artifact["tree"]["total_bytes"] for artifact in document["artifacts"])
        self.assertEqual(total, 21856218452)

    def test_shipped_contract_pins_the_ticketed_revisions(self) -> None:
        document = fa.load_contract(ROOT / "ingestion-contract.json")
        pinned = {artifact["artifact_id"]: artifact["source"]["revision"] for artifact in document["artifacts"]}
        self.assertEqual(pinned["complexa-protein"], "ffed199e32612b98ffa04f4640d34d37b137fca5")
        self.assertEqual(pinned["complexa-ligand"], "bc90c8b2c701ceb52d5faef72600b6b5be880244")
        self.assertEqual(pinned["complexa-ame"], "9743d749a8754080a32fda857d95579dfa4dabae")

    def test_no_artifact_declares_an_inventory_digest_up_front(self) -> None:
        # A generation must be named by measured bytes, never by a contract edit.
        document = json.loads((ROOT / "ingestion-contract.json").read_text())
        for artifact in document["artifacts"]:
            self.assertNotIn("inventory_sha256", artifact["tree"], artifact["artifact_id"])

    def test_rejects_a_path_that_escapes_the_artifact_directory(self) -> None:
        document = contract_document([{"path": "../escape", "bytes": 4, "sha256": "aa" * 32}])
        document["artifacts"][0]["tree"]["entry_path_pattern"] = "^.*$"
        with self.assertRaisesRegex(fa.IngestionError, "illegal file path"):
            fa.load_contract(self.write(document))

    def test_rejects_a_part_suffixed_name(self) -> None:
        # A contracted ".part" name would collide with the resume file.
        document = contract_document([{"path": "weights.part", "bytes": 4, "sha256": "aa" * 32}])
        with self.assertRaisesRegex(fa.IngestionError, "illegal file path"):
            fa.load_contract(self.write(document))

    def test_rejects_a_name_outside_the_declared_pattern(self) -> None:
        document = contract_document([{"path": "WEIGHTS", "bytes": 4, "sha256": "aa" * 32}])
        with self.assertRaisesRegex(fa.IngestionError, "outside entry_path_pattern"):
            fa.load_contract(self.write(document))

    def test_rejects_a_malformed_digest(self) -> None:
        document = contract_document([{"path": "weights", "bytes": 4, "sha256": "not-a-digest"}])
        with self.assertRaisesRegex(fa.IngestionError, "64 lowercase hex"):
            fa.load_contract(self.write(document))

    def test_rejects_totals_that_disagree_with_the_file_list(self) -> None:
        document = contract_document([{"path": "weights", "bytes": 4, "sha256": "aa" * 32}])
        document["artifacts"][0]["tree"]["total_bytes"] = 5
        with self.assertRaisesRegex(fa.IngestionError, "total_bytes disagrees"):
            fa.load_contract(self.write(document))

    def test_rejects_a_count_that_disagrees_with_the_file_list(self) -> None:
        document = contract_document([{"path": "weights", "bytes": 4, "sha256": "aa" * 32}])
        document["artifacts"][0]["tree"]["entry_count"] = 2
        with self.assertRaisesRegex(fa.IngestionError, "entry_count disagrees"):
            fa.load_contract(self.write(document))

    def test_huggingface_and_direct_resolvers(self) -> None:
        document = fa.load_contract(ROOT / "ingestion-contract.json")
        by_id = {artifact["artifact_id"]: artifact for artifact in document["artifacts"]}
        self.assertEqual(
            fa.source_url(by_id["complexa-ame"], "complexa_ame.ckpt"),
            "https://huggingface.co/nvidia/NV-Proteina-Complexa-AME-160M-v1/resolve/"
            "9743d749a8754080a32fda857d95579dfa4dabae/complexa_ame.ckpt",
        )
        self.assertEqual(
            fa.source_url(by_id["rosettafold3-checkpoint"], "rf3_foundry_01_24_latest_remapped.ckpt"),
            "https://files.ipd.uw.edu/pub/rf3/rf3_foundry_01_24_latest_remapped.ckpt",
        )

    def test_selecting_an_unknown_artifact_fails(self) -> None:
        document = fa.load_contract(ROOT / "ingestion-contract.json")
        with self.assertRaisesRegex(fa.IngestionError, "no artifact"):
            fa.selected(document, ("nope",))


class TransportTests(ServerCase):
    def test_downloads_and_publishes_under_the_contracted_name(self) -> None:
        payload = blob(50_000)
        base = self.serve(payload)
        destination = self.tmp / "weights.bin"
        outcome = fa.fetch_file(f"{base}/weights.bin", destination, len(payload),
                                hashlib.sha256(payload).hexdigest(), retries=3, timeout=10, log=self.quiet)
        self.assertEqual(outcome.state, "downloaded")
        self.assertEqual(destination.read_bytes(), payload)
        self.assertFalse(destination.with_name("weights.bin.part").exists())

    def test_resumes_from_a_partial_file_instead_of_restarting(self) -> None:
        payload = blob(80_000, seed=3)
        base = self.serve(payload)
        destination = self.tmp / "weights.bin"
        destination.with_name("weights.bin.part").write_bytes(payload[:30_000])
        outcome = fa.fetch_file(f"{base}/weights.bin", destination, len(payload),
                                hashlib.sha256(payload).hexdigest(), retries=3, timeout=10, log=self.quiet)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(outcome.resumed_from, 30_000)
        self.assertEqual(outcome.downloaded_bytes, 50_000)
        self.assertEqual(_Handler.requests, ["bytes=30000-"])

    def test_a_truncated_stream_is_resumed_and_then_completes(self) -> None:
        payload = blob(60_000, seed=5)
        base = self.serve(payload, truncate_after=20_000)
        destination = self.tmp / "weights.bin"

        def stop_truncating(message: str) -> None:
            _Handler.truncate_after = None

        outcome = fa.fetch_file(f"{base}/weights.bin", destination, len(payload),
                                hashlib.sha256(payload).hexdigest(), retries=4, timeout=10, log=stop_truncating)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertGreater(outcome.attempts, 1)
        self.assertEqual(_Handler.requests, ["", "bytes=20000-"])

    def test_a_wrong_digest_is_never_published(self) -> None:
        payload = blob(10_000, seed=9)
        base = self.serve(payload)
        destination = self.tmp / "weights.bin"
        with self.assertRaisesRegex(fa.IngestionError, "does not match contracted"):
            fa.fetch_file(f"{base}/weights.bin", destination, len(payload), "bb" * 32,
                          retries=2, timeout=10, log=self.quiet)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name("weights.bin.part").exists())

    def test_a_server_that_ignores_a_range_request_is_refused(self) -> None:
        # Appending a whole second copy onto the part file is the failure this
        # guards; it would silently double the file and never match its digest.
        payload = blob(40_000, seed=11)
        base = self.serve(payload, accept_ranges=False)
        destination = self.tmp / "weights.bin"
        destination.with_name("weights.bin.part").write_bytes(payload[:10_000])
        with self.assertRaisesRegex(fa.IngestionError, "was answered 200, not 206"):
            fa.fetch_file(f"{base}/weights.bin", destination, len(payload),
                          hashlib.sha256(payload).hexdigest(), retries=2, timeout=10, log=self.quiet)
        self.assertFalse(destination.exists())

    def test_a_size_that_disagrees_with_upstream_is_refused(self) -> None:
        payload = blob(15_000, seed=13)
        base = self.serve(payload)
        destination = self.tmp / "weights.bin"
        with self.assertRaisesRegex(fa.IngestionError, "contract says"):
            fa.fetch_file(f"{base}/weights.bin", destination, len(payload) + 1,
                          hashlib.sha256(payload).hexdigest(), retries=2, timeout=10, log=self.quiet)

    def test_an_already_verified_file_is_not_downloaded_again(self) -> None:
        payload = blob(5_000, seed=17)
        base = self.serve(payload)
        destination = self.tmp / "weights.bin"
        destination.write_bytes(payload)
        outcome = fa.fetch_file(f"{base}/weights.bin", destination, len(payload),
                                hashlib.sha256(payload).hexdigest(), retries=2, timeout=10, log=self.quiet)
        self.assertEqual(outcome.state, "already-verified")
        self.assertEqual(_Handler.requests, [])

    def test_an_overlong_partial_file_is_discarded_and_refetched(self) -> None:
        payload = blob(9_000, seed=19)
        base = self.serve(payload)
        destination = self.tmp / "weights.bin"
        destination.with_name("weights.bin.part").write_bytes(payload + b"junk")
        outcome = fa.fetch_file(f"{base}/weights.bin", destination, len(payload),
                                hashlib.sha256(payload).hexdigest(), retries=3, timeout=10, log=self.quiet)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(outcome.resumed_from, 0)


class ReceiptTests(ServerCase):
    def loopback(self, base: str) -> None:
        """Resolve contracted names against the test server.

        ``source_url`` insists on https for the direct resolver, which is right
        in production and untestable over loopback, so these tests replace the
        resolution step they are not exercising. Resolution itself is covered by
        ``ContractTests.test_huggingface_and_direct_resolvers``.
        """

        original = fa.source_url
        fa.source_url = lambda artifact, filename: f"{base}/{filename}"  # type: ignore[assignment]
        self.addCleanup(setattr, fa, "source_url", original)

    def test_staging_receipt_records_source_and_identity(self) -> None:
        payload = blob(2_048, seed=23)
        base = self.serve(payload)
        self.loopback(base)
        document = contract_document([entry_for("weights.bin", payload)])
        contract = self.tmp / "contract.json"
        contract.write_text(json.dumps(document))
        staging = self.tmp / "staging"
        receipt = self.tmp / "receipt.json"
        code = fa.main([
            "--contract", str(contract), "--staging-root", str(staging), "--receipt", str(receipt),
            "--namespace", "ns", "--claim", "claim-a", "--sub-path", "some/sub/path",
        ])
        self.assertEqual(code, 0)
        document = json.loads(receipt.read_text())
        self.assertEqual(document["schema"], fa.RECEIPT_SCHEMA)
        self.assertEqual(document["staging"], {
            "namespace": "ns", "claim": "claim-a", "sub_path": "some/sub/path",
            "root": str(staging), "visibility": "task-private",
        })
        entry = document["artifacts"][0]
        self.assertEqual(entry["total_bytes"], len(payload))
        self.assertEqual(entry["files"][0]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(entry["source"]["revision"], "rev-1")

    def test_a_failing_artifact_is_recorded_and_others_still_stage(self) -> None:
        good = blob(1_024, seed=29)
        base = self.serve(good)
        self.loopback(base)
        document = contract_document([entry_for("weights.bin", good)])
        broken = json.loads(json.dumps(document["artifacts"][0]))
        broken["artifact_id"] = "broken"
        broken["files"] = [{"path": "weights.bin", "bytes": len(good), "sha256": "cc" * 32}]
        document["artifacts"].insert(0, broken)
        contract = self.tmp / "contract.json"
        contract.write_text(json.dumps(document))
        receipt = self.tmp / "receipt.json"
        code = fa.main([
            "--contract", str(contract), "--staging-root", str(self.tmp / "staging"),
            "--receipt", str(receipt), "--continue-on-artifact-error",
        ])
        self.assertEqual(code, 1)
        parsed = json.loads(receipt.read_text())
        self.assertEqual([item["artifact_id"] for item in parsed["failures"]], ["broken"])
        self.assertEqual([item["artifact_id"] for item in parsed["artifacts"]], ["demo"])


class RendererTests(unittest.TestCase):
    def render(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "name": "stage", "namespace": "ns", "run_id": "r1", "image": "python:3.11-slim",
            "claim": "claim-a", "config_map": "cm", "staging_sub_path": "a/b",
            "artifact_ids": ("complexa-protein",), "node_selectors": {"storage.example/cache": "true"},
            "run_as_user": 65532, "run_as_group": 65532, "supplemental_group": 65532,
            "cpu": "1", "memory": "2Gi", "retries": 8, "deadline_seconds": 100,
            "continue_on_artifact_error": True,
        }
        arguments.update(overrides)
        return rij.stage_job(**arguments)

    def test_never_sets_fsgroup(self) -> None:
        # fsGroup would recursively rewrite the ownership of the tenant-private
        # AlphaFold 3 and PyRosetta trees sharing this claim.
        context = self.render()["spec"]["template"]["spec"]["securityContext"]
        self.assertNotIn("fsGroup", context)
        self.assertEqual(context["supplementalGroups"], [65532])
        self.assertTrue(context["runAsNonRoot"])

    def test_declares_no_toleration_so_staging_cannot_land_on_a_gpu(self) -> None:
        self.assertNotIn("tolerations", self.render()["spec"]["template"]["spec"])

    def test_hardcodes_no_project_region_or_registry(self) -> None:
        rendered = json.dumps(self.render())
        for token in ("eu-north1", "project-e00rene", "cr.eu-north1.nebius.cloud", "nvidia.com/gpu"):
            self.assertNotIn(token, rendered)

    def test_requests_and_limits_agree_so_the_pod_is_guaranteed(self) -> None:
        resources = self.render()["spec"]["template"]["spec"]["containers"][0]["resources"]
        self.assertEqual(resources["requests"], resources["limits"])
        self.assertEqual(resources["requests"], {"cpu": "1", "memory": "2Gi"})

    def test_does_not_retry_the_pod_behind_our_back(self) -> None:
        # The tool resumes; a second pod writing the same part file does not.
        self.assertEqual(self.render()["spec"]["backoffLimit"], 0)

    def test_command_carries_the_selected_artifacts_and_receipt(self) -> None:
        command = self.render()["spec"]["template"]["spec"]["containers"][0]["command"]
        self.assertIn("--artifact-id", command)
        self.assertIn("complexa-protein", command)
        self.assertIn("--continue-on-artifact-error", command)
        self.assertIn("/claim/a/b/.receipts/staging.r1.json", command)


CATALOG = REPO / "model-artifacts"
CATALOG_MANIFESTS = {
    "complexa-protein": "manifest-complexa-protein.json",
    "complexa-ligand": "manifest-complexa-ligand.json",
    "complexa-ame": "manifest-complexa-ame.json",
    "rosettafold3-checkpoint": "manifest-rosettafold3-checkpoint.json",
}


@unittest.skipIf(not (CATALOG / "manifest-complexa-protein.json").is_file(),
                 "the accepted public artifact catalog is not present on this branch")
class CatalogAgreementTests(unittest.TestCase):
    """The ingestion contract must never drift from the accepted catalog.

    The catalog is the authority on what these artifacts *are*. This contract
    restates those identities because the staging job runs in-cluster from a
    ConfigMap and cannot read the repository, so the duplication is deliberate
    and load-bearing. Duplication that nothing checks is how two sources of
    truth quietly disagree, which is exactly the failure a pinned digest exists
    to prevent, so every restated field is compared here and drift fails closed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = {artifact["artifact_id"]: artifact
                        for artifact in fa.load_contract(ROOT / "ingestion-contract.json")["artifacts"]}

    def manifest(self, artifact_id: str) -> dict[str, object]:
        return json.loads((CATALOG / CATALOG_MANIFESTS[artifact_id]).read_text())

    def test_the_catalog_covers_every_artifact_this_task_ingests(self) -> None:
        self.assertEqual(sorted(self.contract), sorted(CATALOG_MANIFESTS))

    def test_pinned_source_revisions_agree(self) -> None:
        for artifact_id in CATALOG_MANIFESTS:
            with self.subTest(artifact_id):
                self.assertEqual(self.contract[artifact_id]["source"]["revision"],
                                 self.manifest(artifact_id)["source"]["revision"])

    def test_every_file_identity_agrees(self) -> None:
        for artifact_id in CATALOG_MANIFESTS:
            with self.subTest(artifact_id):
                manifest = self.manifest(artifact_id)
                catalog = {entry["path"]: (entry["bytes"], entry["sha256"])
                           for entry in manifest["content"]["files"]}
                ours = {entry["path"]: (entry["bytes"], entry["sha256"])
                        for entry in self.contract[artifact_id]["files"]}
                self.assertEqual(ours, catalog)
                self.assertEqual(self.contract[artifact_id]["tree"]["total_bytes"],
                                 manifest["content"]["expanded_bytes"])

    def test_licences_and_entitlements_agree(self) -> None:
        for artifact_id in CATALOG_MANIFESTS:
            with self.subTest(artifact_id):
                manifest = self.manifest(artifact_id)
                source = self.contract[artifact_id]["source"]
                self.assertEqual(source["license_id"], manifest["license"]["id"])
                self.assertEqual(source["entitlement_state"], manifest["entitlement_state"])

    def test_every_resolved_url_stays_within_the_catalog_source(self) -> None:
        # The catalog records a source URI; a direct-https manifest names the
        # file itself while this contract names the directory its resolver
        # appends to. Comparing the resolved URL rather than the raw field keeps
        # that shape difference from hiding a real divergence.
        for artifact_id, artifact in self.contract.items():
            with self.subTest(artifact_id):
                declared = self.manifest(artifact_id)["source"]["uri"]
                for entry in artifact["files"]:
                    resolved = fa.source_url(artifact, entry["path"])
                    if declared.startswith("hf://"):
                        repo = declared.removeprefix("hf://")
                        self.assertTrue(resolved.startswith(f"https://huggingface.co/{repo}/resolve/"))
                    else:
                        self.assertIn(resolved, {declared, f"{declared.rstrip('/')}/{entry['path']}"})


class PromoteRendererTests(unittest.TestCase):
    def render(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "name": "promote", "namespace": "ns", "run_id": "r1", "image": "python:3.11-slim",
            "claim": "claim-a", "config_map": "cm", "verifier_config_map": "verifier",
            "staging_sub_path": "a/b", "host_root": "/mnt/example-reference-data/data",
            "tree_prefix": "scientific-localization/public",
            "artifact_ids": ("complexa-protein",),
            "node_selectors": {"storage.example/cache": "true"},
            "reference_user": 1000, "reference_group": 1000,
            "ingress_user": 65532, "ingress_group": 65532,
            "supplemental_groups": (65532, 1000),
            "cpu": "1", "memory": "2Gi", "deadline_seconds": 100,
            "reclaim": True, "dry_run_reclaim": False,
        }
        arguments.update(overrides)
        return rij.promote_job(**arguments)

    def pod(self, **overrides: object) -> dict[str, object]:
        return self.render(**overrides)["spec"]["template"]["spec"]

    def test_the_copy_runs_before_anything_is_released(self) -> None:
        pod = self.pod()
        self.assertEqual([item["name"] for item in pod["initContainers"]], ["promote"])
        self.assertEqual([item["name"] for item in pod["containers"]], ["reclaim"])

    def test_each_step_runs_as_the_account_that_owns_what_it_writes(self) -> None:
        pod = self.pod()
        promote = pod["initContainers"][0]["securityContext"]
        reclaim = pod["containers"][0]["securityContext"]
        self.assertEqual(promote["runAsUser"], 1000)
        self.assertEqual(reclaim["runAsUser"], 65532)
        self.assertEqual(pod["securityContext"]["supplementalGroups"], [65532, 1000])

    def test_never_sets_fsgroup(self) -> None:
        self.assertNotIn("fsGroup", self.pod()["securityContext"])

    def test_the_copy_cannot_write_to_the_ingress_claim(self) -> None:
        mounts = {item["name"]: item for item in self.pod()["initContainers"][0]["volumeMounts"]}
        self.assertTrue(mounts["claim"]["readOnly"])
        self.assertNotIn("readOnly", mounts["reference"])

    def test_the_release_step_cannot_write_to_the_reference_plane(self) -> None:
        mounts = {item["name"]: item for item in self.pod()["containers"][0]["volumeMounts"]}
        self.assertTrue(mounts["reference"]["readOnly"])
        self.assertNotIn("readOnly", mounts["claim"])

    def test_publishes_under_the_host_root_with_host_addressing(self) -> None:
        command = self.pod()["initContainers"][0]["command"]
        self.assertIn("--volume-kind", command)
        self.assertEqual(command[command.index("--volume-kind") + 1], "host-path")
        self.assertEqual(command[command.index("--host-root") + 1], "/mnt/example-reference-data/data")
        generations = command[command.index("--generations-root") + 1]
        self.assertEqual(generations, "/reference/scientific-localization/public/generations")
        self.assertIn("--allow-cross-filesystem-copy", command)

    def test_the_release_step_is_driven_by_the_promotion_receipt(self) -> None:
        promote = self.pod()["initContainers"][0]["command"]
        reclaim = self.pod()["containers"][0]["command"]
        self.assertEqual(
            promote[promote.index("--receipt") + 1],
            reclaim[reclaim.index("--promotion-receipt") + 1],
        )

    def test_skipping_the_release_leaves_a_job_that_still_has_a_container(self) -> None:
        pod = self.pod(reclaim=False)
        self.assertEqual([item["name"] for item in pod["containers"]], ["done"])

    def test_hardcodes_no_project_region_or_registry(self) -> None:
        rendered = json.dumps(self.render())
        for token in ("eu-north1", "project-e00rene", "cr.eu-north1.nebius.cloud", "nvidia.com/gpu"):
            self.assertNotIn(token, rendered)

    def test_declares_no_toleration_so_promotion_cannot_land_on_a_gpu(self) -> None:
        self.assertNotIn("tolerations", self.pod())


@unittest.skipIf(localization_adapters() is None,
                 "the reviewed localization successor is not available on this branch")
class PromotionBase(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.package_parent = build_package(self.tmp / "pkg", localization_adapters())

    def stage(self, payloads: dict[str, bytes]) -> tuple[Path, Path, Path]:
        document = contract_document([entry_for(name, data) for name, data in sorted(payloads.items())])
        contract = self.tmp / "contract.json"
        contract.write_text(json.dumps(document))
        staging = self.tmp / "claim" / "staging"
        (staging / "demo").mkdir(parents=True)
        for name, data in payloads.items():
            (staging / "demo" / name).write_bytes(data)
        return contract, staging, self.tmp / "claim" / "generations"

    def promote(self, contract: Path, staging: Path, generations: Path, receipt: Path) -> int:
        return pg.main([
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--tree-sub-path", "scientific-localization/public/generations",
            "--namespace", "ns", "--claim", "claim-a", "--receipt", str(receipt),
        ])

    def pretend_cross_filesystem(self) -> None:
        """Force the cross-device branch while both paths really are local.

        The copy, the digest check, and the rename that follows it all run for
        real; only the device comparison is staged, because a test cannot
        conjure a second filesystem.
        """

        class Elsewhere:
            @staticmethod
            def stat() -> object:
                return type("S", (), {"st_dev": -1})()

        original = pg.nearest_existing
        pg.nearest_existing = lambda path: Elsewhere  # type: ignore[assignment,return-value]
        self.addCleanup(setattr, pg, "nearest_existing", original)


class PromotionTests(PromotionBase):
    def test_publishes_a_content_addressed_generation_with_its_terminal_marker(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(4_000, 31), "b.ckpt": blob(6_000, 37)})
        receipt = self.tmp / "promotion.json"
        self.assertEqual(self.promote(contract, staging, generations, receipt), 0)

        parsed = json.loads(receipt.read_text())
        entry = parsed["generations"][0]
        generation = entry["generation"]
        self.assertRegex(generation, r"^[0-9a-f]{64}$")

        published = generations / "demo" / "sha256" / generation
        self.assertTrue(published.is_dir())
        self.assertEqual(entry["published_path"], str(published))
        self.assertEqual(
            entry["sub_path"],
            f"scientific-localization/public/generations/demo/sha256/{generation}",
        )
        # The terminal marker travels inside the generation, so a consumer that
        # mounts only the generation can still admit it.
        marker = published / ".fs2-runtime-tree.json"
        self.assertTrue(marker.is_file())
        document = json.loads(marker.read_text())
        self.assertEqual(document["generation"], generation)
        self.assertEqual(document["artifact_id"], "demo")
        self.assertEqual(document["source_revision"], "rev-1")
        self.assertEqual(hashlib.sha256(marker.read_bytes()).hexdigest(), entry["marker_sha256"])
        self.assertNotIn("demo", [item.name for item in staging.iterdir()])

    def test_the_generation_and_its_files_are_read_only(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(2_000, 41)})
        receipt = self.tmp / "promotion.json"
        self.promote(contract, staging, generations, receipt)
        generation = json.loads(receipt.read_text())["generations"][0]["generation"]
        published = generations / "demo" / "sha256" / generation
        self.assertEqual(published.stat().st_mode & 0o222, 0)
        self.assertEqual((published / "a.ckpt").stat().st_mode & 0o222, 0)

    def test_the_marker_carries_no_timestamp_or_host_identity(self) -> None:
        # Two promotions of the same tree must produce byte-identical markers.
        contract, staging, generations = self.stage({"a.ckpt": blob(3_000, 43)})
        receipt = self.tmp / "promotion.json"
        self.promote(contract, staging, generations, receipt)
        generation = json.loads(receipt.read_text())["generations"][0]["generation"]
        marker = json.loads((generations / "demo" / "sha256" / generation / ".fs2-runtime-tree.json").read_text())
        for forbidden in ("observed_at", "generated_at", "node", "pod", "run_id", "duration_seconds"):
            self.assertNotIn(forbidden, marker)

    def test_identical_bytes_promote_to_the_same_generation_twice(self) -> None:
        payloads = {"a.ckpt": blob(2_500, 47)}
        contract, staging, generations = self.stage(payloads)
        first = self.tmp / "one.json"
        self.promote(contract, staging, generations, first)
        generation = json.loads(first.read_text())["generations"][0]["generation"]

        (staging / "demo").mkdir(parents=True)
        for name, data in payloads.items():
            (staging / "demo" / name).write_bytes(data)
        second = self.tmp / "two.json"
        self.assertEqual(self.promote(contract, staging, generations, second), 0)
        entry = json.loads(second.read_text())["generations"][0]
        self.assertEqual(entry["generation"], generation)
        self.assertTrue(entry["already_published"])

    def test_a_cross_filesystem_rerun_reuses_and_leaves_no_temporary_copy(self) -> None:
        payloads = {"a.ckpt": blob(3_300, 131)}
        contract, staging, generations = self.stage(payloads)
        self.pretend_cross_filesystem()
        arguments = [
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--volume-kind", "host-path", "--host-root", "/mnt/example-reference-data/data",
            "--allow-cross-filesystem-copy",
        ]
        first = self.tmp / "one.json"
        self.assertEqual(pg.main([*arguments, "--receipt", str(first)]), 0)
        generation = json.loads(first.read_text())["generations"][0]["generation"]

        second = self.tmp / "two.json"
        self.assertEqual(pg.main([*arguments, "--receipt", str(second)]), 0)
        entry = json.loads(second.read_text())["generations"][0]
        self.assertTrue(entry["already_published"])
        self.assertEqual(entry["generation"], generation)
        # The redundant second copy is released rather than left on the plane.
        self.assertEqual([item.name for item in (generations / "demo").iterdir()], ["sha256"])

    def test_a_reused_generation_whose_bytes_changed_is_refused(self) -> None:
        # The tool proved the bytes it staged; it has proved nothing about bytes
        # another writer published under the same digest.
        contract, staging, generations = self.stage({"a.ckpt": blob(2_400, 137)})
        self.pretend_cross_filesystem()
        arguments = [
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--volume-kind", "host-path", "--host-root", "/mnt/example-reference-data/data",
            "--allow-cross-filesystem-copy",
        ]
        first = self.tmp / "one.json"
        self.assertEqual(pg.main([*arguments, "--receipt", str(first)]), 0)
        published = Path(json.loads(first.read_text())["generations"][0]["published_path"])
        published.chmod(0o755)
        target = published / "a.ckpt"
        target.chmod(0o644)
        target.write_bytes(blob(2_400, 138))

        second = self.tmp / "two.json"
        self.assertEqual(pg.main([*arguments, "--receipt", str(second)]), 1)
        self.assertIn("not the", json.loads(second.read_text())["failures"][0]["error"])

    def test_a_corrupted_staged_file_is_never_promoted(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(3_500, 53)})
        (staging / "demo" / "a.ckpt").write_bytes(blob(3_500, 54))
        receipt = self.tmp / "promotion.json"
        self.assertEqual(self.promote(contract, staging, generations, receipt), 1)
        self.assertFalse((generations / "demo").exists())
        self.assertIn("does not match contracted", json.loads(receipt.read_text())["failures"][0]["error"])

    def test_an_uncontracted_extra_file_blocks_promotion(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(1_500, 59)})
        (staging / "demo" / "stowaway.txt").write_bytes(b"x")
        receipt = self.tmp / "promotion.json"
        self.assertEqual(self.promote(contract, staging, generations, receipt), 1)
        self.assertFalse((generations / "demo").exists())
        self.assertIn("uncontracted entries", json.loads(receipt.read_text())["failures"][0]["error"])

    def test_a_short_staged_file_blocks_promotion(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(1_200, 61)})
        (staging / "demo" / "a.ckpt").write_bytes(blob(1_200, 61)[:900])
        receipt = self.tmp / "promotion.json"
        self.assertEqual(self.promote(contract, staging, generations, receipt), 1)
        self.assertIn("contract says", json.loads(receipt.read_text())["failures"][0]["error"])

    def test_a_cross_device_promotion_is_refused_before_anything_is_sealed(self) -> None:
        # A cross-device rename silently becomes a copy, and this claim has no
        # room for a second copy of a 20 GiB checkpoint set.
        contract, staging, generations = self.stage({"a.ckpt": blob(1_000, 71)})

        class Elsewhere:
            @staticmethod
            def stat() -> object:
                return type("S", (), {"st_dev": -1})()

        original = pg.nearest_existing
        pg.nearest_existing = lambda path: Elsewhere  # type: ignore[assignment,return-value]
        self.addCleanup(setattr, pg, "nearest_existing", original)

        receipt = self.tmp / "promotion.json"
        self.assertEqual(self.promote(contract, staging, generations, receipt), 1)
        self.assertIn("different filesystems", json.loads(receipt.read_text())["failures"][0]["error"])
        self.assertFalse((generations / "demo").exists())
        # The staged file is untouched, so a corrected run can still promote it.
        self.assertEqual((staging / "demo" / "a.ckpt").stat().st_mode & 0o200, 0o200)

    def test_a_cross_filesystem_source_is_copied_verified_then_renamed(self) -> None:
        payloads = {"a.ckpt": blob(4_000, 83), "b.ckpt": blob(2_500, 89)}
        contract, staging, generations = self.stage(payloads)
        self.pretend_cross_filesystem()
        receipt = self.tmp / "promotion.json"
        code = pg.main([
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--namespace", "ns", "--claim", "claim-a", "--receipt", str(receipt),
            "--allow-cross-filesystem-copy",
        ])
        self.assertEqual(code, 0)
        entry = json.loads(receipt.read_text())["generations"][0]
        self.assertEqual(entry["commit"]["method"], "cross-filesystem-copy-then-rename")
        published = generations / "demo" / "sha256" / entry["generation"]
        for name, data in payloads.items():
            self.assertEqual((published / name).read_bytes(), data)
        self.assertTrue((published / ".fs2-runtime-tree.json").is_file())
        # The ingress copy survives the promotion; releasing it is a separate,
        # receipt-gated step.
        self.assertTrue((staging / "demo" / "a.ckpt").is_file())
        # No reserved temporary directory is left behind.
        self.assertEqual([item.name for item in (generations / "demo").iterdir()], ["sha256"])

    def test_a_host_backed_generation_is_addressed_by_its_host_root(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(1_800, 91)})
        self.pretend_cross_filesystem()
        receipt = self.tmp / "promotion.json"
        code = pg.main([
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--volume-kind", "host-path", "--host-root", "/mnt/example-reference-data/data",
            "--receipt", str(receipt), "--allow-cross-filesystem-copy",
        ])
        self.assertEqual(code, 0)
        entry = json.loads(receipt.read_text())["generations"][0]
        marker = json.loads(Path(entry["published_path"]).joinpath(".fs2-runtime-tree.json").read_text())
        self.assertEqual(marker["host_root"], "/mnt/example-reference-data/data")
        self.assertEqual(marker["claim"], "")
        self.assertEqual(marker["namespace"], "")

    def test_mixing_claim_and_host_addressing_is_refused(self) -> None:
        # A marker carrying both would describe a location that does not exist.
        contract, staging, generations = self.stage({"a.ckpt": blob(1_600, 93)})
        self.pretend_cross_filesystem()
        receipt = self.tmp / "promotion.json"
        code = pg.main([
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--volume-kind", "host-path", "--host-root", "/mnt/example-reference-data/data",
            "--namespace", "ns", "--claim", "claim-a",
            "--receipt", str(receipt), "--allow-cross-filesystem-copy",
        ])
        self.assertEqual(code, 1)
        self.assertIn("host root", json.loads(receipt.read_text())["failures"][0]["error"])

    def test_a_corrupted_source_never_reaches_the_destination(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(3_000, 97)})
        (staging / "demo" / "a.ckpt").write_bytes(blob(3_000, 98))
        self.pretend_cross_filesystem()
        receipt = self.tmp / "promotion.json"
        code = pg.main([
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--receipt", str(receipt), "--allow-cross-filesystem-copy",
        ])
        self.assertEqual(code, 1)
        self.assertFalse((generations / "demo" / "sha256").exists())
        self.assertIn("contract says", json.loads(receipt.read_text())["failures"][0]["error"])

    def test_the_receipt_records_the_rename_as_the_commit(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(1_100, 73)})
        receipt = self.tmp / "promotion.json"
        self.promote(contract, staging, generations, receipt)
        commit = json.loads(receipt.read_text())["generations"][0]["commit"]
        self.assertEqual(commit["method"], "same-filesystem-rename")

    def test_pruning_removes_an_empty_staging_directory_and_keeps_a_partial(self) -> None:
        payloads = {"a.ckpt": blob(1_300, 79)}
        contract, staging, generations = self.stage(payloads)
        # A second artifact that never finished downloading; its partial must
        # survive, because deleting it would cost a re-download this claim
        # cannot absorb.
        unfinished = staging / "other"
        unfinished.mkdir()
        (unfinished / "b.ckpt.part").write_bytes(b"half a file")

        receipt = self.tmp / "promotion.json"
        code = pg.main([
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--namespace", "ns", "--claim", "claim-a", "--receipt", str(receipt),
            "--prune-staging",
        ])
        self.assertEqual(code, 0)
        self.assertFalse((staging / "demo").exists())
        self.assertTrue((unfinished / "b.ckpt.part").is_file())

    def test_the_receipt_names_the_successor_functions_it_used(self) -> None:
        contract, staging, generations = self.stage({"a.ckpt": blob(800, 67)})
        receipt = self.tmp / "promotion.json"
        self.promote(contract, staging, generations, receipt)
        promoter = json.loads(receipt.read_text())["promoter"]
        self.assertEqual(promoter["module"], "fs2_localization.localization")
        self.assertIn("promote_generation", promoter["functions"])


class ReclaimTests(PromotionBase):
    def promoted(self, payloads: dict[str, bytes]) -> tuple[Path, Path, Path]:
        contract, staging, generations = self.stage(payloads)
        self.pretend_cross_filesystem()
        receipt = self.tmp / "promotion.json"
        # The real ingress crosses from the tenant claim to the Terraform-managed
        # reference plane, which is a host directory, so these exercise exactly
        # that addressing rather than the claim-backed one.
        code = pg.main([
            "--contract", str(contract), "--staging-root", str(staging),
            "--generations-root", str(generations),
            "--localization-package-parent", str(self.package_parent),
            "--volume-kind", "host-path", "--host-root", "/mnt/example-reference-data/data",
            "--receipt", str(receipt), "--allow-cross-filesystem-copy",
        ])
        self.assertEqual(code, 0)
        return receipt, staging, generations

    def reclaim(self, receipt: Path, staging: Path, out: Path, *extra: str) -> int:
        return rs.main([
            "--promotion-receipt", str(receipt), "--staging-root", str(staging),
            "--receipt", str(out), *extra,
        ])

    def test_releases_ingress_once_the_generation_is_confirmed(self) -> None:
        receipt, staging, _ = self.promoted({"a.ckpt": blob(2_200, 101)})
        out = self.tmp / "reclaim.json"
        self.assertEqual(self.reclaim(receipt, staging, out), 0)
        self.assertFalse((staging / "demo").exists())
        document = json.loads(out.read_text())
        self.assertEqual(document["bytes_released"], 2_200)
        self.assertEqual(document["released"][0]["artifact_id"], "demo")

    def test_a_dry_run_reports_without_deleting(self) -> None:
        receipt, staging, _ = self.promoted({"a.ckpt": blob(1_700, 103)})
        out = self.tmp / "reclaim.json"
        self.assertEqual(self.reclaim(receipt, staging, out, "--dry-run"), 0)
        self.assertTrue((staging / "demo" / "a.ckpt").is_file())
        self.assertEqual(json.loads(out.read_text())["released"][0]["bytes_released"], 1_700)

    def test_refuses_when_the_published_generation_is_gone(self) -> None:
        receipt, staging, generations = self.promoted({"a.ckpt": blob(1_900, 107)})
        entry = json.loads(receipt.read_text())["generations"][0]
        published = Path(entry["published_path"])
        published.chmod(0o755)
        shutil.rmtree(published)
        out = self.tmp / "reclaim.json"
        self.assertEqual(self.reclaim(receipt, staging, out), 1)
        self.assertTrue((staging / "demo" / "a.ckpt").is_file())
        self.assertIn("missing", json.loads(out.read_text())["refused"][0]["error"])

    def test_refuses_when_the_terminal_marker_was_changed(self) -> None:
        receipt, staging, _ = self.promoted({"a.ckpt": blob(2_100, 109)})
        entry = json.loads(receipt.read_text())["generations"][0]
        published = Path(entry["published_path"])
        published.chmod(0o755)
        marker = published / ".fs2-runtime-tree.json"
        marker.chmod(0o644)
        marker.write_text("{}")
        out = self.tmp / "reclaim.json"
        self.assertEqual(self.reclaim(receipt, staging, out), 1)
        self.assertTrue((staging / "demo" / "a.ckpt").is_file())
        self.assertIn("does not match", json.loads(out.read_text())["refused"][0]["error"])

    def test_refuses_a_source_outside_the_staging_root(self) -> None:
        receipt, staging, _ = self.promoted({"a.ckpt": blob(1_400, 113)})
        document = json.loads(receipt.read_text())
        outside = self.tmp / "not-staging"
        outside.mkdir()
        (outside / "a.ckpt").write_bytes(b"x")
        document["generations"][0]["source_root"] = str(outside)
        receipt.write_text(json.dumps(document))
        out = self.tmp / "reclaim.json"
        self.assertEqual(self.reclaim(receipt, staging, out), 1)
        self.assertTrue((outside / "a.ckpt").is_file())
        self.assertIn("outside the staging root", json.loads(out.read_text())["refused"][0]["error"])

    def test_a_truncated_published_file_blocks_release(self) -> None:
        receipt, staging, _ = self.promoted({"a.ckpt": blob(2_600, 127)})
        entry = json.loads(receipt.read_text())["generations"][0]
        published = Path(entry["published_path"])
        published.chmod(0o755)
        target = published / "a.ckpt"
        target.chmod(0o644)
        target.write_bytes(b"short")
        out = self.tmp / "reclaim.json"
        self.assertEqual(self.reclaim(receipt, staging, out), 1)
        self.assertTrue((staging / "demo" / "a.ckpt").is_file())
        self.assertIn("bytes", json.loads(out.read_text())["refused"][0]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
