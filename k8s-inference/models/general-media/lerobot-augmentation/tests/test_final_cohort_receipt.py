"""The additive final receipt must not broaden the measured qualification."""

import json
from datetime import datetime
from pathlib import Path


def receipt():
    path = Path(__file__).parents[1] / "activation/qualification/final-cohorts-r151.json"
    return json.loads(path.read_text())


def test_exact_five_positive_workflows_six_full_reader_datasets():
    value = receipt()
    assert value["release"]["helm_revision"] == 151
    assert value["release"]["worker_image"].endswith(
        "@sha256:1eeb26e239243c3c088b1ce9cb184f6cde9639100a6583d0d39b45d787cd85a1"
    )
    assert len(value["cases"]) == 5
    validations = [v for case in value["cases"] for v in case["validations"]]
    children = [c for case in value["cases"] for c in case["children"]]
    assert len(validations) == 6
    assert sum(v["decoded_camera_frames"] for v in validations) == 768
    assert sum(v["nonvideo_values_compared"] for v in validations) == 13824
    assert all(v["nonvideo_values_exact"] for v in validations)
    assert sum(c["operation"] == "generate-media" for c in children) == 12
    assert sum(c["operation"] == "upload" for c in children) == 11
    assert all(c["status"] == "succeeded" for c in children)
    for case in value["cases"]:
        assert case["max_concurrency"] == 1
        assert case["inflight_replay_verified"] and case["terminal_replay_verified"]
        assert case["client_end_to_end_seconds"] > case["server_parent_seconds"]
        for child in case["children"]:
            assert child["same_token"] and child["same_principal"] and child["same_tenant"]


def test_cancel_proves_actual_running_child_and_preserves_prior_harness_failures():
    value = receipt()
    probes = value["base_probes"]
    assert probes["invalid_selection"]["generation_children"] == 0
    assert probes["cancellation"]["generation_children"] == 0
    assert probes["concurrency"]["body"]["error"]["type"] == "concurrency_exceeded"
    history = value["additional_cancellation_history"]
    assert len(history) == 3
    assert history[0]["status"] == "succeeded"
    assert history[1]["status"] == "cancelled"
    assert all(child["status"] == "succeeded" for child in history[1]["children"])
    final = history[2]
    assert final["helper_outcome"] == "operator_assisted_active_child_parent_cancellation_passed"
    assert final["public_child_before_cancel_status"] == "running"
    assert final["operator_assisted_child_discovery"]
    assert not final["public_child_discovery_verified"]
    assert not final["gpu_kernel_interruption_verified"]
    children = [c for c in final["children"] if c["operation"] == "generate-media"]
    assert len(children) == 1 and children[0]["status"] == "cancelled"
    child = children[0]
    assert (
        datetime.fromisoformat(child["started_at"])
        < datetime.fromisoformat(final["cancel_requested_at"])
        < datetime.fromisoformat(child["completed_at"])
    )


def test_qualification_boundaries_and_natural_cleanup_are_explicit():
    value = receipt()
    for key in (
        "customer_ready",
        "physical_alignment_verified",
        "semantic_intent_verified",
        "actual_librechat_verified",
        "packaged_qualification_profile_modified_by_this_report",
    ):
        assert value[key] is False
    assert value["summary"]["fully_attributed_generation_children"] == 3
    assert value["summary"]["unavailable_exact_gpu_identity_children"] == 9
    cleanup = value["cleanup"]
    assert cleanup["read_only"] and not cleanup["manual_scaling_or_deletion"]
    assert cleanup["exact_canary_active_operations"]["total_active"] == 0
    assert len(cleanup["cpu_workloads"]) == 10
    assert cleanup["all_scoped_cpu_jobs_and_pods_absent"]
    assert cleanup["native_cosmos"]["desired_replicas"] == 0
    assert cleanup["native_cosmos"]["observed_replicas"] == 0
    assert cleanup["native_cosmos"]["pods"] == []
