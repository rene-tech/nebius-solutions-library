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
        # Authoritative qualification, not a guess from the pool name.
        "model_eligible_pool_ids": {
            "example-model": ["preemptible"],
            "alphafold3": ["preemptible"],
            "hopper-only-model": ["hopper"],
        },

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
        # The affinity is present regardless; the selector narrows it further.
        self.assertEqual(
            pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
                "nodeSelectorTerms"
            ][0]["matchExpressions"][0]["values"],
            ["preemptible"],
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
                "resource_kind": "gpu",
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
                "reserved_resource_usage": {
                    "example.com/accelerator": "1",
                    "cpu": "500m",
                    "memory": "512Mi",
                },
                # Exactly as Kueue reported them, per PodSet.
                "reserved_pod_set_assignments": [
                    {
                        "name": "main",
                        "count": 1,
                        "flavors": {"example.com/accelerator": "example-preemptible"},
                        "resource_usage": {
                            "example.com/accelerator": "1",
                            "cpu": "500m",
                            "memory": "512Mi",
                        },
                    }
                ],
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

        # A CPU-only stage has no accelerator facts to record.
        cpu_stage = json.loads(json.dumps(receipt))
        cpu_stage["workload"].update(
            {
                "resource_kind": "cpu",
                "accelerator_resource": None,
                "accelerator_count": 0,
                "pool_id": None,
                "resource_flavor": None,
                "actual_resource_flavor": "fs2-core",
                "actual_pool_id": "reference-cpu",
                "cpu_class": "reference-data",
                # One pool behind one flavor, so the pool is known as soon as
                # quota is reserved, exactly like a GPU attempt.
                "pool_resolution_mode": "per-pool-flavor",
                "reserved_resource_usage": {"cpu": "16", "memory": "64Gi"},
                "reserved_pod_set_assignments": [
                    {
                        "name": "main",
                        "count": 1,
                        "flavors": {"cpu": "fs2-core", "memory": "fs2-core"},
                        "resource_usage": {"cpu": "16", "memory": "64Gi"},
                    }
                ],
            }
        )
        self.assertFalse(list(validator.iter_errors(cpu_stage)))
        invented = json.loads(json.dumps(cpu_stage))
        invented["workload"]["accelerator_count"] = 1
        self.assertTrue(list(validator.iter_errors(invented)))

        # A CPU stage is placed by class; a GPU stage is not.
        without_class = json.loads(json.dumps(cpu_stage))
        without_class["workload"]["cpu_class"] = None
        self.assertTrue(list(validator.iter_errors(without_class)))
        gpu_with_class = json.loads(json.dumps(receipt))
        gpu_with_class["workload"]["cpu_class"] = "reference-data"
        self.assertTrue(list(validator.iter_errors(gpu_with_class)))

        # Any valid Kubernetes ResourceName is recordable, not a fixed list.
        for resource_name in ("hugepages-2Mi", "example.com/rdma_shared_device_a", "cpu"):
            with self.subTest(resource=resource_name):
                valid = json.loads(json.dumps(receipt))
                valid["workload"]["reserved_resource_usage"] = {resource_name: "1"}
                valid["workload"]["reserved_pod_set_assignments"][0]["resource_usage"] = {
                    resource_name: "1"
                }
                self.assertFalse(list(validator.iter_errors(valid)))
        for resource_name in ("a" * 254 + "/gpu", "example.com/" + "g" * 64):
            with self.subTest(rejected_resource=resource_name):
                invalid = json.loads(json.dumps(receipt))
                invalid["workload"]["reserved_resource_usage"] = {resource_name: "1"}
                self.assertTrue(list(validator.iter_errors(invalid)))

        # Quantities follow the apimachinery grammar.
        for bad_quantity in ("5ni", "3mi", "-1", "1Gib"):
            with self.subTest(quantity=bad_quantity):
                invalid = json.loads(json.dumps(receipt))
                invalid["workload"]["reserved_resource_usage"]["cpu"] = bad_quantity
                self.assertTrue(list(validator.iter_errors(invalid)))
        for good_quantity in ("500m", "2.5", "1e3", "4Ti", "100n", "250u", "0"):
            with self.subTest(quantity=good_quantity):
                valid = json.loads(json.dumps(receipt))
                valid["workload"]["reserved_resource_usage"]["cpu"] = good_quantity
                self.assertFalse(list(validator.iter_errors(valid)))
        # The actual admission tuple is mandatory and must not be inferable
        # from the requested policy.
        for missing in (
            "actual_cluster_queue",
            "actual_resource_flavor",
            "reserved_resource_usage",
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
                invalid["workload"]["reserved_resource_usage"] = bad_usage
                self.assertTrue(list(validator.iter_errors(invalid)))
        # QuotaReserved and Admitted stay separate timestamps.
        for missing in ("quota_reserved_at", "admitted_at"):
            with self.subTest(timing=missing):
                incomplete = json.loads(json.dumps(receipt))
                del incomplete["timing"][missing]
                self.assertTrue(list(validator.iter_errors(incomplete)))

        # A scheduled record must carry the pool the stage actually ran on.
        without_pool = json.loads(json.dumps(receipt))
        del without_pool["workload"]["actual_pool_id"]
        self.assertTrue(list(validator.iter_errors(without_pool)))
        null_pool_after_scheduling = json.loads(json.dumps(receipt))
        null_pool_after_scheduling["workload"]["actual_pool_id"] = None
        self.assertTrue(list(validator.iter_errors(null_pool_after_scheduling)))

        # A reservation that never became an admission is representable, and
        # must not carry an invented actual tuple.
        reserved_only = json.loads(json.dumps(receipt))
        reserved_only["outcome"]["admission_state"] = "quota-reserved"
        reserved_only["timing"]["admitted_at"] = None
        reserved_only["timing"]["pod_scheduled_at"] = None
        # status.admission exists at QuotaReserved, so the tuple stays; only
        # the admission timestamp is absent.
        self.assertFalse(list(validator.iter_errors(reserved_only)))
        borrowed = json.loads(json.dumps(reserved_only))
        borrowed["timing"]["admitted_at"] = "2026-09-02T00:00:03Z"
        self.assertTrue(list(validator.iter_errors(borrowed)))
        # A class whose ResourceFlavor spans several pools has no knowable
        # pool until a Pod is scheduled. QuotaReserved happens before that, so
        # requiring a pool here would force the collector to invent one. The
        # ClusterQueue, the flavor, and the reserved assignments are known at
        # QuotaReserved and stay required.
        reserved_before_scheduling = json.loads(json.dumps(reserved_only))
        reserved_before_scheduling["workload"].update(
            {
                "resource_kind": "cpu",
                "accelerator_resource": None,
                "accelerator_count": 0,
                "pool_id": None,
                "resource_flavor": None,
                "actual_resource_flavor": "general-cpu",
                "actual_pool_id": None,
                "cpu_class": "general-cpu",
                # One ResourceFlavor over several pools: the pool is knowable
                # only from the Node the Pod lands on, so it is null until
                # then. This is a recorded property of the class, not an
                # assumption about CPU work in general.
                "pool_resolution_mode": "node-label-observation",
                "reserved_resource_usage": {"cpu": "8", "memory": "32Gi"},
                "reserved_pod_set_assignments": [
                    {
                        "name": "main",
                        "count": 1,
                        "flavors": {"cpu": "general-cpu", "memory": "general-cpu"},
                        "resource_usage": {"cpu": "8", "memory": "32Gi"},
                    }
                ],
            }
        )
        self.assertFalse(list(validator.iter_errors(reserved_before_scheduling)))
        # The same for a reservation lost before anything was scheduled.
        lost_before_scheduling = json.loads(json.dumps(reserved_before_scheduling))
        lost_before_scheduling["outcome"]["admission_state"] = "reservation-lost"
        lost_before_scheduling["timing"]["reservation_lost_at"] = "2026-09-02T00:00:09Z"
        self.assertFalse(list(validator.iter_errors(lost_before_scheduling)))
        # A per-pool-flavor CPU class knows its pool at QuotaReserved, so a
        # null there is a missing fact rather than an unknown one. Both
        # classes this repository and its contributor produce today are
        # per-pool-flavor; the allowance above applies only to a class that
        # actually records the multi-pool mode.
        per_pool_cpu_reserved = json.loads(json.dumps(reserved_before_scheduling))
        per_pool_cpu_reserved["workload"]["pool_resolution_mode"] = "per-pool-flavor"
        self.assertTrue(list(validator.iter_errors(per_pool_cpu_reserved)))
        per_pool_cpu_reserved["workload"]["actual_pool_id"] = "general-cpu-small"
        self.assertFalse(list(validator.iter_errors(per_pool_cpu_reserved)))

        # A CPU attempt must say which mode it froze rather than leaving a
        # consumer to guess which rule applies.
        without_mode = json.loads(json.dumps(reserved_before_scheduling))
        del without_mode["workload"]["pool_resolution_mode"]
        self.assertTrue(list(validator.iter_errors(without_mode)))

        # A GPU reservation resolves its pool from the accelerator flavor map
        # at admission, so a null there is a missing fact, not an unknown one.
        gpu_reserved = json.loads(json.dumps(reserved_only))
        gpu_reserved["workload"]["actual_pool_id"] = None
        self.assertEqual(gpu_reserved["workload"]["resource_kind"], "gpu")
        self.assertTrue(list(validator.iter_errors(gpu_reserved)))

        # Once a Pod is scheduled the pool is knowable, so a null is refused.
        scheduled_without_pool = json.loads(json.dumps(reserved_before_scheduling))
        scheduled_without_pool["timing"]["pod_scheduled_at"] = "2026-09-02T00:00:07Z"
        self.assertTrue(list(validator.iter_errors(scheduled_without_pool)))
        scheduled_without_pool["workload"]["actual_pool_id"] = "reference-cpu"
        self.assertFalse(list(validator.iter_errors(scheduled_without_pool)))

        for dropped in (
            "actual_cluster_queue",
            "actual_resource_flavor",
            "reserved_resource_usage",
        ):
            with self.subTest(reserved_without=dropped):
                incomplete = json.loads(json.dumps(reserved_only))
                del incomplete["workload"][dropped]
                self.assertTrue(list(validator.iter_errors(incomplete)))

        # A never-reserved attempt has no reservation facts at all.
        pending = json.loads(json.dumps(reserved_only))
        pending["outcome"]["admission_state"] = "pending"
        pending["timing"]["quota_reserved_at"] = None
        pending["timing"]["queue_latency_seconds"] = None
        pending["workload"]["actual_cluster_queue"] = None
        pending["workload"]["actual_resource_flavor"] = None
        pending["workload"]["actual_pool_id"] = None
        pending["workload"]["reserved_resource_usage"] = {}
        pending["workload"]["reserved_pod_set_assignments"] = []
        self.assertFalse(list(validator.iter_errors(pending)))
        for invented_field, value in (
            ("quota_reserved_at", "2026-09-02T00:00:02Z"),
            ("queue_latency_seconds", 2),
        ):
            with self.subTest(pending_with=invented_field):
                invented = json.loads(json.dumps(pending))
                invented["timing"][invented_field] = value
                self.assertTrue(list(validator.iter_errors(invented)))
        invented_usage = json.loads(json.dumps(pending))
        invented_usage["workload"]["reserved_resource_usage"] = {"cpu": "500m"}
        self.assertTrue(list(validator.iter_errors(invented_usage)))

        # A lost reservation keeps what was actually held.
        lost = json.loads(json.dumps(reserved_only))
        lost["outcome"]["admission_state"] = "reservation-lost"
        lost["timing"]["reservation_lost_at"] = "2026-09-02T00:00:09Z"
        self.assertFalse(list(validator.iter_errors(lost)))
        stripped = json.loads(json.dumps(lost))
        del stripped["workload"]["reserved_resource_usage"]
        self.assertTrue(list(validator.iter_errors(stripped)))
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

    def test_a_model_must_be_qualified_for_the_pool_it_is_placed_on(self) -> None:
        """A service class lists every pool; qualification is separate."""

        # The head of the preference is not eligible for this model, so the
        # renderer selects the pool the model is actually qualified for.
        value = contract()
        value["pools"]["hopper"] = {
            "resource_flavor": "example-hopper",
            "accelerator_resource": "example.com/accelerator",
            "accelerator_resource_name": "example.com/accelerator",
            "capacity": 2,
        }
        for policy in value["service_classes"].values():
            policy["pool_preference"] = ["preemptible", "hopper"]
        value["cluster_queues"]["scientific"]["spec"]["resourceGroups"][0]["flavors"].append(
            {"name": "example-hopper", "resources": [{"name": "example.com/accelerator", "nominalQuota": "2"}]}
        )
        value["local_queue_routes"]["lane-a"]["model_ids"] = ["example-model", "hopper-only-model"]
        raw = json.dumps(value).encode("utf-8")
        path = Path(self.directory.name) / "heterogeneous.json"
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        job = self.render(
            "preemptor",
            contract=path,
            contract_sha256=digest,
            model_id="hopper-only-model",
        )["items"][0]
        self.assertEqual(
            job["metadata"]["annotations"]["fs2.nebius.ai/resource-flavor-preference"],
            "example-hopper",
        )
        # The globally preferred flavor is first in the queue's order and is
        # incompatible, so the Pod itself must be constrained. Kueue does not
        # read the annotation.
        expressions = job["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertEqual(
            expressions,
            [
                {
                    "key": "accelerator.fs2.nebius/pool-id",
                    "operator": "In",
                    "values": ["hopper"],
                }
            ],
        )
        self.assertNotIn("nodeSelector", job["spec"]["template"]["spec"])

        # A model eligible for both keeps the queue's order inside its own set.
        both = self.render(
            "preemptor",
            contract=path,
            contract_sha256=digest,
            model_id="example-model",
        )["items"][0]
        self.assertEqual(
            both["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"],
            ["preemptible"],
        )

        # An explicit pool the model is not qualified for is refused.
        with self.assertRaisesRegex(RENDERER.ContractError, "outside the pools model"):
            self.render(
                "preemptor",
                contract=path,
                contract_sha256=digest,
                model_id="hopper-only-model",
                pool_id="preemptible",
            )

    def test_a_model_eligible_for_warm_and_burst_keeps_both(self) -> None:
        """Eligibility is a set, so a model can burst beyond its warm pool."""

        value = contract()
        value["pools"]["burst"] = {
            "resource_flavor": "example-burst",
            "accelerator_resource_name": "example.com/accelerator",
            "capacity": 4,
        }
        for policy in value["service_classes"].values():
            policy["pool_preference"] = ["preemptible", "burst"]
        value["cluster_queues"]["scientific"]["spec"]["resourceGroups"][0]["flavors"].append(
            {"name": "example-burst", "resources": [{"name": "example.com/accelerator", "nominalQuota": "4"}]}
        )
        value["model_eligible_pool_ids"]["example-model"] = ["preemptible", "burst"]
        raw = json.dumps(value).encode("utf-8")
        path = Path(self.directory.name) / "warm-burst.json"
        path.write_bytes(raw)
        job = self.render(
            "preemptor",
            contract=path,
            contract_sha256=hashlib.sha256(raw).hexdigest(),
        )["items"][0]
        # Both pools, in the queue's search order, so Kueue may place the work
        # on either without leaving the qualified set.
        self.assertEqual(
            job["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"],
            ["preemptible", "burst"],
        )
        self.assertEqual(
            job["metadata"]["annotations"]["fs2.nebius.ai/eligible-pool-ids"],
            "preemptible,burst",
        )

    def test_a_model_with_no_eligible_pool_is_refused(self) -> None:
        # No intersection with the service class's order.
        value = contract()
        value["model_eligible_pool_ids"]["example-model"] = ["absent-pool"]
        raw = json.dumps(value).encode("utf-8")
        path = Path(self.directory.name) / "ineligible.json"
        path.write_bytes(raw)
        with self.assertRaisesRegex(RENDERER.ContractError, "not qualified for any pool"):
            self.render(
                "preemptor",
                contract=path,
                contract_sha256=hashlib.sha256(raw).hexdigest(),
            )

        # A model the producer says has no deployed compatible pool.
        empty = contract()
        empty["model_eligible_pool_ids"]["example-model"] = []
        raw = json.dumps(empty).encode("utf-8")
        path = Path(self.directory.name) / "empty-eligibility.json"
        path.write_bytes(raw)
        with self.assertRaisesRegex(RENDERER.ContractError, "no eligible pools"):
            self.render(
                "preemptor",
                contract=path,
                contract_sha256=hashlib.sha256(raw).hexdigest(),
            )

        # A model absent from the map is unknown here, not unrestricted.
        missing = contract()
        del missing["model_eligible_pool_ids"]["example-model"]
        raw = json.dumps(missing).encode("utf-8")
        path = Path(self.directory.name) / "missing-eligibility.json"
        path.write_bytes(raw)
        with self.assertRaisesRegex(RENDERER.ContractError, "no eligible pools"):
            self.render(
                "preemptor",
                contract=path,
                contract_sha256=hashlib.sha256(raw).hexdigest(),
            )

    def test_an_override_cannot_bypass_the_resolved_lane(self) -> None:
        with self.assertRaisesRegex(RENDERER.ContractError, "not the lane this policy resolves"):
            self.render(
                "preemptor",
                model_id="alphafold3",
                tenant_a="tenant-academic",
                queue_a="lane-a",
            )
