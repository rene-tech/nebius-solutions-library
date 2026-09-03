from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
RENDERER_PATH = ROOT / "acceptance/scientific-scheduling/render_acceptance.py"
EVIDENCE_SCHEMA_PATH = ROOT / "acceptance/scientific-scheduling/evidence.schema.json"
SPEC = importlib.util.spec_from_file_location("scientific_scheduling_acceptance", RENDERER_PATH)
assert SPEC is not None and SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def contract() -> dict:
    flavor = {
        "name": "example-preemptible",
        "resources": [{"name": "example.com/accelerator", "nominalQuota": "4"}],
    }
    queue = {
        "apiVersion": "kueue.x-k8s.io/v1beta2",
        "kind": "ClusterQueue",
        "metadata": {"name": "scientific"},
        "spec": {
            "resourceGroups": [
                {"coveredResources": ["example.com/accelerator"], "flavors": [flavor]}
            ]
        },
    }
    local_queues = {
        name: {
            "apiVersion": "kueue.x-k8s.io/v1beta2",
            "kind": "LocalQueue",
            "metadata": {"name": name, "namespace": "fs2-models"},
            "spec": {"clusterQueue": "scientific"},
        }
        for name in ("lane-a", "lane-b", "inference-models")
    }
    local_queues["academic-scientific"] = {
        "apiVersion": "kueue.x-k8s.io/v1beta2",
        "kind": "LocalQueue",
        "metadata": {"name": "academic-scientific", "namespace": "fs2-academic-poc"},
        "spec": {"clusterQueue": "scientific"},
    }
    service_classes = {
        "platform-critical": {
            "workload_priority_class": "platform-critical",
            "priority": 10000,
            "default_local_queue": "inference-models",
            "preemption_mode": "restartable",
            "pool_preference": ["preemptible"],
            "caller_selectable": False,
        },
        "presentation": {
            "workload_priority_class": "presentation",
            "priority": 1000,
            "default_local_queue": "lane-a",
            "preemption_mode": "restartable",
            "pool_preference": ["preemptible"],
            "max_queue_seconds": 60,
            "max_execution_seconds": 900,
            "caller_selectable": True,
        },
        "interactive": {
            "workload_priority_class": "interactive",
            "priority": 100,
            "default_local_queue": "lane-a",
            "preemption_mode": "restartable",
            "pool_preference": ["preemptible"],
            "caller_selectable": True,
        },
        "customer-batch": {
            "workload_priority_class": "standard",
            "priority": 0,
            "default_local_queue": "lane-a",
            "preemption_mode": "restartable",
            "pool_preference": ["preemptible"],
            "caller_selectable": True,
        },
        "bulk-backfill": {
            "workload_priority_class": "batch",
            "priority": -100,
            "default_local_queue": "lane-b",
            "preemption_mode": "restartable",
            "pool_preference": ["preemptible"],
            "max_queue_seconds": 3600,
            "max_execution_seconds": 21600,
            "caller_selectable": True,
        },
    }
    return {
        "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
        "cohort": None,
        "cluster_queues": {"scientific": queue},
        "local_queues": local_queues,
        "workload_priority_classes": {
            name: {
                "apiVersion": "kueue.x-k8s.io/v1beta2",
                "kind": "WorkloadPriorityClass",
                "metadata": {"name": name},
                "value": value,
            }
            for name, value in {
                "platform-critical": 10000,
                "presentation": 1000,
                "interactive": 100,
                "standard": 0,
                "batch": -100,
            }.items()
        },
        "service_classes": service_classes,
        "local_queue_routes": {
            "lane-a": {
                "namespace": "fs2-models",
                "cluster_queue": "scientific",
                "model_ids": ["example-model"],
                "tenant_ids": ["tenant-a"],
                "service_classes": ["presentation", "interactive", "customer-batch", "bulk-backfill"],
            },
            "lane-b": {
                "namespace": "fs2-models",
                "cluster_queue": "scientific",
                "model_ids": ["example-model"],
                "tenant_ids": ["tenant-b"],
                "service_classes": ["presentation", "interactive", "customer-batch", "bulk-backfill"],
            },
            "inference-models": {
                "namespace": "fs2-models",
                "cluster_queue": "scientific",
                "model_ids": [],
                "tenant_ids": [],
                "service_classes": [],
            },
            "academic-scientific": {
                "namespace": "fs2-academic-poc",
                "cluster_queue": "scientific",
                "model_ids": ["alphafold3"],
                "tenant_ids": ["tenant-academic"],
                "service_classes": [
                    "platform-critical",
                    "presentation",
                    "interactive",
                    "customer-batch",
                    "bulk-backfill",
                ],
            },
        },
        "namespace_bound_models": {"alphafold3": "fs2-academic-poc"},
        "cluster_queue_namespaces": {"scientific": ["fs2-academic-poc", "fs2-models"]},
        "pool_node_label_key": "accelerator.fs2.nebius/pool-id",
        "pools": {
            "preemptible": {
                "resource_flavor": "example-preemptible",
                "accelerator_resource_name": "example.com/accelerator",
                "capacity": 4,
            }
        },
        "pool_capacity": {"preemptible": 4},
        "shared_pool_quota": {"preemptible": 4},
    }


