from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalog" / "gpu-platforms.json"
SCHEMA_PATH = ROOT / "catalog" / "gpu-platforms.schema.json"

EXPECTED_PLATFORMS = {
    "gpu-l40s-a": {
        "host_architecture": "amd64",
        "regions": {"eu-north1"},
        "presets": {
            "1gpu-8vcpu-32gb",
            "1gpu-16vcpu-64gb",
            "1gpu-24vcpu-96gb",
            "1gpu-32vcpu-128gb",
            "1gpu-40vcpu-160gb",
        },
        "preemptible": True,
        "mig": "unsupported",
    },
    "gpu-l40s-d": {
        "host_architecture": "amd64",
        "regions": {"eu-north1"},
        "presets": {
            "1gpu-16vcpu-96gb",
            "1gpu-32vcpu-192gb",
            "1gpu-48vcpu-288gb",
            "2gpu-64vcpu-384gb",
            "2gpu-96vcpu-576gb",
            "4gpu-128vcpu-768gb",
            "4gpu-192vcpu-1152gb",
        },
        "preemptible": True,
        "mig": "unsupported",
    },
    "gpu-h100-sxm": {
        "host_architecture": "amd64",
        "regions": {"eu-north1"},
        "presets": {"1gpu-16vcpu-200gb", "8gpu-128vcpu-1600gb"},
        "preemptible": True,
        "mig": "supported-manual-operator",
    },
    "gpu-h200-sxm": {
        "host_architecture": "amd64",
        "regions": {"eu-north1", "eu-north2", "eu-west1", "us-central1"},
        "presets": {"1gpu-16vcpu-200gb", "8gpu-128vcpu-1600gb"},
        "preemptible": True,
        "mig": "supported-manual-operator",
    },
    "gpu-b200-sxm": {
        "host_architecture": "amd64",
        "regions": {"us-central1"},
        "presets": {"1gpu-20vcpu-224gb", "8gpu-160vcpu-1792gb"},
        "preemptible": True,
        "mig": "supported-manual-operator",
    },
    "gpu-b200-sxm-a": {
        "host_architecture": "amd64",
        "regions": {"me-west1"},
        "presets": {"1gpu-20vcpu-224gb", "8gpu-160vcpu-1792gb"},
        "preemptible": True,
        "mig": "supported-manual-operator",
    },
    "gpu-b300-sxm": {
        "host_architecture": "amd64",
        "regions": {"eu-west2", "uk-south1", "us-north1"},
        "presets": {"1gpu-24vcpu-346gb", "8gpu-192vcpu-2768gb"},
        "preemptible": True,
        "mig": "declared-unverified",
    },
    "gpu-rtx6000": {
        "host_architecture": "amd64",
        "regions": {"us-central1"},
        "presets": {"1gpu-24vcpu-218gb", "8gpu-192vcpu-1744gb"},
        "preemptible": True,
        "mig": "supported-manual-operator",
    },
    "gpu-rtx6000-a": {
        "host_architecture": "amd64",
        "regions": {"eu-south1"},
        "presets": {"1gpu-24vcpu-218gb", "8gpu-192vcpu-1744gb"},
        "preemptible": False,
        "mig": "supported-manual-operator",
    },
    "gpu-gb300": {
        "host_architecture": "arm64",
        "regions": {"eu-north1"},
        "presets": {"4gpu-112vcpu-800gb"},
        "preemptible": False,
        "mig": "blocked-current-solution",
    },
}


class GpuPlatformCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_schema_is_valid_and_catalog_conforms(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        Draft202012Validator(
            self.schema,
            format_checker=FormatChecker(),
        ).validate(self.catalog)

    def test_snapshot_contains_exactly_the_ten_current_platforms(self) -> None:
        platforms = self.catalog["platforms"]
        self.assertEqual(set(platforms), set(EXPECTED_PLATFORMS))

        preset_pattern = re.compile(
            r"^(?P<gpus>[1-9][0-9]*)gpu-"
            r"(?P<vcpu>[1-9][0-9]*)vcpu-"
            r"(?P<memory>[1-9][0-9]*)gb$"
        )
        source_ids = set(self.catalog["source_metadata"])

        for platform_id, expected in EXPECTED_PLATFORMS.items():
            with self.subTest(platform=platform_id):
                platform = platforms[platform_id]
                self.assertEqual(platform["platform_id"], platform_id)
                self.assertEqual(
                    platform["host"]["architecture"],
                    expected["host_architecture"],
                )
                self.assertEqual(set(platform["regions"]), expected["regions"])
                self.assertTrue(platform["capacity"]["regular_supported"])
                self.assertEqual(
                    platform["capacity"]["preemptible_supported"],
                    expected["preemptible"],
                )
                self.assertEqual(platform["mig"]["status"], expected["mig"])
                self.assertFalse(
                    platform["mig"]["managed_driver_image_compatible"]
                )
                self.assertTrue(
                    platform["driverfull"]["compatibility_must_be_queried"]
                )
                self.assertTrue(set(platform["source_ids"]).issubset(source_ids))

                presets = {item["preset_id"]: item for item in platform["presets"]}
                self.assertEqual(set(presets), expected["presets"])
                for preset_id, preset in presets.items():
                    match = preset_pattern.fullmatch(preset_id)
                    self.assertIsNotNone(match)
                    assert match is not None
                    self.assertEqual(preset["gpu_count"], int(match["gpus"]))
                    self.assertEqual(preset["vcpu_count"], int(match["vcpu"]))
                    self.assertEqual(preset["memory_gib"], int(match["memory"]))

    def test_topology_flags_match_preset_capabilities(self) -> None:
        for platform_id, platform in self.catalog["platforms"].items():
            with self.subTest(platform=platform_id):
                compatible = {
                    preset["preset_id"]
                    for preset in platform["presets"]
                    if preset["gpu_cluster_compatible"]
                }
                topology = platform["topology"]
                self.assertEqual(set(topology["gpu_cluster_presets"]), compatible)
                self.assertEqual(topology["gpu_cluster_supported"], bool(compatible))

        gb300 = self.catalog["platforms"]["gpu-gb300"]["topology"]
        self.assertEqual(gb300["deployment_class"], "gb300-nvlink-rack")
        self.assertTrue(gb300["nvlink_instance_group_required"])
        self.assertEqual(gb300["nodes_per_nvlink_group"], 18)

    def test_unknown_future_platform_and_preset_are_passthrough(self) -> None:
        contract = self.catalog["selection_contract"]
        self.assertEqual(contract["catalog_role"], "informational-snapshot")
        self.assertFalse(contract["catalog_membership_required"])
        self.assertFalse(contract["terraform_allowlist"])
        self.assertEqual(contract["unknown_platform_policy"], "passthrough")
        self.assertEqual(contract["unknown_preset_policy"], "passthrough")
        self.assertTrue(contract["live_validation_required"])

        selection_schema = self.schema["$defs"]["platformSelection"]
        serialized_schema = json.dumps(selection_schema, sort_keys=True)
        self.assertNotIn('"enum"', serialized_schema)
        self.assertNotIn('"const"', serialized_schema)
        Draft202012Validator(selection_schema).validate(
            {
                "platform": "gpu-future-architecture-v1",
                "preset": "16gpu-512vcpu-8192gb",
                "future_provider_field": {"is_also_passthrough": True},
            }
        )

    def test_sources_are_public_official_references(self) -> None:
        for source_id, source in self.catalog["source_metadata"].items():
            with self.subTest(source=source_id):
                url = source["url"]
                self.assertTrue(
                    url.startswith("https://docs.nebius.com/")
                    or url.startswith(
                        "https://github.com/nebius/nebius-solutions-library/"
                    )
                    or url.startswith("https://docs.nvidia.com/")
                )


if __name__ == "__main__":
    unittest.main()
