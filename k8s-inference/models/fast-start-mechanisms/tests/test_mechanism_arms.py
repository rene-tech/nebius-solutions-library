"""The campaign arms must be the production mechanism render, not a copy."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "components/control-plane/src"))

from mechanism_arms import (  # noqa: E402
    ARMS,
    PROMOTION_ARMS,
    ArmError,
    load_contract,
    render_arm,
    target,
)
from run_mechanism_campaign import summarise  # noqa: E402


def _spec() -> dict:
    spec = target(load_contract(), "qwen3-8b")
    spec["node_name"] = "h100-node-a"
    return spec


class MechanismArmTests(unittest.TestCase):
    def test_every_arm_renders_and_only_the_mechanism_differs(self) -> None:
        spec = _spec()
        rendered = {arm: render_arm(spec, arm=arm, attempt=0, campaign_id="t") for arm in ARMS}

        # The control arm is the conventional render: retained payload mounted
        # read-only and a compile cache discarded with the Pod.
        volumes = {item["name"]: item for item in rendered["conventional"]["pod"]["spec"]["volumes"]}
        self.assertIn("emptyDir", volumes["runtime-cache"])
        self.assertEqual(rendered["conventional"]["pod"]["spec"]["containers"][0]["args"], list(spec["runtime_args"]))

        for arm, value in rendered.items():
            with self.subTest(arm=arm):
                self.assertEqual(value["pod"]["spec"]["nodeSelector"]["kubernetes.io/hostname"], "h100-node-a")
                # Every arm keeps the same model argv, so the runtime contract
                # identity is unchanged and the cohorts stay comparable.
                self.assertEqual(value["pod"]["spec"]["containers"][0]["args"][0], spec["payload_content_path"])
                self.assertTrue(value["mechanism_config_digest"].startswith("sha256:"))

    def test_each_mechanism_has_a_distinct_configuration_identity(self) -> None:
        spec = _spec()
        digests = {
            arm: render_arm(spec, arm=arm, attempt=0, campaign_id="t")["mechanism_config_digest"] for arm in ARMS
        }
        self.assertEqual(len(set(digests.values())), len(digests))

    def test_promotion_arms_declare_the_capacity_they_hold(self) -> None:
        spec = _spec()
        for arm in PROMOTION_ARMS:
            with self.subTest(arm=arm):
                value = render_arm(spec, arm=arm, attempt=0, campaign_id="t")
                self.assertTrue(value["promotion"])
                held = value["reserved_accelerators"] or value["reserved_host_memory_bytes"]
                self.assertTrue(held, "a promotion arm must state the capacity it pre-holds")

    def test_the_boltzgen_target_is_declared_but_not_runnable_yet(self) -> None:
        contract = load_contract()
        boltzgen = contract["targets"]["boltzgen"]
        self.assertEqual(boltzgen["state"], "pending-serving-runtime")
        self.assertTrue(boltzgen["blocker"])
        with self.assertRaises(ArmError):
            target(contract, "boltzgen")

    def test_the_contract_names_no_opaque_node(self) -> None:
        # The node is a run-time target passed with --node, so the checked-in
        # contract stays free of cluster-specific resource identities.
        self.assertNotIn("computeinstance-", json.dumps(load_contract()))

    def test_campaign_summary_and_published_evidence_never_grant_a_level(self) -> None:
        receipts = []
        for arm, seconds in (("conventional", 100.0), ("regional-cache", 60.0)):
            receipts.extend(
                {
                    "arm": arm,
                    "mechanism": arm,
                    "warmup": False,
                    "succeeded": True,
                    "model_start_seconds": seconds + index / 100,
                    "capacity_preheld": False,
                    "reserved_host_memory_bytes": None,
                    "reserved_accelerators": None,
                }
                for index in range(20)
            )

        summary = summarise(receipts)
        self.assertEqual(summary["arms"]["regional-cache"]["p95_saving_seconds_vs_conventional"], 40.0)
        self.assertTrue(summary["arms"]["regional-cache"]["meets_minimum_samples"])
        self.assertFalse(summary["arms"]["regional-cache"]["qualifies_a_level"])

        published = json.loads((HERE.parent / "evidence/qwen3-8b-r1/comparison.json").read_text(encoding="utf-8"))
        narrative = (HERE.parent / "evidence/qwen3-8b-r1/EVIDENCE.md").read_text(encoding="utf-8")
        self.assertEqual(set(published["arms"]), set(ARMS))
        for arm, result in published["arms"].items():
            with self.subTest(arm=arm):
                self.assertEqual(result["samples"], 20)
                self.assertEqual(result["failed"], 0)
                self.assertFalse(result["qualifies_a_level"])
                self.assertIn(f"`{arm}`", narrative)


if __name__ == "__main__":
    unittest.main()
