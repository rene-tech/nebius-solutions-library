from __future__ import annotations

import copy
import importlib.util
import os
import unittest
from pathlib import Path


VERIFIER_PATH = Path(__file__).with_name("verify_accelerator_pool_plan.py")
INFRA_ROOT = VERIFIER_PATH.parents[1]
SYNTHETIC_TARGETS = Path(__file__).with_name("fixtures") / "public-synthetic-targets.json"
os.environ["K8S_INFERENCE_TARGET_CONTRACT_PATH"] = str(SYNTHETIC_TARGETS)
SPEC = importlib.util.spec_from_file_location("fs2_accelerator_plan", VERIFIER_PATH)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)

PROJECT_ID = "project-syntheticlocal"
SOURCE_COMMIT = "a" * 40


class AcceleratorPoolPlanTests(unittest.TestCase):
    def test_terraform_contract_is_stable_id_bounds_only(self) -> None:
        variables = (INFRA_ROOT / "variables.tf").read_text(encoding="utf-8")
        data = (INFRA_ROOT / "data.tf").read_text(encoding="utf-8")
        outputs = (INFRA_ROOT / "outputs.tf").read_text(encoding="utf-8")
        cluster = (INFRA_ROOT / "cluster.tf").read_text(encoding="utf-8")

        self.assertIn('variable "accelerator_pool_capacity_overrides"', variables)
        self.assertIn("type        = map(map(number))", variables)
        self.assertIn(
            'toset(keys(bounds)) == toset(["min_nodes", "max_nodes"])',
            variables,
        )
        self.assertIn(
            "toset(keys(local.selected_accelerator_pool_profile.pools))", data
        )
        self.assertIn(
            "local.selected_accelerator_pool_profile.pools[pool_id].max_nodes",
            data,
        )
        self.assertIn("override_mode", variables)
        self.assertIn('"capacity-only-patch"', variables)
        self.assertIn('output "accelerator_pool_contract_sha256"', outputs)
        self.assertIn("length(var.accelerator_pool_capacity_overrides) == 0", variables)
        for immutable_projection in (
            "each.value.provider.platform",
            "each.value.provider.preset",
            "each.value.provider.driver.preset",
            "each.value.accelerator.resource_api.resource_name",
            "each.value.features.mig.mode",
            "each.value.region_availability",
        ):
            self.assertIn(immutable_projection, cluster)
        self.assertNotIn(
            "accelerator_pool_capacity_overrides[pool_id].provider", variables
        )
        self.assertNotIn(
            "accelerator_pool_capacity_overrides[pool_id].accelerator", variables
        )

    def fixture(
        self,
        *,
        mode: str = "noop",
        capacity_profile: str = "minimal",
        accelerator_profile: str | None = None,
        floor_profile: str = "zero",
        overrides: dict | None = None,
    ) -> dict:
        effective_overrides = copy.deepcopy(overrides or {})
        effective_accelerator_profile = accelerator_profile or capacity_profile
        v2_only = bool(effective_overrides) or (
            effective_accelerator_profile != capacity_profile
        )
        contract = VERIFY.expected_contract(
            project_id=PROJECT_ID,
            source_commit=SOURCE_COMMIT,
            capacity_profile=capacity_profile,
            accelerator_profile=accelerator_profile,
            floor_profile=floor_profile,
            overrides=effective_overrides,
        )
        action = {
            "create": ["create"],
            "noop": ["no-op"],
            "destroy": ["delete"],
        }[mode]
        side = "before" if mode == "destroy" else "after"
        changes: list[dict] = []
        for pool_id, pool in contract["pools"].items():
            changes.append(
                {
                    "address": f'nebius_mk8s_v1_node_group.gpu["{pool_id}"]',
                    "change": {
                        "actions": action,
                        side: {
                            "autoscaling": {
                                "min_node_count": pool["capacity"]["min_nodes"],
                                "max_node_count": pool["capacity"]["max_nodes"],
                            },
                            "template": {
                                "metadata": {
                                    "labels": {
                                        **pool["scheduling"]["stable_node_labels"],
                                        "lifecycle.fs2.nebius/run": "rtest01",
                                    }
                                },
                                "resources": {
                                    "platform": pool["provider"]["platform"],
                                    "preset": pool["provider"]["preset"],
                                },
                                "os": pool["provider"]["os"],
                                "gpu_settings": {
                                    "drivers_preset": pool["provider"]["driver"][
                                        "preset"
                                    ]
                                },
                            },
                        },
                    },
                }
            )
        changes.append(
            {
                "address": "terraform_data.target_contract",
                "change": {
                    "actions": action,
                    side: {
                        "input": {
                            "accelerator_pool_contract": contract,
                            "accelerator_pool_contract_sha256": (
                                VERIFY.canonical_sha256(contract)
                            ),
                            "infrastructure_contract": (
                                None
                                if v2_only
                                else {"schema": "legacy-test-fixture/v1"}
                            ),
                            "infrastructure_contract_sha256": (
                                None if v2_only else "b" * 64
                            ),
                        }
                    },
                },
            }
        )
        document = {
            "variables": {
                "project_id": {"value": PROJECT_ID},
                "source_commit": {"value": SOURCE_COMMIT},
                "capacity_profile": {"value": capacity_profile},
                "accelerator_pool_profile": {"value": accelerator_profile},
                "gpu_floor_profile": {"value": floor_profile},
                "accelerator_pool_capacity_overrides": {"value": effective_overrides},
            },
            "resource_changes": changes,
        }
        if mode != "destroy":
            document["planned_values"] = {
                "outputs": {
                    "accelerator_pool_contract": {"value": contract},
                    "accelerator_pool_contract_sha256": {
                        "value": VERIFY.canonical_sha256(contract)
                    },
                    "infrastructure_contract": {
                        "value": (
                            None
                            if v2_only
                            else {"schema": "legacy-test-fixture/v1"}
                        )
                    },
                }
            }
        return document

    def errors(self, document: dict, *, mode: str = "noop") -> list[str]:
        return VERIFY.validate_plan(
            document,
            mode=mode,
            expected_project_id=PROJECT_ID,
            expected_source_commit=SOURCE_COMMIT,
        )

    def test_default_and_capacity_only_override_pass_all_plan_modes(self) -> None:
        override = {
            "nebius-b300-preemptible-1x": {"min_nodes": 1, "max_nodes": 1},
            "nebius-b300-preemptible-8x": {"min_nodes": 0, "max_nodes": 1},
        }
        for mode in ("create", "noop", "destroy"):
            with self.subTest(mode=mode, variant="default"):
                self.assertEqual(self.errors(self.fixture(mode=mode), mode=mode), [])
            with self.subTest(mode=mode, variant="override"):
                self.assertEqual(
                    self.errors(self.fixture(mode=mode, overrides=override), mode=mode),
                    [],
                )

    def test_accelerator_profile_can_be_selected_independently(self) -> None:
        document = self.fixture(
            capacity_profile="minimal",
            accelerator_profile="full_catalog",
        )
        self.assertEqual(self.errors(document), [])
        contract = document["planned_values"]["outputs"][
            "accelerator_pool_contract"
        ]["value"]
        self.assertEqual(contract["profile"], "full_catalog")
        self.assertEqual(
            contract["pools"]["nebius-b300-preemptible-1x"]["capacity"][
                "max_nodes"
            ],
            6,
        )
        self.assertIsNone(
            document["planned_values"]["outputs"]["infrastructure_contract"][
                "value"
            ]
        )

    def test_unknown_pool_and_immutable_field_fail_closed(self) -> None:
        cases = (
            (
                {"unknown-pool": {"min_nodes": 0, "max_nodes": 0}},
                "unknown pool ID",
            ),
            (
                {
                    "nebius-b300-preemptible-1x": {
                        "min_nodes": 0,
                        "max_nodes": 1,
                        "gpus_per_node": 8,
                    }
                },
                "exactly min_nodes/max_nodes",
            ),
            (
                {
                    "nebius-b300-preemptible-1x": {
                        "min_nodes": 0,
                        "max_nodes": 1,
                        "platform": "gpu-h100-sxm",
                    }
                },
                "exactly min_nodes/max_nodes",
            ),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                document = self.fixture()
                document["variables"]["accelerator_pool_capacity_overrides"][
                    "value"
                ] = overrides
                self.assertTrue(
                    any(expected in error for error in self.errors(document))
                )

    def test_bounds_above_reviewed_profile_fail_closed(self) -> None:
        document = self.fixture()
        document["variables"]["accelerator_pool_capacity_overrides"]["value"] = {
            "nebius-b300-preemptible-1x": {"min_nodes": 0, "max_nodes": 2}
        }
        self.assertTrue(
            any("0 <= min <= max <= 1" in error for error in self.errors(document))
        )

    def test_provider_fact_and_contract_digest_drift_fail_closed(self) -> None:
        document = self.fixture()
        address = 'nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-1x"]'
        change = next(
            item for item in document["resource_changes"] if item["address"] == address
        )
        change["change"]["after"]["template"]["resources"]["platform"] = "gpu-h100-sxm"
        document["planned_values"]["outputs"]["accelerator_pool_contract_sha256"][
            "value"
        ] = "0" * 64
        errors = self.errors(document)
        self.assertTrue(
            any("platform differs from profile" in error for error in errors)
        )
        self.assertTrue(any("output differs" in error for error in errors))

    def test_legacy_and_extra_generic_addresses_fail_closed(self) -> None:
        for address, expected in (
            (
                "nebius_mk8s_v1_node_group.gpu_b300_1x",
                "legacy singleton",
            ),
            (
                'nebius_mk8s_v1_node_group.gpu["unexpected-pool"]',
                "address set differs",
            ),
        ):
            with self.subTest(address=address):
                document = self.fixture()
                document["resource_changes"].append(
                    {
                        "address": address,
                        "change": {"actions": ["no-op"], "after": {}},
                    }
                )
                self.assertTrue(
                    any(expected in error for error in self.errors(document))
                )

    def test_override_requires_legacy_contract_to_be_null(self) -> None:
        document = self.fixture(
            overrides={"nebius-b300-preemptible-1x": {"min_nodes": 0, "max_nodes": 1}}
        )
        document["planned_values"]["outputs"]["infrastructure_contract"]["value"] = {
            "schema": "stale/v1"
        }
        errors = self.errors(document)
        self.assertTrue(
            any(
                "legacy infrastructure_contract must be null" in error
                for error in errors
            )
        )


if __name__ == "__main__":
    unittest.main()
