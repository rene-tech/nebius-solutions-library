from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative: str) -> dict:
    value = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{relative} must contain one YAML object")
    return value


class SchedulingObservabilityContractTests(unittest.TestCase):
    def test_kueue_controller_enables_both_fair_sharing_layers(self) -> None:
        values = load_yaml("stages/foundation/values/kueue.yaml")
        manager = yaml.safe_load(
            values["managerConfig"]["controllerManagerConfigYaml"]
        )

        self.assertEqual(
            manager["fairSharing"]["preemptionStrategies"],
            ["LessThanOrEqualToFinalShare", "LessThanInitialShare"],
        )
        self.assertEqual(
            manager["admissionFairSharing"],
            {"usageHalfLifeTime": "168h", "usageSamplingInterval": "5m"},
        )

    def test_queue_renderer_is_pool_driven_and_retains_stable_addresses(self) -> None:
        module_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "modules/kueue-scheduling").glob("*.tf"))
        )
        queue_source = (ROOT / "stages/workloads/queue.tf").read_text(
            encoding="utf-8"
        )

        self.assertNotRegex(module_source.lower(), r"\b(?:b300|h100|nvidia)\b")
        self.assertIn('resource "kubernetes_manifest" "async_cluster_queue"', queue_source)
        self.assertIn('resource "kubernetes_manifest" "model_local_queue"', queue_source)
        self.assertIn('module "kueue_scheduling"', queue_source)
        self.assertIn("pool.node.gpus_per_node * pool.capacity.max_nodes", queue_source)

    def test_dcgm_keeps_exact_gpu_and_pod_identity_at_five_seconds(self) -> None:
        base = load_yaml("stages/workloads/values/dcgm-exporter.yaml")
        cadence = load_yaml(
            "stages/workloads/values/dcgm-cadence-profiles.yaml"
        )
        standard = cadence["profiles"]["standard"]
        cold = cadence["profiles"]["coldStartCampaign"]

        self.assertIs(base["serviceMonitor"]["honorLabels"], True)
        regex = base["serviceMonitor"]["metricRelabelings"][0]["regex"].lower()
        self.assertNotIn("uuid", regex)
        self.assertNotIn("pod_uid", regex)
        self.assertEqual(standard["attributionMetricCollectionInterval"], "5s")
        self.assertEqual(standard["helmValues"]["arguments"], ["--collect-interval", "5000"])
        self.assertEqual(standard["helmValues"]["serviceMonitor"]["interval"], "5s")
        self.assertEqual(standard["minimumNominalWindowSeconds"], 10)
        self.assertEqual(cold["helmValues"]["serviceMonitor"]["interval"], "1s")

        for profile in (standard, cold):
            profile_text = str(
                profile["helmValues"]["serviceMonitor"]["metricRelabelings"]
            ).lower()
            self.assertNotIn("uuid", profile_text)
            self.assertNotIn("pod_uid", profile_text)

    def test_raw_traces_and_kubernetes_events_have_one_collector_path(self) -> None:
        gateway = load_yaml("stages/foundation/values/otel-gateway.yaml")
        cluster = load_yaml("stages/foundation/values/otel-cluster.yaml")
        node = load_yaml("stages/foundation/values/otel-node.yaml")

        self.assertEqual(
            gateway["config"]["exporters"]["otlp/tempo"]["endpoint"],
            "fs2-tempo.fs2-observability.svc.cluster.local:4317",
        )
        self.assertEqual(
            gateway["config"]["service"]["pipelines"]["traces"]["exporters"],
            ["spanmetrics", "otlp/tempo"],
        )
        deleted = {
            item["key"]
            for item in gateway["config"]["processors"]["resource/sanitize"][
                "attributes"
            ]
        }
        self.assertIn("http.request.header.authorization", deleted)
        self.assertNotIn("tenant.id", deleted)
        self.assertNotIn("api.key.id", deleted)

        self.assertEqual(cluster["mode"], "deployment")
        self.assertEqual(cluster["replicaCount"], 1)
        self.assertIs(cluster["presets"]["kubernetesEvents"]["enabled"], True)
        self.assertEqual(
            cluster["config"]["exporters"]["otlp"]["endpoint"],
            "fs2-otel-gateway.fs2-observability.svc.cluster.local:4317",
        )
        extracted = {
            item["key"]: item["tag_name"]
            for item in node["config"]["processors"]["k8sattributes"]["extract"][
                "labels"
            ]
        }
        self.assertEqual(extracted["fs2.nebius.ai/model-id"], "fs2.model.id")
        self.assertEqual(
            extracted["fs2.nebius.ai/workload-id"], "fs2.workload.id"
        )
        self.assertEqual(extracted["fs2.nebius.ai/attempt-id"], "fs2.attempt.id")

    def test_tempo_is_persistent_pinned_and_grafana_discoverable(self) -> None:
        tempo = load_yaml("stages/foundation/values/tempo.yaml")
        foundation = (
            ROOT / "stages/foundation/observability_backends.tf"
        ).read_text(encoding="utf-8")
        workload_network = (
            ROOT / "stages/workloads/observability.tf"
        ).read_text(encoding="utf-8")
        control_plane = (ROOT / "stages/workloads/control_plane.tf").read_text(
            encoding="utf-8"
        )

        self.assertEqual(tempo["tempo"]["tag"], "2.9.0")
        self.assertEqual(tempo["tempo"]["retention"], "168h")
        self.assertIs(tempo["persistence"]["enabled"], True)
        self.assertEqual(tempo["persistence"]["size"], "50Gi")
        self.assertEqual(
            tempo["persistence"]["storageClassName"], "compute-csi-default-sc"
        )
        self.assertRegex(foundation, re.compile(r'version\s+=\s+local\.chart_versions\.tempo'))
        self.assertIn('grafana_datasource = "1"', foundation)
        self.assertIn('port     = "3200"', workload_network)
        self.assertRegex(control_plane, re.compile(r"tempo\s+=\s+true"))


if __name__ == "__main__":
    unittest.main()
