"""Offline tests for the live-surface acceptance runner; no network, no cluster."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "run_live_surface_acceptance.py"
EXPECTATIONS_PATH = ROOT / "expectations" / "h100-retained.json"
SPEC = importlib.util.spec_from_file_location("fs2_live_surface_acceptance", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

CONTROL_PLANE = "sha256:" + "1" * 64
ADMIN_CONSOLE = "sha256:" + "2" * 64
SECRET = "live-surface-bearer-value-must-never-appear"  # noqa: S105


def expectations() -> dict[str, Any]:
    return MODULE.load_expectations(EXPECTATIONS_PATH)


def bundle() -> dict[str, Any]:
    return {
        "schema": MODULE.BUNDLE_SCHEMA,
        "cluster": {
            "cluster_id": "mk8scluster-example",
            "cluster_name": "k8s-inference-example",
            "kube_context": "k8s-inference-example",
            "project_id": "project-example",
            "region": "eu-north1",
        },
        "endpoints": {
            "admin_portal_url": "https://203.0.113.10/admin/",
            "alertmanager_url": "https://203.0.113.10/admin/observability/alertmanager",
            "grafana_url": "https://203.0.113.10/admin/observability/grafana",
            "inference_base_url": "https://203.0.113.10/v1",
            "mcp_url": "https://203.0.113.10/mcp",
            "tempo_explore_url": "https://203.0.113.10/admin/observability/grafana/explore",
        },
        "credentials": {
            "admin_bootstrap_token": SECRET + "-admin",
            "mcp_inference_token": SECRET + "-inference",
            "inference_access_token": SECRET + "-inference",
            "scientific_access_token": SECRET + "-scientific",
            "grafana": {"username": "grafana-user", "password": SECRET + "-grafana"},
        },
        "mcp_access": {"tenant_id": "tenant-example", "models": ["*"], "scopes": ["catalog.read", "mcp.invoke"]},
    }


def deployment(name: str, digest: str, *, replicas: int = 2, ready: int | None = None) -> dict[str, Any]:
    ready = replicas if ready is None else ready
    return {
        "metadata": {"name": name, "generation": 3},
        "spec": {
            "replicas": replicas,
            "template": {"spec": {"containers": [{"image": f"registry.example/cp@{digest}"}]}},
        },
        "status": {
            "replicas": replicas,
            "updatedReplicas": replicas,
            "readyReplicas": ready,
            "availableReplicas": ready,
            "observedGeneration": 3,
        },
    }


def daemonset(digest: str, *, ready: int = 2) -> dict[str, Any]:
    return {
        "metadata": {"name": "fs2-serve-control-plane-gpu-observer", "generation": 2},
        "spec": {"template": {"spec": {"containers": [{"image": f"registry.example/cp@{digest}"}]}}},
        "status": {
            "desiredNumberScheduled": 2,
            "numberReady": ready,
            "updatedNumberScheduled": 2,
            "numberAvailable": ready,
            "observedGeneration": 2,
        },
    }


def active(name: str, namespace: str | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    return {"metadata": metadata, "status": {"conditions": [{"type": "Active", "status": "True"}]}}


def mcp_projection(model_ids: list[str], scientific: list[str]) -> dict[str, Any]:
    return {
        "cache_scope": "private",
        "model_ids": model_ids,
        "protocol_version": "2026-07-28",
        "scientific_model_ids": scientific,
        "tools": ["list_models", "list_scientific_models", "submit_scientific_run", "invoke_model"],
        "ttl_ms": 0,
    }


def scientific_rows(model_ids: list[str]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "model_id": model_id,
                "operations": ["predict-structure"],
                "service_classes": ["customer-batch"],
                "parameter_schema": "fs2-serve.nebius.ai/schema/example/v1",
                "execution_identity_sha256": "a" * 64,
            }
            for model_id in model_ids
        ],
    }


class ExpectationsTests(unittest.TestCase):
    def test_retained_h100_expectations_load(self) -> None:
        value = expectations()
        self.assertEqual(len(value["scientific_model_ids"]), 10)
        self.assertEqual(value["chat_probe"]["model_id"], "qwen3-8b")

    def test_invalid_expectations_are_rejected_before_any_probe(self) -> None:
        base = expectations()
        broken_cases = [
            {"schema": "other"},
            {"scientific_model_ids": ["a", "a"]},
            {"general_token_excluded_scientific_model_ids": ["not-in-catalog"]},
            {"openai_model_ids": ["not-general"]},
            {"chat_probe": {**base["chat_probe"], "marker": "short"}},
            {"chat_probe": {**base["chat_probe"], "request_overrides": {"model": "other"}}},
            {"chat_probe": {**base["chat_probe"], "max_tokens": 8}},
        ]
        with TemporaryDirectory() as temporary:
            for index, patch in enumerate(broken_cases):
                path = Path(temporary) / f"broken-{index}.json"
                path.write_text(json.dumps({**base, **patch}))
                with self.assertRaises(MODULE.AcceptanceInputError, msg=str(patch)):
                    MODULE.load_expectations(path)


class BundleTests(unittest.TestCase):
    def test_complete_owner_only_bundle_passes(self) -> None:
        evidence, passed = MODULE.evaluate_bundle(bundle(), 0o600, True)
        self.assertTrue(passed, evidence)
        self.assertEqual(evidence["mode"], "0600")
        self.assertTrue(evidence["distinct_scientific_token"])
        self.assertNotIn(SECRET, json.dumps(evidence))

    def test_group_readable_or_incomplete_bundle_fails(self) -> None:
        self.assertFalse(MODULE.evaluate_bundle(bundle(), 0o640, True)[1])
        self.assertFalse(MODULE.evaluate_bundle(bundle(), 0o600, False)[1])
        missing = bundle()
        del missing["credentials"]["scientific_access_token"]
        self.assertFalse(MODULE.evaluate_bundle(missing, 0o600, True)[1])
        shared = bundle()
        shared["credentials"]["scientific_access_token"] = shared["credentials"]["inference_access_token"]
        self.assertFalse(MODULE.evaluate_bundle(shared, 0o600, True)[1])

    def test_read_bundle_rejects_symlinks(self) -> None:
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "bundle.json"
            target.write_text(json.dumps(bundle()))
            link = Path(temporary) / "link.json"
            os.symlink(target, link)
            with self.assertRaises(MODULE.AcceptanceInputError):
                MODULE.read_bundle(link)
            document, mode, owner = MODULE.read_bundle(target)
            self.assertEqual(document["schema"], MODULE.BUNDLE_SCHEMA)
            self.assertTrue(owner)
            self.assertIsInstance(mode, int)


class ReleaseTests(unittest.TestCase):
    def test_exact_digests_and_ready_replicas_pass(self) -> None:
        deployments = {
            "fs2-serve-control-plane": deployment("fs2-serve-control-plane", CONTROL_PLANE, replicas=3),
            "fs2-serve-control-plane-admin-console": deployment("fs2-serve-control-plane-admin-console", ADMIN_CONSOLE),
            "fs2-serve-control-plane-model-controller": deployment(
                "fs2-serve-control-plane-model-controller", CONTROL_PLANE
            ),
        }
        evidence, passed = MODULE.evaluate_kubernetes_release(
            deployments,
            daemonset(CONTROL_PLANE),
            control_plane_digest=CONTROL_PLANE,
            admin_console_digest=ADMIN_CONSOLE,
        )
        self.assertTrue(passed, evidence)
        self.assertEqual(evidence["ready_release_pods"], {"admin-console": 2, "gateway": 3, "model-controller": 2})

    def test_stale_digest_or_unready_observer_fails(self) -> None:
        deployments = {
            "fs2-serve-control-plane": deployment("fs2-serve-control-plane", "sha256:" + "9" * 64),
            "fs2-serve-control-plane-admin-console": deployment("fs2-serve-control-plane-admin-console", ADMIN_CONSOLE),
            "fs2-serve-control-plane-model-controller": deployment(
                "fs2-serve-control-plane-model-controller", CONTROL_PLANE
            ),
        }
        self.assertFalse(
            MODULE.evaluate_kubernetes_release(
                deployments,
                daemonset(CONTROL_PLANE),
                control_plane_digest=CONTROL_PLANE,
                admin_console_digest=ADMIN_CONSOLE,
            )[1]
        )
        deployments["fs2-serve-control-plane"] = deployment("fs2-serve-control-plane", CONTROL_PLANE)
        self.assertFalse(
            MODULE.evaluate_kubernetes_release(
                deployments,
                daemonset(CONTROL_PLANE, ready=1),
                control_plane_digest=CONTROL_PLANE,
                admin_console_digest=ADMIN_CONSOLE,
            )[1]
        )

    def test_kueue_requires_exact_queues_flavors_and_active_conditions(self) -> None:
        value = expectations()
        cluster_queues = {name: active(name) for name in value["cluster_queues"]}
        local_queues = [active("inference-models", "fs2-models"), active("academic-scientific", "fs2-academic-poc")]
        flavors = set(value["resource_flavors"])
        priorities = set(value["workload_priority_classes"]) | {"extra"}
        evidence, passed = MODULE.evaluate_kueue(cluster_queues, local_queues, flavors, priorities, value)
        self.assertTrue(passed, evidence)
        self.assertEqual(evidence["local_queue_namespaces"], ["fs2-academic-poc", "fs2-models"])
        self.assertFalse(
            MODULE.evaluate_kueue(cluster_queues, local_queues, flavors - {"general-cpu"}, priorities, value)[1]
        )
        inactive = dict(cluster_queues)
        inactive["general-cpu"] = {"metadata": {"name": "general-cpu"}, "status": {"conditions": []}}
        self.assertFalse(MODULE.evaluate_kueue(inactive, local_queues, flavors, priorities, value)[1])


class CatalogTests(unittest.TestCase):
    def admin_inputs(self) -> dict[str, Any]:
        value = expectations()
        return {
            "session_status": 200,
            "cookie_round_trip": True,
            "delete_status": 204,
            "context_value": {"server_authoritative": True},
            "models_value": {
                "items": [
                    {"identity": {"id": model_id, "gpu_class": "nvidia-h100-sxm5-80gb"}, "runtime": {"state": "ready"}}
                    for model_id in value["general_model_ids"]
                ]
            },
            "scientific_value": {
                "items": [
                    {"model_id": model_id, "readiness": "qualified"} for model_id in value["scientific_model_ids"]
                ],
                "projection_issues": [],
            },
            "capacity_value": {
                "node_scaler": {
                    "state": "available",
                    "configured": True,
                    "healthy": True,
                    "provider": "nebius-managed-node-group-autoscaler",
                },
                "kueue": {"cluster_queues": [1, 2, 3], "local_queues": [1, 2, 3]},
                "node_pools": [1, 2, 3],
            },
            "observability_value": {
                "components": [
                    {
                        "id": component,
                        "installed": True,
                        "health": "healthy",
                        "data_present": True,
                        "launch": {"enabled": True},
                    }
                    for component in value["observability_components"]
                ]
            },
            "expectations": value,
        }

    def test_admin_backend_passes_only_when_every_profile_is_qualified(self) -> None:
        inputs = self.admin_inputs()
        evidence, passed = MODULE.evaluate_admin(**inputs)
        self.assertTrue(passed, evidence)
        self.assertEqual(evidence["scientific_readiness"]["qualified"], 10)
        self.assertEqual(evidence["gpu_classes"]["qwen3-8b"], "nvidia-h100-sxm5-80gb")
        inputs["scientific_value"]["items"][0]["readiness"] = "candidate"
        self.assertFalse(MODULE.evaluate_admin(**inputs)[1])
        inputs = self.admin_inputs()
        inputs["observability_value"]["components"][0]["launch"]["enabled"] = False
        self.assertFalse(MODULE.evaluate_admin(**inputs)[1])

    def test_mcp_catalogs_are_scoped_per_token(self) -> None:
        value = expectations()
        scientific = value["scientific_model_ids"]
        excluded = set(value["general_token_excluded_scientific_model_ids"])
        general_evidence, general_passed = MODULE.evaluate_mcp(
            mcp_projection(value["general_model_ids"], sorted(set(scientific) - excluded)),
            expected_general=set(value["general_model_ids"]),
            expected_scientific=set(scientific) - excluded,
            expectations=value,
            excluded=excluded,
        )
        self.assertTrue(general_passed, general_evidence)
        self.assertEqual(general_evidence["licensed_models_excluded"], ["alphafold3", "bindcraft"])
        _, scientific_passed = MODULE.evaluate_mcp(
            mcp_projection([], scientific),
            expected_general=set(),
            expected_scientific=set(scientific),
            expectations=value,
        )
        self.assertTrue(scientific_passed)
        leaked = mcp_projection(value["general_model_ids"], scientific)
        self.assertFalse(
            MODULE.evaluate_mcp(
                leaked,
                expected_general=set(value["general_model_ids"]),
                expected_scientific=set(scientific) - excluded,
                expectations=value,
                excluded=excluded,
            )[1]
        )
        cached = {**mcp_projection([], scientific), "ttl_ms": 60000}
        self.assertFalse(
            MODULE.evaluate_mcp(
                cached, expected_general=set(), expected_scientific=set(scientific), expectations=value
            )[1]
        )

    def test_openai_catalog_must_agree_with_admin_identity_on_accelerator_class(self) -> None:
        value = expectations()
        listing = {
            "data": [{"id": "qwen3-8b", "enabled": True, "gpu_class": "nvidia-h100-sxm5-80gb", "operations": ["chat"]}]
        }
        evidence, passed = MODULE.evaluate_openai_catalog(
            listing, 200, admin_gpu_classes={"qwen3-8b": "nvidia-h100-sxm5-80gb"}, expectations=value
        )
        self.assertTrue(passed, evidence)
        stale = {
            "data": [{"id": "qwen3-8b", "enabled": True, "gpu_class": "NVIDIA-B300-SXM6-288GB", "operations": ["chat"]}]
        }
        evidence, passed = MODULE.evaluate_openai_catalog(
            stale, 200, admin_gpu_classes={"qwen3-8b": "nvidia-h100-sxm5-80gb"}, expectations=value
        )
        self.assertFalse(passed)
        self.assertFalse(evidence["gpu_class_matches_admin_identity"])
        self.assertFalse(MODULE.evaluate_openai_catalog(listing, 200, admin_gpu_classes={}, expectations=value)[1])
        self.assertFalse(
            MODULE.evaluate_openai_catalog(
                {"data": []}, 200, admin_gpu_classes={"qwen3-8b": "nvidia-h100-sxm5-80gb"}, expectations=value
            )[1]
        )

    def test_http_scientific_discovery_mirrors_both_mcp_catalogs(self) -> None:
        value = expectations()
        scientific = value["scientific_model_ids"]
        excluded = set(value["general_token_excluded_scientific_model_ids"])
        evidence, passed = MODULE.evaluate_http_scientific_discovery(
            scientific_rows(scientific),
            200,
            scientific_rows(sorted(set(scientific) - excluded)),
            200,
            expectations=value,
        )
        self.assertTrue(passed, evidence)
        self.assertEqual(evidence["licensed_models_excluded"], ["alphafold3", "bindcraft"])
        self.assertFalse(
            MODULE.evaluate_http_scientific_discovery(
                scientific_rows(scientific), 200, scientific_rows(scientific), 200, expectations=value
            )[1]
        )
        incomplete = scientific_rows(scientific)
        del incomplete["data"][0]["parameter_schema"]
        self.assertFalse(
            MODULE.evaluate_http_scientific_discovery(
                incomplete, 200, scientific_rows(sorted(set(scientific) - excluded)), 200, expectations=value
            )[1]
        )
        self.assertFalse(
            MODULE.evaluate_http_scientific_discovery(
                {}, 404, scientific_rows(sorted(set(scientific) - excluded)), 200, expectations=value
            )[1]
        )


class ChatTests(unittest.TestCase):
    def completion(self, content: str, *, finish_reason: str = "stop", tokens: int = 7) -> dict[str, Any]:
        return {
            "model": "qwen3-8b",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
            ],
            "usage": {"prompt_tokens": 21, "completion_tokens": tokens, "total_tokens": 21 + tokens},
        }

    def test_marker_must_be_present_and_text_is_never_copied(self) -> None:
        evidence, passed = MODULE.evaluate_chat(
            status_code=200,
            document=self.completion("<think>\nshort\n</think>\n\nFS2_LIVE_SURFACE_OK"),
            marker="FS2_LIVE_SURFACE_OK",
            model_id="qwen3-8b",
            elapsed_seconds=1.2345,
            terminal_status="succeeded",
            operation_id_present=True,
        )
        self.assertTrue(passed, evidence)
        self.assertNotIn("short", json.dumps(evidence))
        self.assertEqual(evidence["completion_tokens"], 7)
        self.assertEqual(evidence["elapsed_seconds"], 1.234)
        truncated = self.completion("<think>\nstill thinking", finish_reason="length")
        self.assertFalse(
            MODULE.evaluate_chat(
                status_code=200,
                document=truncated,
                marker="FS2_LIVE_SURFACE_OK",
                model_id="qwen3-8b",
                elapsed_seconds=1.0,
                terminal_status="succeeded",
                operation_id_present=True,
            )[1]
        )
        self.assertFalse(
            MODULE.evaluate_chat(
                status_code=200,
                document=self.completion("FS2_LIVE_SURFACE_OK"),
                marker="FS2_LIVE_SURFACE_OK",
                model_id="qwen3-8b",
                elapsed_seconds=1.0,
                terminal_status="failed",
                operation_id_present=True,
            )[1]
        )

    def test_chat_probe_follows_the_bounded_wait_to_the_durable_result(self) -> None:
        marker = "FS2_LIVE_SURFACE_OK"
        completion = self.completion(marker)
        calls: list[tuple[str, str]] = []

        class Response:
            def __init__(self, status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
                self.status_code = status_code
                self._body = body
                self.headers = headers or {}
                self.content = json.dumps(body).encode()

            def json(self) -> dict[str, Any]:
                return self._body

        class Client:
            def __init__(self) -> None:
                self.polls = 0

            def post(self, path: str, *, headers: dict[str, str], json: dict[str, Any]) -> Response:  # noqa: A002
                calls.append(("POST", path))
                assert json["model"] == "qwen3-8b" and json["chat_template_kwargs"] == {"enable_thinking": False}
                assert marker in json["messages"][0]["content"]
                return Response(202, {"id": "op-1", "status": "queued"}, {"x-fs2-operation-id": "op-1"})

            def get(self, path: str, *, headers: dict[str, str]) -> Response:
                calls.append(("GET", path))
                if path.endswith("/result"):
                    return Response(200, completion)
                self.polls += 1
                return Response(200, {"status": "running" if self.polls < 3 else "succeeded"})

        ticks = iter(range(0, 1000))
        evidence, passed = MODULE.chat_probe(
            Client(),
            token=SECRET,
            probe=expectations()["chat_probe"],
            clock=lambda: float(next(ticks)),
            sleep=lambda _: None,
        )
        self.assertTrue(passed, evidence)
        self.assertEqual(evidence["terminal_status"], "succeeded")
        self.assertEqual(calls[0], ("POST", "/v1/chat/completions"))
        self.assertEqual(calls[-1], ("GET", "/v1/operations/op-1/result"))
        self.assertNotIn(SECRET, json.dumps(evidence))


class ReceiptTests(unittest.TestCase):
    def test_receipt_is_sorted_value_free_and_owner_only(self) -> None:
        checks = {
            "zeta": ({"ok": True}, True),
            "alpha": ({"count": 1}, False),
        }
        receipt = MODULE.build_receipt(
            checks,
            started_at="2026-09-05T00:00:00Z",
            completed_at="2026-09-05T00:00:05Z",
            target={"source_commit": "a" * 40, "control_plane_digest": CONTROL_PLANE},
            expectations_sha256="b" * 64,
        )
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["failures"], ["alpha"])
        self.assertEqual(list(receipt["checks"]), ["alpha", "zeta"])
        self.assertEqual(receipt["schema"], MODULE.SCHEMA)
        MODULE.assert_value_free(receipt, (SECRET,))
        with self.assertRaises(MODULE.AcceptanceInputError):
            MODULE.assert_value_free({"leak": SECRET}, (SECRET,))
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "receipt.json"
            MODULE.write_receipt(path, receipt, overwrite=False)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text())["status"], "FAIL")
            with self.assertRaises(MODULE.AcceptanceInputError):
                MODULE.write_receipt(path, receipt, overwrite=False)
            MODULE.write_receipt(path, {**receipt, "status": "PASS"}, overwrite=True)
            self.assertEqual(json.loads(path.read_text())["status"], "PASS")

    def test_secret_values_cover_nested_credentials(self) -> None:
        values = MODULE.secret_values(bundle())
        self.assertIn(SECRET + "-grafana", values)
        self.assertIn(SECRET + "-scientific", values)
        self.assertNotIn("grafana-user", values[:0])

    def test_main_rejects_malformed_identities_before_probing(self) -> None:
        with self.assertRaises(MODULE.AcceptanceInputError):
            MODULE.main(
                [
                    "--bundle",
                    "/nonexistent",
                    "--kubeconfig",
                    "/nonexistent",
                    "--context",
                    "ctx",
                    "--expectations",
                    str(EXPECTATIONS_PATH),
                    "--source-commit",
                    "short",
                    "--control-plane-digest",
                    CONTROL_PLANE,
                    "--admin-console-digest",
                    ADMIN_CONSOLE,
                ]
            )


if __name__ == "__main__":
    unittest.main()
