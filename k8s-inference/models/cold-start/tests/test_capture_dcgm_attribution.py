from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "capture_dcgm_attribution.py"
SPEC = importlib.util.spec_from_file_location("fs2_capture_dcgm", SCRIPT)
assert SPEC and SPEC.loader
DCGM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DCGM)


class DcgmAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.cadence_binding_path = Path(self._temporary.name) / "cadence.json"
        self._write_cadence_binding(self._cadence_binding())

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _cadence_binding() -> dict:
        cadence = DCGM._cadence_binding_module()
        profile, profile_digest = cadence._profile_contract(
            DCGM.CADENCE_PROFILE_PATH, True
        )
        monitor = profile["helmValues"]["serviceMonitor"]
        output = {
            "schema": "fs2-serve.nebius.ai/dcgm-attribution-terraform/v1",
            "campaign_enabled": True,
            "attribution_metric_collection_interval": profile[
                "attributionMetricCollectionInterval"
            ],
            "scrape_interval": monitor["interval"],
            "scrape_timeout": monitor["scrapeTimeout"],
            "campaign_metrics": list(DCGM.METRICS),
            "minimum_nominal_window_seconds": profile["minimumNominalWindowSeconds"],
            "missing_sample_policy": "FAIL_CLOSED_NO_ESTIMATE",
        }
        config_identity = {
            "namespace": "fs2-observability",
            "name": "fs2-dcgm-exporter-config",
            "uid": "config-uid-1",
            "resource_version": "101",
        }
        binding = {
            "schema": "fs2-serve.nebius.ai/dcgm-cadence-live-binding/v1",
            "source": {"commit": "1" * 40, "tree": "2" * 40},
            "terraform": {
                "saved_plan_sha256": "3" * 64,
                "cadence_profile_sha256": profile_digest,
                "dcgm_attribution_contract": output,
                "dcgm_attribution_contract_sha256": cadence.digest(output),
            },
            "observed": {
                "config_map": {
                    "identity": config_identity,
                    "data_sha256": hashlib.sha256(
                        profile["helmValues"]["config"]["data"].encode("utf-8")
                    ).hexdigest(),
                },
                "service_monitor": {
                    "identity": {
                        "namespace": "fs2-observability",
                        "name": "fs2-dcgm-exporter",
                        "uid": "monitor-uid-1",
                        "resource_version": "102",
                    },
                    "generation": 2,
                    "interval": monitor["interval"],
                    "scrape_timeout": monitor["scrapeTimeout"],
                    "metric_relabelings": monitor["metricRelabelings"],
                    "spec_sha256": "4" * 64,
                },
                "daemon_set": {
                    "identity": {
                        "namespace": "fs2-observability",
                        "name": "fs2-dcgm-exporter",
                        "uid": "daemonset-uid-1",
                        "resource_version": "103",
                    },
                    "generation": 3,
                    "observed_generation": 3,
                    "desired_number_scheduled": 1,
                    "updated_number_scheduled": 1,
                    "number_ready": 1,
                    "exporter_image": cadence.EXPORTER_IMAGE,
                    "config_map_name": config_identity["name"],
                    "config_map_uid": config_identity["uid"],
                    "config_map_resource_version": config_identity["resource_version"],
                    "pod_template_sha256": "5" * 64,
                    "ready_pod_uids": ["exporter-pod-uid-1"],
                },
            },
            "captured_at": "2026-08-28T11:59:59Z",
        }
        binding["receipt_digest"] = cadence.digest(binding)
        return binding

    def _write_cadence_binding(self, binding: dict) -> None:
        cadence = DCGM._cadence_binding_module()
        self.cadence_binding_path.write_bytes(cadence.canonical_bytes(binding))
        os.chmod(self.cadence_binding_path, 0o600)

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            prometheus_url="http://127.0.0.1:19090",
            attempt_id="qwen3-8b-b300--fresh-node-zero-pod--r01",
            namespace="fs2-models",
            pod_uid=["pod-uid-1"],
            node_uid=["node-uid-1"],
            gpu_uuid=["GPU-test-1"],
            expected_gpu_count=1,
            attempt_t0="2026-08-28T12:00:05Z",
            attempt_t1="2026-08-28T12:00:55Z",
            start="2026-08-28T12:00:05Z",
            end="2026-08-28T12:00:55Z",
            cadence_binding_receipt=self.cadence_binding_path,
        )

    @staticmethod
    def _series(metric: str, *, namespace: str = "fs2-models") -> list[dict]:
        return [
            {
                "metric": {
                    "__name__": metric,
                    "namespace": namespace,
                    "pod": "qwen3-8b-b300-abc",
                    "pod_uid": "pod-uid-1",
                    "container": "vllm",
                    "UUID": "GPU-test-1",
                    "gpu": "0",
                },
                "values": [[1787918407, "10"], [1787918410, "30"]],
            }
        ]

    def _receipt(self) -> dict:
        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            return self._series(expression.split("{", 1)[0])

        with mock.patch.object(DCGM, "_query_range_vector", side_effect=query):
            return DCGM.build_receipt(self._args())

    @staticmethod
    def _reseal(receipt: dict) -> None:
        unsigned = dict(receipt)
        unsigned.pop("receipt_digest", None)
        receipt["receipt_digest"] = DCGM._digest(unsigned)

    def test_builds_private_summary_without_dropping_raw_attribution(self) -> None:
        queries: list[tuple[str, str]] = []

        def query(_origin: str, expression: str, evaluation_time: str) -> list[dict]:
            queries.append((expression, evaluation_time))
            metric = expression.split("{", 1)[0]
            return self._series(metric)

        with mock.patch.object(DCGM, "_query_range_vector", side_effect=query):
            receipt = DCGM.build_receipt(self._args())
        self.assertEqual(receipt["summary"]["attributed_device_count"], 1)
        self.assertEqual(receipt["summary"]["mean_gpu_utilization_percent"], 20)
        self.assertEqual(receipt["summary"]["peak_framebuffer_bytes"], 30 * 1024 * 1024)
        self.assertEqual(receipt["schema"], "fs2-serve.nebius.ai/dcgm-attribution/v2")
        self.assertEqual(receipt["query"]["endpoint"], "/api/v1/query")
        self.assertEqual(
            receipt["sampling_feasibility"]["minimum_nominal_proxy_offset_seconds"],
            2,
        )
        self.assertEqual(
            receipt["sampling_feasibility"]["proxy_classification"],
            "NOMINAL_SCRAPE_PROXY",
        )
        self.assertEqual(
            receipt["sampling_feasibility"]["hardware_source_timestamp_state"],
            "UNOBSERVED_HARDWARE_SOURCE_TIMESTAMP",
        )
        self.assertEqual(receipt["query"]["range_selector_milliseconds"], 50001)
        self.assertTrue(
            all(expression.endswith("[50001ms]") for expression, _time in queries)
        )
        self.assertEqual(
            {evaluation_time for _expression, evaluation_time in queries},
            {"2026-08-28T12:00:55Z"},
        )
        self.assertEqual(
            receipt["series"]["DCGM_FI_DEV_GPU_UTIL"][0]["labels"]["UUID"],
            "GPU-test-1",
        )
        self.assertEqual(
            receipt["series"]["DCGM_FI_DEV_GPU_UTIL"],
            receipt["raw_query_series"]["DCGM_FI_DEV_GPU_UTIL"],
        )

    def test_instant_range_vector_query_returns_raw_matrix_samples(self) -> None:
        payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": self._series("metric")},
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch.object(
            DCGM.urllib.request, "urlopen", return_value=response
        ) as get:
            result = DCGM._query_range_vector(
                "http://127.0.0.1:19090",
                'metric{pod_uid="pod-uid-1"}[50001ms]',
                "2026-08-28T12:00:55Z",
            )
        self.assertEqual(result, payload["data"]["result"])
        request = get.call_args.args[0]
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(parsed.path, "/api/v1/query")
        self.assertNotEqual(parsed.path, "/api/v1/query_range")
        parameters = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parameters["time"], ["2026-08-28T12:00:55Z"])
        self.assertEqual(parameters["query"], ['metric{pod_uid="pod-uid-1"}[50001ms]'])

    def test_rejects_non_matrix_prometheus_result(self) -> None:
        payload = {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with (
            mock.patch.object(DCGM.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(DCGM.DcgmError, "prometheus_query_unsuccessful"),
        ):
            DCGM._query_range_vector(
                "http://127.0.0.1:19090",
                'metric{pod_uid="pod-uid-1"}[50001ms]',
                "2026-08-28T12:00:55Z",
            )

    def test_rejects_cross_namespace_series(self) -> None:
        with self.assertRaisesRegex(DCGM.DcgmError, "dcgm_attribution_mismatch"):
            DCGM._series(
                "DCGM_FI_DEV_GPU_UTIL",
                self._series("DCGM_FI_DEV_GPU_UTIL", namespace="other"),
                "fs2-models",
                {"pod-uid-1"},
                1787918399.999,
                1787918400,
                1787918402,
                1787918460,
            )

    def test_rejects_samples_outside_the_raw_range_vector(self) -> None:
        values = self._series("DCGM_FI_DEV_GPU_UTIL")
        values[0]["values"][0][0] = 1787918399
        with self.assertRaisesRegex(DCGM.DcgmError, "dcgm_sample_outside_raw_query"):
            DCGM._series(
                "DCGM_FI_DEV_GPU_UTIL",
                values,
                "fs2-models",
                {"pod-uid-1"},
                1787918399.999,
                1787918400,
                1787918402,
                1787918460,
            )

    def test_stale_pre_t0_sample_is_preserved_but_never_aggregated(self) -> None:
        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            metric = expression.split("{", 1)[0]
            values = self._series(metric)
            values[0]["values"] = [
                [1787918404.9995, "999"],
                [1787918407, "10"],
                [1787918410, "30"],
            ]
            return values

        with mock.patch.object(DCGM, "_query_range_vector", side_effect=query):
            receipt = DCGM.build_receipt(self._args())
        self.assertEqual(receipt["summary"]["mean_gpu_utilization_percent"], 20)
        self.assertEqual(
            receipt["sampling_feasibility"]["excluded_pre_t0_sample_count"],
            {"DCGM_FI_DEV_FB_USED": 1, "DCGM_FI_DEV_GPU_UTIL": 1},
        )
        self.assertEqual(
            receipt["raw_query_series"]["DCGM_FI_DEV_GPU_UTIL"][0]["values"][0],
            [1787918404.9995, 999.0],
        )
        self.assertEqual(
            receipt["series"]["DCGM_FI_DEV_GPU_UTIL"][0]["values"][0],
            [1787918407.0, 10.0],
        )

    def test_early_in_window_cached_sample_is_never_aggregated(self) -> None:
        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            metric = expression.split("{", 1)[0]
            values = self._series(metric)
            values[0]["values"] = [
                [1787918406, "999"],
                [1787918407, "10"],
                [1787918410, "30"],
            ]
            return values

        with mock.patch.object(DCGM, "_query_range_vector", side_effect=query):
            receipt = DCGM.build_receipt(self._args())
        self.assertEqual(receipt["summary"]["mean_gpu_utilization_percent"], 20)
        self.assertEqual(
            receipt["sampling_feasibility"]["excluded_pre_nominal_proxy_sample_count"],
            {"DCGM_FI_DEV_FB_USED": 1, "DCGM_FI_DEV_GPU_UTIL": 1},
        )
        self.assertEqual(
            receipt["raw_query_series"]["DCGM_FI_DEV_GPU_UTIL"][0]["values"][0],
            [1787918406.0, 999.0],
        )
        self.assertEqual(
            receipt["series"]["DCGM_FI_DEV_GPU_UTIL"][0]["values"][0],
            [1787918407.0, 10.0],
        )

    def test_stale_pre_t0_samples_cannot_satisfy_required_evidence(self) -> None:
        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            metric = expression.split("{", 1)[0]
            values = self._series(metric)
            values[0]["values"] = [[1787918404.9995, "999"]]
            return values

        with (
            mock.patch.object(DCGM, "_query_range_vector", side_effect=query),
            self.assertRaisesRegex(DCGM.DcgmError, "dcgm_metric_samples_missing"),
        ):
            DCGM.build_receipt(self._args())

    def test_attempt_shorter_than_collection_plus_scrape_fails_closed(self) -> None:
        args = self._args()
        args.attempt_t1 = "2026-08-28T12:00:05.500Z"
        args.end = args.attempt_t1

        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            metric = expression.split("{", 1)[0]
            values = self._series(metric)
            values[0]["values"] = [[1787918405.25, "10"]]
            return values

        with (
            mock.patch.object(DCGM, "_query_range_vector", side_effect=query),
            self.assertRaisesRegex(
                DCGM.DcgmError, "attempt_shorter_than_collection_plus_scrape"
            ),
        ):
            DCGM.build_receipt(args)

    def test_validator_rejects_resealed_early_cached_sample_as_aggregated(
        self,
    ) -> None:
        receipt = self._receipt()
        for metric in DCGM.METRICS:
            early = [1787918406.0, 999.0]
            receipt["raw_query_series"][metric][0]["values"].insert(0, early)
            receipt["series"][metric][0]["values"].insert(0, early)
        self._reseal(receipt)
        with self.assertRaisesRegex(DCGM.DcgmError, "dcgm_series_invalid"):
            DCGM.validate_receipt(receipt)

    def test_post_floor_cached_value_remains_only_a_nominal_proxy(self) -> None:
        receipt = self._receipt()
        self.assertEqual(
            receipt["series"]["DCGM_FI_DEV_GPU_UTIL"][0]["values"][0],
            [1787918407.0, 10.0],
        )
        sampling = receipt["sampling_feasibility"]
        self.assertEqual(sampling["proxy_classification"], "NOMINAL_SCRAPE_PROXY")
        self.assertEqual(
            sampling["instrumentation_gap"], "DCGM_SOURCE_TIMESTAMP_UNOBSERVED"
        )
        self.assertFalse(sampling["hardware_sample_timestamp_available"])

        forged = copy.deepcopy(receipt)
        forged["sampling_feasibility"]["hardware_source_timestamp_state"] = (
            "OBSERVED_POST_T0_HARDWARE_TIMESTAMP"
        )
        self._reseal(forged)
        with self.assertRaisesRegex(DCGM.DcgmError, "dcgm_sampling_contract_invalid"):
            DCGM.validate_receipt(forged)

    def test_rejects_forged_cadence_provenance(self) -> None:
        cadence = DCGM._cadence_binding_module()
        binding = self._cadence_binding()
        binding["terraform"]["dcgm_attribution_contract"]["scrape_interval"] = "2s"
        binding["terraform"]["dcgm_attribution_contract_sha256"] = cadence.digest(
            binding["terraform"]["dcgm_attribution_contract"]
        )
        unsigned = dict(binding)
        unsigned.pop("receipt_digest")
        binding["receipt_digest"] = cadence.digest(unsigned)
        with self.assertRaisesRegex(DCGM.DcgmError, "cadence_terraform_output_invalid"):
            DCGM.validate_cadence_binding(binding)

        binding = self._cadence_binding()
        binding["observed"]["daemon_set"]["observed_generation"] = 2
        unsigned = dict(binding)
        unsigned.pop("receipt_digest")
        binding["receipt_digest"] = cadence.digest(unsigned)
        with self.assertRaisesRegex(
            DCGM.DcgmError, "cadence_daemon_set_rollout_invalid"
        ):
            DCGM.validate_cadence_binding(binding)

    def test_rejects_gpu_identity_mismatch(self) -> None:
        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            metric = expression.split("{", 1)[0]
            return self._series(metric)

        args = self._args()
        args.gpu_uuid = ["GPU-other"]
        with (
            mock.patch.object(DCGM, "_query_range_vector", side_effect=query),
            self.assertRaisesRegex(DCGM.DcgmError, "dcgm_gpu_identity_mismatch"),
        ):
            DCGM.build_receipt(args)

    def test_rejects_wider_or_narrower_than_attempt_query_windows(self) -> None:
        def query(_origin: str, expression: str, _evaluation_time: str) -> list[dict]:
            metric = expression.split("{", 1)[0]
            return self._series(metric)

        wider = self._args()
        wider.start = "2026-08-28T12:00:00Z"
        wider.end = "2026-08-28T12:01:00Z"
        narrower = self._args()
        narrower.start = "2026-08-28T12:00:10Z"
        narrower.end = "2026-08-28T12:00:50Z"
        for args in (wider, narrower):
            with (
                self.subTest(start=args.start, end=args.end),
                mock.patch.object(DCGM, "_query_range_vector", side_effect=query),
                self.assertRaisesRegex(
                    DCGM.DcgmError, "query_window_not_exact_attempt"
                ),
            ):
                DCGM.build_receipt(args)

    def test_prometheus_must_be_local_port_forward(self) -> None:
        for value in (
            "https://127.0.0.1:9090",
            "http://prometheus.fs2-observability:9090",
            "http://127.0.0.1",
        ):
            with self.subTest(value=value), self.assertRaises(DCGM.DcgmError):
                DCGM._prometheus_origin(value)

    def test_validator_rejects_resealed_query_timestamp_and_summary_drift(self) -> None:
        DCGM.validate_receipt(self._receipt())
        mutations = {
            "query": lambda value: value["query"].__setitem__(
                "endpoint", "/api/v1/query_range"
            ),
            "raw-timestamp": lambda value: value["raw_query_series"][
                "DCGM_FI_DEV_GPU_UTIL"
            ][0]["values"][0].__setitem__(0, 1787918404.9995),
            "summary": lambda value: value["summary"].__setitem__(
                "mean_gpu_utilization_percent", 99
            ),
            "collection-cadence": lambda value: value[
                "sampling_feasibility"
            ].__setitem__("terraform_bound_collection_interval_seconds", 30),
        }
        for name, mutate in mutations.items():
            receipt = copy.deepcopy(self._receipt())
            mutate(receipt)
            self._reseal(receipt)
            with self.subTest(name=name), self.assertRaises(DCGM.DcgmError):
                DCGM.validate_receipt(receipt)

    def test_validator_rejects_digest_drift_and_v1(self) -> None:
        receipt = self._receipt()
        receipt["query"]["endpoint"] = "/api/v1/query_range"
        with self.assertRaisesRegex(DCGM.DcgmError, "dcgm_receipt_digest_mismatch"):
            DCGM.validate_receipt(receipt)

        receipt = self._receipt()
        receipt["schema"] = "fs2-serve.nebius.ai/dcgm-attribution/v1"
        self._reseal(receipt)
        with self.assertRaisesRegex(DCGM.DcgmError, "dcgm_receipt_schema_invalid"):
            DCGM.validate_receipt(receipt)

    def test_validator_rejects_resealed_stale_only_series(self) -> None:
        receipt = self._receipt()
        for metric in DCGM.METRICS:
            stale = copy.deepcopy(receipt["raw_query_series"][metric][0])
            stale["values"] = [[1787918404.9995, 999.0]]
            receipt["raw_query_series"][metric] = [stale]
            receipt["series"][metric] = []
            receipt["sampling_feasibility"]["excluded_pre_t0_sample_count"][metric] = 1
        receipt["summary"] = {
            "attributed_device_count": 0,
            "gpu_utilization_sample_count": 0,
            "mean_gpu_utilization_percent": 0,
            "peak_gpu_utilization_percent": 0,
            "framebuffer_sample_count": 0,
            "peak_framebuffer_bytes": 0,
        }
        self._reseal(receipt)
        with self.assertRaisesRegex(DCGM.DcgmError, "dcgm_series_invalid"):
            DCGM.validate_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