class RendererFixture:
    """Shared renderer fixture; not a TestCase, so nothing runs twice."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.contract_path = Path(self.directory.name) / "contract.json"
        raw = json.dumps(contract()).encode("utf-8")
        self.contract_path.write_bytes(raw)
        self.contract_sha256 = hashlib.sha256(raw).hexdigest()
        self.base = {
            "contract": self.contract_path,
            "contract_sha256": self.contract_sha256,
            "run_id": "r20260902a",
            "image": "registry.example.invalid/holder@sha256:" + "a" * 64,
            "model_id": "example-model",
            "tenant_a": "tenant-a",
            "tenant_b": "tenant-b",
            "queue_a": None,
            "queue_b": None,
            "pool_id": None,
            "victim_service_class": "bulk-backfill",
            "preemptor_service_class": "presentation",
            "parallelism": 2,
            "minimum_parallelism": None,
            "hold_seconds": 900,
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def render(self, scenario: str, **overrides: object) -> dict:
        return RENDERER.render(argparse.Namespace(scenario=scenario, **(self.base | overrides)))


class ScientificSchedulingAcceptanceTests(RendererFixture, unittest.TestCase):

    def test_priority_victim_and_preemptor_are_frozen_from_policy(self) -> None:
        victim = self.render("victims", tenant_a="tenant-b")["items"][0]
        preemptor = self.render("preemptor")["items"][0]

        self.assertEqual(victim["metadata"]["labels"]["kueue.x-k8s.io/priority-class"], "batch")
        self.assertEqual(victim["metadata"]["labels"]["kueue.x-k8s.io/queue-name"], "lane-b")
        self.assertEqual(victim["metadata"]["labels"]["kueue.x-k8s.io/max-exec-time-seconds"], "21600")
        self.assertEqual(victim["metadata"]["annotations"]["fs2.nebius.ai/max-queue-seconds"], "3600")
        self.assertEqual(preemptor["metadata"]["labels"]["kueue.x-k8s.io/priority-class"], "presentation")
        self.assertEqual(preemptor["spec"]["parallelism"], 1)
        self.assertEqual(victim, self.render("victims", tenant_a="tenant-b")["items"][0])

    def test_fairness_and_partial_admission_are_explicit(self) -> None:
        fairness = self.render(
            "fairness",
            victim_service_class="customer-batch",
            queue_a="lane-a",
            queue_b="lane-b",
            tenant_a="tenant-a",
            tenant_b="tenant-b",
        )
        self.assertEqual(
            [item["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] for item in fairness["items"]],
            ["lane-a", "lane-b"],
        )
        partial = self.render(
            "partial-admission",
            tenant_a="tenant-b",
            minimum_parallelism=1,
            parallelism=4,
        )["items"][0]
        self.assertEqual(partial["metadata"]["annotations"]["kueue.x-k8s.io/job-min-parallelism"], "1")
        self.assertEqual(partial["spec"]["completionMode"], "Indexed")

    def test_scale_zero_forces_reviewed_pool_without_vendor_literals(self) -> None:
        job = self.render(
            "scale-zero",
            tenant_a="tenant-b",
            pool_id="preemptible",
        )["items"][0]
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(
            pod["nodeSelector"], {"accelerator.fs2.nebius/pool-id": "preemptible"}
        )
        self.assertEqual(
            pod["containers"][0]["resources"]["requests"]["example.com/accelerator"], "1"
        )
        self.assertNotIn("nvidia", json.dumps(job).lower())
        self.assertNotIn("b300", json.dumps(job).lower())

    def test_renderer_rejects_unpinned_images_and_route_bypass(self) -> None:
        with self.assertRaisesRegex(RENDERER.ContractError, "sha256"):
            self.render("preemptor", image="registry.example.invalid/holder:latest")
        with self.assertRaisesRegex(RENDERER.ContractError, "not caller-selectable"):
            self.render("preemptor", preemptor_service_class="platform-critical")
        # An exact tenant route wins, so tenant-b resolves to its own lane
        # rather than being refused.
        self.assertEqual(
            self.render("preemptor", tenant_a="tenant-b")["items"][0]["metadata"]["labels"][
                "kueue.x-k8s.io/queue-name"
            ],
            "lane-b",
        )
        with self.assertRaisesRegex(RENDERER.ContractError, "DNS label"):
            self.render("preemptor", run_id="a..b")
        with self.assertRaisesRegex(RENDERER.ContractError, "DNS label"):
            self.render("preemptor", model_id="a..b")
        with self.assertRaisesRegex(RENDERER.ContractError, "label value"):
            self.render("preemptor", tenant_a="tenant/a")

    def test_evidence_schema_is_valid_and_accepts_a_complete_receipt(self) -> None:
        schema = json.loads(EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        receipt = {
            "schema": "fs2-serve.nebius.ai/scientific-scheduling-acceptance-evidence/v1",
            "run_id": "r20260902a",
            "source_commit": "a" * 40,
            "scheduling_contract_sha256": "b" * 64,
            "target": {
                "project_id": "project-example",
                "region": "example-region1",
                "cluster_id": "cluster-example",
                "context": "context-example",
                "gpu_type": "example-accelerator",
                "capacity_type": "preemptible",
            },
            "workload": {
                "scenario": "partial-admission",
                "namespace": "fs2-models",
                "job": "fs2-sa-r20260902a-partial-1-example",
                "workload_uid": "uid-example",
                "service_class": "bulk-backfill",
                "local_queue": "lane-b",
                "cluster_queue": "scientific",
                "priority_class": "batch",
                "pool_id": "preemptible",
                "resource_flavor": "example-preemptible",
                "accelerator_resource": "example.com/accelerator",
                "accelerator_count": 1,
                "model_id": "example-model",
                "tenant_id": "tenant-b",
                "operation_id": "operation-example",
                "workload_id": "workload-example",
                "attempt_id": "attempt-example",
                "stage_id": "design",
                "cpu_class": None,
                "actual_cluster_queue": "scientific",
                "actual_resource_flavor": "example-preemptible",
                "actual_pool_id": "preemptible",
                "admitted_resource_usage": {
                    "example.com/accelerator": "1",
                    "cpu": "500m",
                    "memory": "512Mi",
                },
            },
            "timing": {
                "attempt_queued_at": "2026-09-02T00:00:00Z",
                "created_at": "2026-09-02T00:00:00Z",
                "quota_reserved_at": "2026-09-02T00:00:02Z",
                "admitted_at": "2026-09-02T00:00:03Z",
                "pod_scheduled_at": "2026-09-02T00:00:05Z",
                "queue_latency_seconds": 2,
                "preempted_at": None,
                "requeued_at": None,
                "reservation_lost_at": None,
                "node_ready_at": None,
            },
            "outcome": {
                "admission_state": "admitted",
                "passed": True,
                "observed_conditions": ["QuotaReserved", "Admitted"],
                "event_reasons": ["Started"],
                "graceful_termination_observed": False,
            },
            "cleanup": {
                "completed": True,
                "jobs_remaining": 0,
                "workloads_remaining": 0,
                "temporary_nodes_remaining": 0,
            },
        }
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)

        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        # The actual admission tuple is mandatory and must not be inferable
        # from the requested policy.
        for missing in (
            "actual_cluster_queue",
            "actual_resource_flavor",
            "admitted_resource_usage",
            "model_id",
            "tenant_id",
            "operation_id",
            "workload_id",
            "attempt_id",
        ):
            with self.subTest(missing=missing):
                incomplete = json.loads(json.dumps(receipt))
                del incomplete["workload"][missing]
                self.assertTrue(list(validator.iter_errors(incomplete)))
        for bad_usage in ({}, {"cpu": "not-a-quantity"}, {"cpu": 500}):
            with self.subTest(usage=bad_usage):
                invalid = json.loads(json.dumps(receipt))
                invalid["workload"]["admitted_resource_usage"] = bad_usage
                self.assertTrue(list(validator.iter_errors(invalid)))
        # QuotaReserved and Admitted stay separate timestamps.
        for missing in ("quota_reserved_at", "admitted_at"):
            with self.subTest(timing=missing):
                incomplete = json.loads(json.dumps(receipt))
                del incomplete["timing"][missing]
                self.assertTrue(list(validator.iter_errors(incomplete)))

        # An admitted record must carry the pool the flavor maps to.
        without_pool = json.loads(json.dumps(receipt))
        del without_pool["workload"]["actual_pool_id"]
        self.assertTrue(list(validator.iter_errors(without_pool)))

        # A reservation that never became an admission is representable, and
        # must not carry an invented actual tuple.
        reserved_only = json.loads(json.dumps(receipt))
        reserved_only["outcome"]["admission_state"] = "quota-reserved"
        reserved_only["timing"]["admitted_at"] = None
        reserved_only["timing"]["pod_scheduled_at"] = None
        reserved_only["workload"]["actual_cluster_queue"] = None
        reserved_only["workload"]["actual_resource_flavor"] = None
        reserved_only["workload"]["actual_pool_id"] = None
        reserved_only["workload"]["admitted_resource_usage"] = {}
        self.assertFalse(list(validator.iter_errors(reserved_only)))

        # A never-reserved attempt has no reservation facts at all.
        pending = json.loads(json.dumps(reserved_only))
        pending["outcome"]["admission_state"] = "pending"
        pending["timing"]["quota_reserved_at"] = None
        pending["timing"]["queue_latency_seconds"] = None
        self.assertFalse(list(validator.iter_errors(pending)))
        invented = json.loads(json.dumps(pending))
        invented["timing"]["quota_reserved_at"] = "2026-09-02T00:00:02Z"
        self.assertTrue(list(validator.iter_errors(invented)))

        lost = json.loads(json.dumps(reserved_only))
        lost["outcome"]["admission_state"] = "reservation-lost"
        lost["timing"]["reservation_lost_at"] = "2026-09-02T00:00:09Z"
        self.assertFalse(list(validator.iter_errors(lost)))
        del lost["timing"]["reservation_lost_at"]
        self.assertTrue(list(validator.iter_errors(lost)))

        # A retry is a new attempt with its own queue clock.
        retry = json.loads(json.dumps(receipt))
        retry["workload"]["attempt_id"] = "attempt-example-2"
        retry["timing"]["attempt_queued_at"] = "2026-09-02T00:01:00Z"
        retry["timing"]["quota_reserved_at"] = "2026-09-02T00:01:04Z"
        retry["timing"]["admitted_at"] = "2026-09-02T00:01:05Z"
        retry["timing"]["queue_latency_seconds"] = 4
        retry["timing"]["requeued_at"] = "2026-09-02T00:00:59Z"
        self.assertFalse(list(validator.iter_errors(retry)))
        no_clock = json.loads(json.dumps(retry))
        del no_clock["timing"]["attempt_queued_at"]
        self.assertTrue(list(validator.iter_errors(no_clock)))


if __name__ == "__main__":
    unittest.main()


class AcceptanceContractFencingTests(RendererFixture, unittest.TestCase):
    def test_raw_contract_bytes_must_match_the_published_revision(self) -> None:
        with self.assertRaisesRegex(RENDERER.ContractError, "expected contract SHA-256"):
            self.render("preemptor", contract_sha256="not-a-digest")
        with self.assertRaisesRegex(RENDERER.ContractError, "do not match the expected"):
            self.render("preemptor", contract_sha256="0" * 64)

    def test_revision_is_the_digest_of_the_exact_applied_bytes(self) -> None:
        """Unicode and <>& must not be re-escaped before hashing."""

        value = contract()
        value["service_classes"]["presentation"]["description"] = "caf\u00e9 <a> & <b>"
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        path = Path(self.directory.name) / "unicode-contract.json"
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        rendered = self.render(
            "preemptor",
            contract=path,
            contract_sha256=digest,
        )
        annotations = rendered["items"][0]["metadata"]["annotations"]
        self.assertEqual(annotations["fs2.nebius.ai/scheduling-contract-sha256"], digest)
        self.assertNotEqual(
            digest,
            hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        )

    def test_academic_model_resolves_to_its_claim_namespace_without_an_override(self) -> None:
        job = self.render(
            "preemptor",
            model_id="alphafold3",
            tenant_a="tenant-academic",
        )["items"][0]
        self.assertEqual(job["metadata"]["namespace"], "fs2-academic-poc")
        self.assertEqual(
            job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"], "academic-scientific"
        )

    def test_wrong_tenant_cannot_take_a_namespace_bound_model_to_another_namespace(self) -> None:
        # tenant-a has no academic lane, and the service class default lane is
        # restricted to another model, so the request is refused outright.
        with self.assertRaisesRegex(RENDERER.ContractError, "restricted to other models"):
            self.render("preemptor", model_id="alphafold3", tenant_a="tenant-a")

        # Even with a permissive default lane, a namespace-bound model must not
        # resolve into a namespace that cannot mount its claim.
        value = contract()
        value["local_queue_routes"]["lane-a"]["model_ids"] = ["example-model", "alphafold3"]
        value["local_queue_routes"]["lane-a"]["tenant_ids"] = []
        raw = json.dumps(value).encode("utf-8")
        path = Path(self.directory.name) / "permissive-default.json"
        path.write_bytes(raw)
        with self.assertRaisesRegex(RENDERER.ContractError, "bound to namespace"):
            self.render(
                "preemptor",
                contract=path,
                contract_sha256=hashlib.sha256(raw).hexdigest(),
                model_id="alphafold3",
                tenant_a="tenant-a",
            )

    def test_a_restricted_default_lane_cannot_admit_another_tenant(self) -> None:
        value = contract()
        # Make the bulk lane the customer-batch default and restrict it.
        value["service_classes"]["customer-batch"]["default_local_queue"] = "lane-b"
        # Model-permissive but tenant-restricted, so only the tenant filter can
        # refuse this caller.
        value["local_queue_routes"]["lane-b"]["model_ids"] = []
        raw = json.dumps(value).encode("utf-8")
        path = Path(self.directory.name) / "restricted-default.json"
        path.write_bytes(raw)
        with self.assertRaisesRegex(RENDERER.ContractError, "restricted to other tenants"):
            self.render(
                "victims",
                contract=path,
                contract_sha256=hashlib.sha256(raw).hexdigest(),
                victim_service_class="customer-batch",
                model_id="unrouted-model",
                tenant_a="tenant-a",
            )

    def test_an_override_cannot_bypass_the_resolved_lane(self) -> None:
        with self.assertRaisesRegex(RENDERER.ContractError, "not the lane this policy resolves"):
            self.render(
                "preemptor",
                model_id="alphafold3",
                tenant_a="tenant-academic",
                queue_a="lane-a",
            )
