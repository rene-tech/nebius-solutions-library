"""Offline tests for supplemental cohort observation projections."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "remediation_export", Path(__file__).with_name("export_observation.py")
)
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def sample(available=None):
    result = {
        "started_at": "2026-09-07T16:00:00Z",
        "pods": {"data": [{"name": "exporter-pod", "node": "gpu-node", "labels": {}}]},
        "qwen_model_deployment": {
            "data": {
                "metadata": {
                    "annotations": {"inference.fs2.nebius.ai/spec-digest": "desired-v1"}
                },
                "status": {"phase": "Ready"},
            }
        },
    }
    if available is not None:
        result["prom_node_root_disk_available_bytes"] = {
            "data": {
                "data": {
                    "result": [
                        {
                            "metric": {
                                "pod": "exporter-pod",
                                "instance": "10.0.0.1:9100",
                            },
                            "value": [1, str(available)],
                        }
                    ]
                }
            }
        }
    return result


def test_missing_disk_metric_is_not_zero_free_space():
    result = export.extra_observations([sample()])
    assert result["root_disk_available_bytes_by_node"] == []
    assert result["qwen_model_phase_counts"] == {"Ready": 1}


def test_disk_headroom_is_labeled_by_actual_node_and_preserves_minimum():
    result = export.extra_observations([sample(200), sample(100), sample(150)])
    state = result["root_disk_available_bytes_by_node"][0]
    assert state["node"] == "gpu-node"
    assert state["first_available_bytes"] == 200
    assert state["minimum_available_bytes"] == 100
    assert state["last_available_bytes"] == 150
    assert state["samples"] == 3


def test_sampled_burst_keeps_readiness_and_model_phase_distinct():
    row = sample()
    row["pods"]["data"].append(
        {
            "name": "qwen-burst",
            "uid": "burst-uid",
            "node": "gpu-node",
            "phase": "Pending",
            "labels": {
                "fs2-serve.nebius.ai/model-deployment": "qwen3-8b",
                "fs2-serve.nebius.ai/workload-role": "burst-pool",
            },
            "conditions": [
                {"type": "Ready", "status": "False"},
                {"type": "Initialized", "status": "False"},
            ],
        }
    )
    state = export.extra_observations([row])["qwen_burst_pods"][0]["states"][0]
    assert state["ready"] == "False" and state["initialized"] == "False"
    assert state["qwen_model_phase"] == "Ready"


def test_kueue_allowlist_uses_frozen_pod_operation_labels():
    def workload(name, operation):
        return {
            "metadata": {"name": name},
            "spec": {"podSets": [{"template": {"metadata": {"labels": {
                "fs2.nebius.ai/operation-id": operation,
            }}}}]},
        }

    rows = [{"workloads": {"data": {"items": [
        workload("campaign-job", "campaign-op"),
        workload("co-tenant-job", "another-op"),
    ]}}}]
    assert export.campaign_kueue_names(rows, {"campaign-op"}) == {"campaign-job"}


def test_per_pod_metric_namespace_coverage_is_observed_not_assumed():
    row = sample()
    row["prom_pod_cpu_cores"] = {"data": {"data": {"result": [
        {"metric": {"namespace": "fs2-models"}, "value": [1, "0.5"]},
    ]}}}
    row["prom_pod_memory_bytes"] = {"data": {"data": {"result": []}}}
    result = export.extra_observations([row])
    assert result["per_pod_metric_observed_namespaces"] == {
        "pod_cpu_cores": ["fs2-models"], "pod_memory_bytes": [],
    }


def test_next_sampler_queries_academic_pod_cpu_and_memory():
    sampler_spec = importlib.util.spec_from_file_location(
        "next_trial_sampler", Path(__file__).with_name("sample_cluster.py")
    )
    sampler = importlib.util.module_from_spec(sampler_spec)
    sampler_spec.loader.exec_module(sampler)
    for name in ("pod_cpu_cores", "pod_memory_bytes"):
        assert 'namespace=~"fs2-system|fs2-models|fs2-academic-poc"' in sampler.QUERIES[name]
