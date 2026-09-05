from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

MODULE_PATH = Path(__file__).resolve().parents[1] / "promote_qualifications.py"
SOURCE_ROOT = MODULE_PATH.parents[2]
SPEC = importlib.util.spec_from_file_location(
    "fs2_scientific_qualification_promotion", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

FRAGMENT_VALIDATOR_PATH = (
    SOURCE_ROOT
    / "models/cancer-immunotherapy/primary-fleet-activation/validate_fragments.py"
)
FRAGMENT_SPEC = importlib.util.spec_from_file_location(
    "fs2_primary_fragment_after_public_qualification", FRAGMENT_VALIDATOR_PATH
)
assert FRAGMENT_SPEC is not None and FRAGMENT_SPEC.loader is not None
FRAGMENT_VALIDATOR = importlib.util.module_from_spec(FRAGMENT_SPEC)
sys.modules[FRAGMENT_SPEC.name] = FRAGMENT_VALIDATOR
FRAGMENT_SPEC.loader.exec_module(FRAGMENT_VALIDATOR)

PRIMARY_GLOB = "models/cancer-immunotherapy/**/activation/fragment.json"
SECONDARY_GLOB = "models/structure/batch-adapters/*/activation/workload-profile.json"
NOW = "2026-09-05T01:00:00Z"
LATER = "2026-09-05T01:05:00Z"


def canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def pretty_sorted(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def private_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


class QualificationFixture:
    def __init__(self, directory: Path) -> None:
        self.root = directory / "repository"
        self.receipts = directory / "receipts"
        self.root.mkdir()
        self.receipts.mkdir(mode=0o700)
        relative_files = {
            MODULE.PROFILE_RELATIVE,
            MODULE.EXECUTION_MAP_RELATIVE,
            MODULE.PROFILE_SCHEMA_RELATIVE,
            MODULE.ELIGIBILITY_SCHEMA_RELATIVE,
            MODULE.PRIMARY_SCHEMA_RELATIVE,
        }
        relative_files.update(
            path.relative_to(SOURCE_ROOT) for path in SOURCE_ROOT.glob(PRIMARY_GLOB)
        )
        relative_files.update(
            path.relative_to(SOURCE_ROOT) for path in SOURCE_ROOT.glob(SECONDARY_GLOB)
        )
        relative_files.update(
            path.relative_to(SOURCE_ROOT)
            for path in SOURCE_ROOT.glob(
                "models/structure/batch-adapters/*/activation/public-acceptance.json"
            )
        )
        for relative in relative_files:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE_ROOT / relative, destination)
        profile_set = self.load(self.root / MODULE.PROFILE_RELATIVE)
        execution_map = self.load(self.root / MODULE.EXECUTION_MAP_RELATIVE)
        self.profiles = {item["model_id"]: item for item in profile_set["profiles"]}
        self.executions = {item["model_id"]: item for item in execution_map["models"]}
        self.owners = MODULE._discover_owners(self.root)

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_bytes())
        assert isinstance(value, dict)
        return value

    def input_identity(self, model_id: str) -> dict[str, str]:
        owner = self.owners[model_id]
        return {
            "kind": owner.kind,
            "path": owner.acceptance_input_relative,
            "sha256": hashlib.sha256(
                owner.acceptance_input_path.read_bytes()
            ).hexdigest(),
        }

    def receipt(self, model_id: str) -> dict[str, Any]:
        profile = self.profiles[model_id]
        execution = self.executions[model_id]
        identity = profile["execution_identity"]
        decisions: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        observed: list[dict[str, Any]] = []
        for index, stage in enumerate(profile["workload"]["stages"], start=1):
            gpu = stage["resource_class"] == "gpu"
            admission = {
                "resolved_pool_id": (
                    profile["resources"]["compatible_pool_ids"][0] if gpu else None
                ),
                "admitted_resource_flavor": "inference-h100" if gpu else None,
                "accelerator_resource_name": "nvidia.com/gpu" if gpu else None,
                "accelerator_count": profile["resources"]["gpu_count"] if gpu else 0,
                "admitted_at": NOW,
            }
            attempt_id = f"attempt-{model_id}-{index}"
            shard_id = f"shard-{index:03d}"
            decisions.append(
                {
                    "stage_id": stage["id"],
                    "resource_class": stage["resource_class"],
                    "resolved_cluster_queue": (
                        "inference-accelerators" if gpu else "general-cpu"
                    ),
                    "resolved_local_queue": (
                        "inference-models" if gpu else "general-cpu"
                    ),
                    "workload_priority_class": "standard",
                    "workload_priority_value": 0,
                    "resolved_pool_preference": (
                        profile["resources"]["compatible_pool_ids"]
                        if gpu
                        else ["batch-cpu"]
                    ),
                    "accelerator_resource_name": "nvidia.com/gpu" if gpu else None,
                    "accelerator_count": (
                        profile["resources"]["gpu_count"] if gpu else 0
                    ),
                    "max_queue_seconds": None,
                    "max_execution_seconds": None,
                    "checkpoint_mode": stage["checkpoint_mode"],
                    # The frozen decision records the service-class policy,
                    # while the profile field describes stage retry behavior.
                    "preemption_mode": "restartable",
                }
            )
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "stage_id": stage["id"],
                    "shard_id": shard_id,
                    "attempt_number": 1,
                    "status": "succeeded",
                    "started_at": NOW,
                    "completed_at": LATER,
                    "scheduling_admission": admission,
                    "kueue_workload_uid": f"kueue-{model_id}-{index}",
                    "k8s_job_uid": f"job-{model_id}-{index}",
                    "pod_uids": [f"pod-{model_id}-{index}"],
                    "node_uids": ["node-h100" if gpu else "node-cpu"],
                    "gpu_uuids": ["GPU-fixture"] if gpu else [],
                    "checkpoint_input": None,
                    "checkpoint_output": None,
                }
            )
            observed.append(
                {
                    "stage_id": stage["id"],
                    "status": "succeeded",
                    "failure_code": None,
                    "attempts": [
                        {
                            "attempt_id": attempt_id,
                            "shard_id": shard_id,
                            "attempt_number": 1,
                            "workload_kind": "Job",
                            "workload_name": f"fixture-{model_id}-{index}",
                            "workload_uid": f"workload-{model_id}-{index}",
                            "workload_namespace": "fs2-models",
                            "route_namespace": "fs2-models",
                            "outcome": "succeeded",
                            "last_phase": "teardown",
                            "resource_released": True,
                            "failure_kind": None,
                            "failure_code": None,
                            "scheduling_admission": admission,
                        }
                    ],
                }
            )
        return {
            "schema": MODULE.MODEL_RECEIPT_SCHEMA,
            "endpoint": {"host": "inference.example", "tls": True},
            "model": {
                "model_id": model_id,
                "variant_id": execution["variant_id"],
            },
            "operation_identity": {
                "operation_id": f"operation-{model_id}",
                "batch_id": f"batch-{model_id}",
                "workload_id": f"workload-{model_id}",
            },
            "terminal_state": {
                "operation": "succeeded",
                "batch": "succeeded",
                "result": "succeeded",
                "semantic_validation": "passed",
            },
            "timestamps": {
                "accepted_at": NOW,
                "available_at": NOW,
                "activation_started_at": None,
                "ready_at": None,
                "started_at": NOW,
                "completed_at": LATER,
                "result_submitted_at": NOW,
                "result_completed_at": LATER,
            },
            "cold_start": {
                "cold_start_seconds": None,
                "runtime": {
                    "pod_uid": f"pod-{model_id}",
                    "node_uid": "node-h100",
                    "gpu_uuids": ["GPU-fixture"],
                    "gpu_count": profile["resources"]["gpu_count"],
                    "preemptible": False,
                },
            },
            "execution_identity": {
                "model_id": model_id,
                "variant_id": execution["variant_id"],
                "model_revision": identity["model_revision"],
                "runtime_image_digest": identity["runtime_image_digest"],
                "runtime_recipe_sha256": identity["runtime_recipe_sha256"],
                "workload_recipe_sha256": identity["workload_recipe_sha256"],
                "model_artifact_manifest_digest": identity["artifact_manifest_digest"],
                "execution_identity_sha256": identity["execution_identity_sha256"],
            },
            "queue": {
                "scheduling_snapshot_digest": "sha256:" + "1" * 64,
                "policy_revision": "2" * 64,
                "captured_at": NOW,
                "service_class": "customer-batch",
                "tenant_queue": "inference-models",
                "model_lane": model_id,
                "stage_decisions": decisions,
                "observed_stages": observed,
            },
            "attempts": attempts,
            "artifact_digests": {
                "uploads": [],
                "input_manifest": {
                    "artifact_id": f"input-{model_id}",
                    "sha256": "3" * 64,
                    "size_bytes": 100,
                    "media_type": "application/vnd.fs2.scientific-manifest+json",
                    "compression": "none",
                },
                "output_manifest": {
                    "artifact_id": f"output-{model_id}",
                    "sha256": "4" * 64,
                    "size_bytes": 200,
                    "media_type": "application/vnd.fs2.scientific-manifest+json",
                    "compression": "none",
                },
                "semantic_validation_receipt_sha256": "5" * 64,
            },
        }

    @staticmethod
    def success_row(
        model_id: str,
        source: dict[str, str],
        receipt: dict[str, Any],
        raw: bytes,
    ) -> dict[str, Any]:
        cold_start = receipt["cold_start"]
        return {
            "model_id": model_id,
            "input": source,
            "status": "succeeded",
            "receipt": {
                "path": f"{model_id}.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            },
            "operation_identity": receipt["operation_identity"],
            "terminal_state": receipt["terminal_state"],
            "execution_identity": receipt["execution_identity"],
            "api_measurements": {
                "cold_start": cold_start,
                "runtime": {
                    "runtime_identity": cold_start["runtime"],
                    "timestamps": receipt["timestamps"],
                    "attempts": receipt["attempts"],
                },
                "queue": receipt["queue"],
                "gpu_occupied_idle": {
                    "available": False,
                    "source_field": None,
                    "value": None,
                },
            },
        }

    def aggregate(
        self,
        successful: dict[str, Callable[[dict[str, Any]], None] | None],
    ) -> Path:
        rows: list[dict[str, Any]] = []
        for model_id in sorted(self.profiles):
            source = self.input_identity(model_id)
            if model_id not in successful:
                rows.append(
                    {
                        "model_id": model_id,
                        "input": source,
                        "status": "failed",
                        "error_code": "fixture_not_run",
                        "api_measurements": None,
                    }
                )
                continue
            receipt = self.receipt(model_id)
            mutate = successful[model_id]
            if mutate is not None:
                mutate(receipt)
            raw = pretty_sorted(receipt)
            private_write(self.receipts / f"{model_id}.json", raw)
            rows.append(self.success_row(model_id, source, receipt, raw))
        aggregate = {
            "schema": MODULE.AGGREGATE_SCHEMA,
            "run_id": "scientific-fleet-fixture-01",
            "endpoint": {"host": "inference.example", "tls": True},
            "summary": {
                "discovered": len(rows),
                "primary": sum(owner.primary for owner in self.owners.values()),
                "secondary": sum(not owner.primary for owner in self.owners.values()),
                "succeeded": len(successful),
                "failed": len(rows) - len(successful),
                "max_parallel": 4,
            },
            "models": rows,
        }
        path = self.receipts / "aggregate.json"
        private_write(path, canonical(aggregate))
        return path

    def acceptance_repository(self) -> Path:
        destination = self.root.parent / "acceptance-repository"
        shutil.copytree(self.root, destination)
        return destination

    def refresh_execution_map(self, changed_model_id: str) -> str:
        execution_path = self.root / MODULE.EXECUTION_MAP_RELATIVE
        execution_map = self.load(execution_path)
        changed = next(
            item
            for item in execution_map["models"]
            if item["model_id"] == changed_model_id
        )
        changed["stages"][0]["active_deadline_seconds"] += 1
        execution_path.write_bytes(pretty_sorted(execution_map))
        digest = hashlib.sha256(MODULE._helm_to_json_bytes(execution_map)).hexdigest()

        catalog_path = self.root / MODULE.PROFILE_RELATIVE
        catalog = self.load(catalog_path)
        for profile in catalog["profiles"]:
            profile["qualification"]["execution_map_sha256"] = digest
        catalog_path.write_bytes(pretty_sorted(catalog))
        for owner in MODULE._discover_owners(self.root).values():
            document = self.load(owner.path)
            profile = (
                document["profile_projection"]["profile"]
                if owner.primary
                else document["profile"]
            )
            profile["qualification"]["execution_map_sha256"] = digest
            owner.path.write_bytes(pretty_sorted(document))
        return digest


class ScientificQualificationPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = QualificationFixture(Path(self.temporary.name))

    @staticmethod
    def tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_dry_run_then_write_promotes_primary_and_secondary_idempotently(
        self,
    ) -> None:
        aggregate = self.fixture.aggregate({"esmfold2": None, "mosaic": None})
        before = self.tree_bytes(self.fixture.root)

        plan = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
        )

        self.assertEqual((plan.promoted, plan.unchanged, plan.skipped), (2, 0, 8))
        self.assertEqual(plan.written_paths, ())
        self.assertEqual(self.tree_bytes(self.fixture.root), before)

        applied = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
            write=True,
        )

        self.assertEqual(
            (applied.promoted, applied.unchanged, applied.skipped), (2, 0, 8)
        )
        catalog = self.fixture.load(self.fixture.root / MODULE.PROFILE_RELATIVE)
        profiles = {item["model_id"]: item for item in catalog["profiles"]}
        for model_id in ("esmfold2", "mosaic"):
            profile = profiles[model_id]
            qualification = profile["qualification"]
            public_raw = (self.fixture.receipts / f"{model_id}.json").read_bytes()
            self.assertEqual(profile["state"], "qualified")
            self.assertEqual(profile["semantic_validation"]["state"], "qualified")
            self.assertEqual(
                qualification["public_completion_receipt_sha256"],
                hashlib.sha256(public_raw).hexdigest(),
            )
            eligibility = list(
                self.fixture.owners[model_id].eligibility_directory.glob(
                    "scheduler-eligibility-*.json"
                )
            )
            self.assertEqual(len(eligibility), 1)
            self.assertEqual(
                qualification["scheduler_eligibility_receipt_sha256"],
                hashlib.sha256(eligibility[0].read_bytes()).hexdigest(),
            )
            receipt = self.fixture.load(eligibility[0])
            self.assertEqual(
                receipt["execution_map_sha256"],
                receipt["acceptance_execution_map_sha256"],
            )
            self.assertEqual(
                receipt["model_execution_map_entry_sha256"],
                hashlib.sha256(
                    canonical(self.fixture.executions[model_id]).rstrip(b"\n")
                ).hexdigest(),
            )
            self.assertEqual(qualification["qualified_at"], LATER)

        primary = self.fixture.load(self.fixture.owners["mosaic"].path)
        fragment_schema = self.fixture.load(
            SOURCE_ROOT
            / "models/cancer-immunotherapy/primary-fleet-activation/fragment.schema.json"
        )
        Draft202012Validator(fragment_schema, format_checker=FormatChecker()).validate(
            primary
        )
        self.assertEqual(
            FRAGMENT_VALIDATOR.validate_fragment_document(
                primary, self.fixture.owners["mosaic"].path
            ),
            [],
        )
        self.assertEqual(
            primary["accepted_evidence"]["h100"]["state"],
            MODULE.PRIMARY_QUALIFIED_STATE,
        )
        self.assertIs(primary["activation_gate"]["public_platform_run_required"], False)
        secondary = self.fixture.load(self.fixture.owners["esmfold2"].path)
        self.assertEqual(secondary["profile"], profiles["esmfold2"])
        self.assertEqual(
            (self.fixture.root / MODULE.EXECUTION_MAP_RELATIVE).read_bytes(),
            before[MODULE.EXECUTION_MAP_RELATIVE.as_posix()],
        )
        self.assertEqual(profiles["boltzgen"], self.fixture.profiles["boltzgen"])

        repeated = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
            write=True,
        )
        self.assertEqual((repeated.promoted, repeated.unchanged), (0, 2))
        self.assertEqual(repeated.written_paths, ())

    def test_only_exact_identity_and_scheduler_matches_are_promoted(self) -> None:
        def stale_identity(receipt: dict[str, Any]) -> None:
            receipt["execution_identity"]["runtime_image_digest"] = "sha256:" + "9" * 64

        def wrong_pool(receipt: dict[str, Any]) -> None:
            gpu = next(
                item
                for item in receipt["queue"]["stage_decisions"]
                if item["resource_class"] == "gpu"
            )
            gpu["resolved_pool_preference"] = ["unqualified-pool"]

        aggregate = self.fixture.aggregate(
            {
                "esmfold2": None,
                "mosaic": stale_identity,
                "openfold3-openbind": wrong_pool,
            }
        )

        result = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
            write=True,
        )

        decisions = {item.model_id: item for item in result.decisions}
        self.assertEqual(decisions["esmfold2"].action, "promote")
        self.assertEqual(
            decisions["mosaic"].reason, "receipt_execution_identity_mismatch"
        )
        self.assertEqual(
            decisions["openfold3-openbind"].reason,
            "scheduler_gpu_decision_mismatch",
        )
        catalog = self.fixture.load(self.fixture.root / MODULE.PROFILE_RELATIVE)
        profiles = {item["model_id"]: item for item in catalog["profiles"]}
        self.assertEqual(profiles["esmfold2"]["state"], "qualified")
        self.assertEqual(profiles["mosaic"], self.fixture.profiles["mosaic"])
        self.assertEqual(
            profiles["openfold3-openbind"],
            self.fixture.profiles["openfold3-openbind"],
        )

    def test_request_selected_optional_stage_subset_is_promoted(self) -> None:
        def omit_affinity(receipt: dict[str, Any]) -> None:
            receipt["queue"]["stage_decisions"] = [
                item
                for item in receipt["queue"]["stage_decisions"]
                if item["stage_id"] != "affinity"
            ]
            receipt["queue"]["observed_stages"] = [
                item
                for item in receipt["queue"]["observed_stages"]
                if item["stage_id"] != "affinity"
            ]
            receipt["attempts"] = [
                item for item in receipt["attempts"] if item["stage_id"] != "affinity"
            ]

        aggregate = self.fixture.aggregate({"boltzgen": omit_affinity})

        result = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
            write=True,
        )

        boltzgen = next(
            item for item in result.decisions if item.model_id == "boltzgen"
        )
        self.assertEqual(boltzgen.action, "promote")
        owner = MODULE._discover_owners(self.fixture.root)["boltzgen"]
        eligibility_path = next(
            owner.eligibility_directory.glob("scheduler-eligibility-*.json")
        )
        eligibility = self.fixture.load(eligibility_path)
        self.assertEqual(
            [
                item["stage_id"]
                for item in eligibility["scheduling_snapshot"]["stage_decisions"]
            ],
            [
                "configure",
                "design",
                "inverse-folding",
                "folding",
                "design-folding",
                "analysis",
                "filtering",
            ],
        )
        self.assertNotIn(
            "affinity",
            {item["stage_id"] for item in eligibility["successful_admissions"]},
        )

    def test_selected_stage_subset_must_preserve_declared_dependencies(self) -> None:
        def omit_folding(receipt: dict[str, Any]) -> None:
            receipt["queue"]["stage_decisions"] = [
                item
                for item in receipt["queue"]["stage_decisions"]
                if item["stage_id"] != "folding"
            ]
            receipt["queue"]["observed_stages"] = [
                item
                for item in receipt["queue"]["observed_stages"]
                if item["stage_id"] != "folding"
            ]
            receipt["attempts"] = [
                item for item in receipt["attempts"] if item["stage_id"] != "folding"
            ]

        aggregate = self.fixture.aggregate({"boltzgen": omit_folding})

        result = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
            write=True,
        )

        boltzgen = next(
            item for item in result.decisions if item.model_id == "boltzgen"
        )
        self.assertEqual(boltzgen.action, "skip")
        self.assertEqual(boltzgen.reason, "scheduler_stage_dependency_mismatch")

    def test_private_canonical_aggregate_and_exact_input_digest_are_required(
        self,
    ) -> None:
        aggregate = self.fixture.aggregate({"mosaic": None})
        os.chmod(aggregate, 0o644)
        with self.assertRaisesRegex(MODULE.PromotionError, "aggregate_invalid"):
            MODULE.promote(
                repository_root=self.fixture.root,
                aggregate_path=aggregate,
            )

        os.chmod(aggregate, 0o600)
        document = self.fixture.load(aggregate)
        mosaic = next(
            item for item in document["models"] if item["model_id"] == "mosaic"
        )
        mosaic["input"]["sha256"] = "0" * 64
        aggregate.unlink()
        private_write(aggregate, canonical(document))
        before = self.tree_bytes(self.fixture.root)
        result = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
            write=True,
        )
        decision = next(item for item in result.decisions if item.model_id == "mosaic")
        self.assertEqual(decision.reason, "acceptance_input_digest_mismatch")
        self.assertEqual(result.promoted, 0)
        self.assertEqual(self.tree_bytes(self.fixture.root), before)

    def test_explicit_acceptance_repository_bridges_only_unrelated_map_drift(
        self,
    ) -> None:
        aggregate = self.fixture.aggregate({"mosaic": None})
        acceptance_root = self.fixture.acceptance_repository()
        accepted_map = self.fixture.load(acceptance_root / MODULE.PROFILE_RELATIVE)[
            "profiles"
        ][0]["qualification"]["execution_map_sha256"]
        current_map = self.fixture.refresh_execution_map("alphafold3")
        self.assertNotEqual(current_map, accepted_map)

        strict = MODULE.promote(
            repository_root=self.fixture.root,
            aggregate_path=aggregate,
            write=True,
        )
        strict_mosaic = next(
            item for item in strict.decisions if item.model_id == "mosaic"
        )
        self.assertEqual(strict_mosaic.reason, "acceptance_input_digest_mismatch")
        self.assertEqual(strict.promoted, 0)

        bridged = MODULE.promote(
            repository_root=self.fixture.root,
            acceptance_repository_root=acceptance_root,
            aggregate_path=aggregate,
            write=True,
        )
        self.assertEqual(bridged.promoted, 1)
        self.assertEqual(bridged.acceptance_execution_map_sha256, accepted_map)
        self.assertEqual(bridged.execution_map_sha256, current_map)
        owner = MODULE._discover_owners(self.fixture.root)["mosaic"]
        evidence_path = next(
            owner.eligibility_directory.glob("scheduler-eligibility-*.json")
        )
        evidence = self.fixture.load(evidence_path)
        self.assertEqual(evidence["acceptance_execution_map_sha256"], accepted_map)
        self.assertEqual(evidence["execution_map_sha256"], current_map)
        self.assertEqual(
            evidence["model_execution_map_entry_sha256"],
            hashlib.sha256(
                MODULE._canonical_bytes(
                    next(
                        item
                        for item in self.fixture.load(
                            self.fixture.root / MODULE.EXECUTION_MAP_RELATIVE
                        )["models"]
                        if item["model_id"] == "mosaic"
                    )
                )
            ).hexdigest(),
        )

    def test_acceptance_repository_rejects_target_execution_map_drift(self) -> None:
        aggregate = self.fixture.aggregate({"mosaic": None})
        acceptance_root = self.fixture.acceptance_repository()
        self.fixture.refresh_execution_map("mosaic")

        result = MODULE.promote(
            repository_root=self.fixture.root,
            acceptance_repository_root=acceptance_root,
            aggregate_path=aggregate,
            write=True,
        )

        mosaic = next(item for item in result.decisions if item.model_id == "mosaic")
        self.assertEqual(mosaic.reason, "acceptance_model_execution_map_drift")
        self.assertEqual(result.promoted, 0)


if __name__ == "__main__":
    unittest.main()
