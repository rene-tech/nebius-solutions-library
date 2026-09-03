"""Bind the public-generation evidence to digests that cannot be hand-edited.

An independent review of the five-generation publication found that the
generation and marker digests were cross-checked between the handoff, the
publication receipts and the two node admissions, but that the numbers a reader
actually reads to judge a tree -- ``entry_count``, ``total_bytes``, the upstream
``archive_provenance`` digest -- were bound to nothing.  Editing any of them by
hand left the whole suite green, so the evidence could disagree with the bytes
it describes without a single test noticing.

The fix is to hash rather than to compare.  Every generation carries an
in-generation marker whose digest is already pinned, and the marker document is
checked in whole.  Re-deriving that digest from the checked-in document makes
every field inside it tamper-evident at once: the generation, the entry count,
the total bytes, the upstream archive digest, the sub-path and the licence.  The
remaining assertions then anchor the receipts and both node admissions to that
one verified document instead of to each other.

The recomputation reproduces ``marker_bytes`` from the localization adapter --
``json.dumps(document, indent=2, sort_keys=True)`` plus a trailing newline --
deliberately by restatement rather than by import, so that a change to the
serializer has to be made twice before a pinned marker digest can move.

The identities asserted here were confirmed against the live cluster on
2026-09-03: two task-owned CPU Jobs, one pinned to each reserved H100 node, read
the public reference-data host root and recomputed all five generation
identities from the bytes on disk with zero mismatches.  That receipt is checked
in beside the candidate's own evidence and is bound by this module too.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALIZATION = ROOT / "models/cancer-immunotherapy/artifact-localization"
EVIDENCE = LOCALIZATION / "evidence"
GATE = LOCALIZATION / "independent-gate"

MARKER_NAME = ".fs2-runtime-tree.json"


def marker_digest(document: dict) -> str:
    """Reproduce ``marker_bytes`` from the localization adapter, by restatement."""

    return hashlib.sha256(
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()


class PublicGenerationEvidenceBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.handoff = cls.load(EVIDENCE / "binding-handoff.json")
        cls.publication = cls.load(
            EVIDENCE / "public-generation-publication-20260903.json"
        )
        cls.qualification = cls.load(
            EVIDENCE / "public-generation-node-qualification-20260903.json"
        )
        cls.recompute = cls.load(
            GATE / "public-generation-independent-recompute-20260903.json"
        )
        cls.anchor = cls.load(
            GATE / "public-generation-content-anchor-20260903.json"
        )
        cls.by_id = {entry["artifact_id"]: entry for entry in cls.handoff["artifacts"]}
        cls.public_ids = {
            entry["artifact_id"]
            for entry in cls.handoff["artifacts"]
            if entry["visibility"] == "public"
        }
        cls.receipts = {
            item["receipt"]["artifact_id"]: item["receipt"]
            for item in cls.publication["artifacts"]
        }
        cls.admission_jobs = [
            job
            for job in cls.qualification["jobs"]
            if job["kind"] == "all-public-marker-admission"
        ]

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_marker_document_reproduces_its_pinned_digest(self) -> None:
        """The one binding that makes every other field in the marker immutable."""

        self.assertTrue(self.by_id, "the handoff declares no artifacts")
        for artifact_id, entry in sorted(self.by_id.items()):
            with self.subTest(artifact=artifact_id):
                marker = entry["marker"]
                document = marker["document"]
                self.assertEqual(marker["manifest_digest"], marker_digest(document))
                # The digest only protects the fields the document actually
                # carries, so the identity the rest of the evidence quotes has
                # to come from inside it.
                self.assertEqual(entry["generation"], document["generation"])
                self.assertEqual(entry["generation"], document["inventory_sha256"])
                # The public trees are flat; the externally installed academic
                # tree is nested and keeps its producer's own algorithm, so the
                # binding is that the document names a known one, not one one.
                self.assertIn(
                    document["inventory_algorithm"],
                    {
                        "fs2-flat-tree-inventory/v1",
                        "fs2-tree-inventory/v2",
                        "fs2-tree-manifest/v1",
                    },
                )
                if artifact_id in self.public_ids:
                    self.assertEqual(
                        "fs2-flat-tree-inventory/v1", document["inventory_algorithm"]
                    )
                self.assertTrue(
                    document["sub_path"].endswith(f"/sha256/{entry['generation']}"),
                    document["sub_path"],
                )
                self.assertTrue(document["read_only"])

    def test_receipt_tree_identity_and_provenance_match_the_verified_marker(self) -> None:
        """A receipt may not describe a different tree than the marker it pins."""

        self.assertEqual(self.public_ids, set(self.receipts))
        for artifact_id in sorted(self.public_ids):
            with self.subTest(artifact=artifact_id):
                document = self.by_id[artifact_id]["marker"]["document"]
                receipt = self.receipts[artifact_id]
                identity = receipt["tree_identity"]
                self.assertEqual(document["entry_count"], identity["entry_count"])
                self.assertEqual(document["total_bytes"], identity["total_bytes"])
                self.assertEqual(
                    document["inventory_algorithm"], identity["inventory_algorithm"]
                )
                self.assertEqual(document["generation"], identity["inventory_sha256"])
                self.assertLessEqual(
                    identity["probe_entries_verified"], identity["entry_count"]
                )
                self.assertGreaterEqual(identity["probe_entries_verified"], 1)
                # Provenance is the upstream object, never the tree it produced.
                archive = receipt["archive_provenance"]
                self.assertEqual(document["source_sha256"], archive["sha256"])
                self.assertEqual(document["source_bytes"], archive["bytes"])
                self.assertEqual(document["source_filename"], archive["filename"])
                self.assertEqual(document["source_revision"], archive["source_revision"])
                self.assertEqual(document["license_id"], archive["license_id"])
                self.assertNotEqual(archive["sha256"], identity["inventory_sha256"])
                self.assertFalse(archive["present_in_mount"])
                self.assertEqual(
                    document["marker_name"] if "marker_name" in document else MARKER_NAME,
                    MARKER_NAME,
                )

    def test_both_node_admissions_describe_the_same_verified_tree(self) -> None:
        """Two nodes, one tree: an admission may not report its own numbers."""

        self.assertEqual(2, len(self.admission_jobs))
        self.assertEqual(2, len({job["node_digest"] for job in self.admission_jobs}))
        self.assertEqual(
            self.qualification["outcome"]["h100_nodes_admitted"],
            len({job["node_digest"] for job in self.admission_jobs}),
        )
        for job in self.admission_jobs:
            with self.subTest(node=job["node_digest"]):
                # An admission that failed is not evidence of an admission.
                self.assertEqual([0], job["exit_codes"])
                self.assertEqual("passed", job["result"]["state"])
                self.assertEqual(0, job["gpu_requested"])
                admitted = {
                    item["artifact_id"]: item for item in job["marker_admissions"]
                }
                self.assertEqual(self.public_ids, set(admitted))
                for artifact_id, item in sorted(admitted.items()):
                    document = self.by_id[artifact_id]["marker"]["document"]
                    self.assertEqual("admitted", item["state"])
                    self.assertEqual(document["generation"], item["generation"])
                    self.assertEqual(
                        self.by_id[artifact_id]["marker"]["manifest_digest"],
                        item["manifest_digest"],
                    )
                    self.assertEqual(document["entry_count"], item["entry_count"])
                    self.assertEqual(document["total_bytes"], item["total_bytes"])
                    self.assertEqual(document["visibility"], item["visibility"])

    def test_publication_outcome_counts_match_the_receipts_it_summarises(self) -> None:
        """A summary line may not claim more artifacts than the file carries."""

        outcome = self.publication["outcome"]
        self.assertEqual(len(self.receipts), outcome["artifact_count"])
        self.assertEqual(
            len(self.public_ids), self.qualification["outcome"]["public_artifact_count"]
        )
        self.assertEqual(0, self.qualification["outcome"]["gpu_allocations_created"])
        for job in self.publication["jobs"]:
            with self.subTest(job=job["name"]):
                self.assertEqual([0], job["exit_codes"])
        produced = {
            artifact
            for job in self.publication["jobs"]
            for artifact in job["artifacts"]
        }
        self.assertEqual(self.public_ids, produced)
        for artifact_id, receipt in sorted(self.receipts.items()):
            with self.subTest(artifact=artifact_id):
                producer = next(
                    item["producer_job"]
                    for item in self.publication["artifacts"]
                    if item["receipt"]["artifact_id"] == artifact_id
                )
                job = next(
                    item for item in self.publication["jobs"] if item["name"] == producer
                )
                self.assertIn(artifact_id, job["artifacts"])
                self.assertEqual("verified", receipt["state"])

    def test_independent_live_recompute_confirms_the_published_identities(self) -> None:
        """The gate's own live recompute is bound to the evidence it cleared."""

        self.assertEqual("confirmed", self.recompute["outcome"]["state"])
        self.assertEqual(
            self.recompute["outcome"]["h100_nodes"],
            self.recompute["outcome"]["h100_nodes_admitted_by_candidate"]
            + self.recompute["outcome"]["h100_nodes_added_by_reviewer"],
        )
        self.assertEqual(0, self.recompute["outcome"]["mismatches"])
        self.assertFalse(self.recompute["method"]["shares_code_with_candidate"])
        self.assertEqual(0, self.recompute["method"]["gpu_allocations_created"])
        self.assertFalse(self.recompute["cluster"]["b300_touched"])

        script = (GATE / self.recompute["method"]["script"]).read_bytes()
        self.assertEqual(
            self.recompute["method"]["script_sha256"],
            hashlib.sha256(script).hexdigest(),
        )

        nodes = self.recompute["nodes"]
        admitted = {job["node_digest"] for job in self.admission_jobs}
        recomputed_on = {node["node_digest"] for node in nodes}
        # The reviewer read the generations on every H100 node that carried the
        # reference-data label, which is a superset of the two the candidate
        # admitted: two more joined the pool while the review was running.
        self.assertTrue(admitted <= recomputed_on, admitted - recomputed_on)
        self.assertEqual(len(nodes), len(recomputed_on))
        self.assertEqual(
            len(nodes), self.recompute["node_digest_derivation"]["distinct_nodes"]
        )
        self.assertEqual(len(nodes), self.recompute["outcome"]["h100_nodes"])
        self.assertEqual(
            admitted,
            {node["node_digest"] for node in nodes if node["admitted_by_candidate"]},
        )
        self.assertEqual(
            len(admitted),
            self.recompute["outcome"]["h100_nodes_admitted_by_candidate"],
        )
        # A node the candidate never probed may not be counted as one it did.
        self.assertEqual(
            len(nodes) - len(admitted),
            self.recompute["outcome"]["h100_nodes_added_by_reviewer"],
        )
        # The opaque instance IDs stay out of the public export, so the digest is
        # the only node identity this file may carry; it is bound to the
        # candidate's admissions above rather than to a name.
        self.assertNotIn("node_resource_id", json.dumps(self.recompute))
        for node in nodes:
            with self.subTest(node=node["node_digest"]):
                self.assertEqual([0], node["exit_codes"])
                self.assertEqual("passed", node["state"])
                self.assertEqual(0, node["gpu_requested"])
                recomputed = {item["artifact_id"]: item for item in node["generations"]}
                self.assertEqual(self.public_ids, set(recomputed))
                for artifact_id, item in sorted(recomputed.items()):
                    entry = self.by_id[artifact_id]
                    document = entry["marker"]["document"]
                    self.assertEqual("passed", item["state"])
                    self.assertTrue(all(item["checks"].values()), item["checks"])
                    self.assertEqual(
                        entry["generation"], item["recomputed_inventory_sha256"]
                    )
                    self.assertEqual(
                        entry["marker"]["manifest_digest"],
                        item["recomputed_marker_sha256"],
                    )
                    self.assertEqual(
                        document["entry_count"], item["recomputed_entry_count"]
                    )
                    self.assertEqual(
                        document["total_bytes"], item["recomputed_total_bytes"]
                    )


    def test_a_content_anchor_exists_for_every_published_generation(self) -> None:
        """The generation name binds CRC-32; something has to bind the content.

        ``fs2-flat-tree-inventory/v1`` serialises ``{bytes, crc32, path}``, so the
        SHA-256 that names a generation covers checksums rather than content
        digests.  CRC-32 is affine over GF(2), so above four bytes a same-length,
        same-CRC-32, different-content file is constructed rather than searched
        for, and two such files produce one generation name.  The published trees
        are still exactly what the receipts describe -- that was recomputed on
        four nodes -- but the name alone cannot prove it to a reader who does not
        already trust the writer.  This binds the real digests instead.
        """

        self.assertEqual("anchored", self.anchor["outcome"]["state"])
        self.assertEqual(
            "fs2-flat-tree-content-sha256/v1", self.anchor["method"]["algorithm"]
        )
        self.assertEqual(0, self.anchor["method"]["gpu_allocations_created"])
        self.assertFalse(self.anchor["cluster"]["b300_touched"])
        self.assertNotIn("node_resource_id", json.dumps(self.anchor))

        script = (GATE / self.anchor["method"]["script"]).read_bytes()
        self.assertEqual(
            self.anchor["method"]["script_sha256"], hashlib.sha256(script).hexdigest()
        )

        anchored = {item["artifact_id"]: item for item in self.anchor["generations"]}
        self.assertEqual(self.public_ids, set(anchored))
        self.assertEqual(
            len(self.public_ids), self.anchor["outcome"]["generations_anchored"]
        )
        for artifact_id in sorted(self.public_ids):
            with self.subTest(artifact=artifact_id):
                document = self.by_id[artifact_id]["marker"]["document"]
                item = anchored[artifact_id]
                self.assertEqual(document["generation"], item["generation"])
                # The anchor must describe the same tree as the verified marker,
                # or it is anchoring something else.
                self.assertEqual(document["entry_count"], item["entry_count"])
                self.assertEqual(document["total_bytes"], item["total_bytes"])
                # A content digest that equals the CRC-32-based name would mean
                # the anchor was copied rather than computed.
                self.assertRegex(item["content_tree_sha256"], r"^[0-9a-f]{64}$")
                self.assertNotEqual(
                    item["content_tree_sha256"], document["generation"]
                )
                files = item.get("files")
                if files is not None:
                    self.assertEqual(item["entry_count"], len(files))
                    self.assertEqual(
                        item["total_bytes"], sum(row["bytes"] for row in files)
                    )
                    for row in files:
                        self.assertRegex(row["sha256"], r"^[0-9a-f]{64}$")

        # Every anchored digest is distinct: two trees sharing one content digest
        # would mean the anchor cannot tell them apart either.
        digests = [item["content_tree_sha256"] for item in self.anchor["generations"]]
        self.assertEqual(len(digests), len(set(digests)))

    def test_the_content_anchor_reproduces_the_candidates_own_probe_digests(self) -> None:
        """Independent bytes, the candidate's number: the two must agree."""

        probe = next(
            job["result"]["mpnn_weights"]
            for job in self.qualification["jobs"]
            if job.get("model_id") == "bindcraft-public-artifacts"
        )
        anchored = {item["artifact_id"]: item for item in self.anchor["generations"]}
        cases = {
            "colabdesign-mpnn-weights-soluble": "soluble",
            "colabdesign-mpnn-weights-vanilla": "original",
        }
        corroboration = self.anchor["corroboration"]
        for artifact_id, key in sorted(cases.items()):
            with self.subTest(artifact=artifact_id):
                claimed = probe[key]
                files = {row["path"]: row["sha256"] for row in anchored[artifact_id]["files"]}
                self.assertEqual(claimed["sha256"], files[claimed["checkpoint"]])
                self.assertEqual(claimed["sha256"], corroboration[artifact_id]["sha256"])
                self.assertEqual(claimed["checkpoint"], corroboration[artifact_id]["file"])
                self.assertTrue(corroboration[artifact_id]["matches_candidate_probe"])
        # The two weight sets are different bytes, which is the whole point of
        # shipping both; a probe reporting one digest twice would be wrong.
        self.assertNotEqual(probe["soluble"]["sha256"], probe["original"]["sha256"])

    def test_no_localization_evidence_file_claims_an_unproven_live_state(self) -> None:
        """Restore a raw-text guard that a later edit narrowed to one file.

        The guard this replaces once asserted, over the raw text of every
        evidence document, that none of them carried ``"generations_published":
        true`` or a ``live`` binding state.  It was rewritten to parse each
        document and check only those with a top-level ``evidence`` key, which
        is ``binding-handoff.json`` alone.  Every other evidence file, and every
        future one, silently stopped being covered.  Scanning the text again
        costs nothing and does not care how a document is shaped.
        """

        paths = sorted(EVIDENCE.glob("*.json")) + sorted(GATE.glob("*.json"))
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            with self.subTest(evidence=path.relative_to(EVIDENCE.parent).as_posix()):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn('"binding_state": "live"', text)
                # The tenant-private PyRosetta tree is not promoted, so no
                # document may report the whole handoff as published.
                self.assertNotIn('"generations_published": true', text)


if __name__ == "__main__":
    unittest.main()
