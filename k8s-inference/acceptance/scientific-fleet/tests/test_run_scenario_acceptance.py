from __future__ import annotations

import json
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import run_scenario_acceptance as scenario_runner  # noqa: E402 - standalone script directory


class ScenarioTests(unittest.TestCase):
    def test_result_bytes_must_match_both_manifest_and_gateway_digest(self) -> None:
        body = b"scientific output\n"
        digest = hashlib.sha256(body).hexdigest()
        pointer = {"artifact_id": "fixture", "size_bytes": len(body), "sha256": digest}
        response = scenario_runner.public.HttpResponse(
            200, {"x-fs2-artifact-sha256": digest}, body
        )
        client = SimpleNamespace(request=lambda *args: response)
        self.assertEqual(scenario_runner.verify_download(client, pointer), body)
        with self.assertRaisesRegex(
            scenario_runner.public.AcceptanceError,
            "downloaded_artifact_identity_mismatch",
        ):
            scenario_runner.verify_download(client, {**pointer, "sha256": "0" * 64})

    def test_all_customer_inputs_keep_valid_manifest_digest_chains(self) -> None:
        root = HERE.parents[1]
        fragments = {
            item.model_id: item.path
            for item in scenario_runner.fleet.discover_inputs(root)
        }
        scenarios = json.loads(
            (HERE / "scenarios/customer-readiness.json").read_bytes()
        )
        changed_inputs = 0
        for scenario in scenarios:
            config = scenario_runner.public.RunConfig(
                endpoint="https://example.invalid",
                repository_root=root,
                activation_fragment=fragments[scenario["model_id"]],
                receipt_path=Path("unused.json"),
                run_id="unit-test",
            )
            original = scenario_runner.public._activation(config)
            _, request, declarations, _ = scenario_runner.prepare_scenario(
                config, scenario
            )
            root_input = next(
                item for item in declarations if item.role == "request-input-manifest"
            )
            scenario_runner.public._verify_declared_bytes(
                request["input_manifest"], root_input.data
            )
            if (
                request["input_manifest"]["media_type"]
                == scenario_runner.public.MANIFEST_MEDIA_TYPE
            ):
                artifacts = [
                    item for item in declarations if item.role == "manifest-artifact"
                ]
                for entry, declared in scenario_runner.public._entry_inputs(
                    json.loads(root_input.data), artifacts
                ):
                    scenario_runner.public._verify_declared_bytes(
                        entry["artifact"], declared.data
                    )
            changed_inputs += (
                request["input_manifest"]["sha256"]
                != original[1]["input_manifest"]["sha256"]
            )
        self.assertEqual(changed_inputs, 5)

    def test_unknown_input_is_rejected_before_upload(self) -> None:
        root = HERE.parents[1]
        fragment = next(
            item.path
            for item in scenario_runner.fleet.discover_inputs(root)
            if item.model_id == "esmfold2"
        )
        config = scenario_runner.public.RunConfig(
            endpoint="https://example.invalid",
            repository_root=root,
            activation_fragment=fragment,
            receipt_path=Path("unused.json"),
            run_id="unit-test",
        )
        with self.assertRaisesRegex(
            scenario_runner.public.AcceptanceError, "scenario_artifact_unknown"
        ):
            scenario_runner.prepare_scenario(
                config, {"model_id": "esmfold2", "artifact_json": {"wrong": {}}}
            )

    def test_overlap_counts_gpu_units_and_handles_simultaneous_release(self) -> None:
        def attempt(start: int, end: int, gpus: int) -> dict:
            return {
                "scheduling_admission": {
                    "accelerator_count": gpus,
                    "admitted_at": f"2026-09-06T10:00:{start:02d}Z",
                },
                "completed_at": f"2026-09-06T10:00:{end:02d}Z",
            }

        rows = [
            {
                "attempts": [
                    attempt(1, 3, 2),
                    attempt(2, 3, 1),
                    attempt(3, 5, 2),
                    attempt(2, 4, 0),
                ]
            }
        ]
        self.assertEqual(scenario_runner.gpu_overlap(rows), 3)


if __name__ == "__main__":
    unittest.main()
