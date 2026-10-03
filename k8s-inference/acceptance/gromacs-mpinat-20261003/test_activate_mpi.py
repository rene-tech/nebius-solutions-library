"""Activation tests use three Apps so unrelated live state is never incidental."""

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from activate_mpi import (
    API_IMAGE,
    NAMESPACE,
    PROOFS,
    REPO,
    ROOT,
    SELECTED,
    TOOLS_IMAGE,
    canonical,
    merge_execution,
    prepare,
    select_api_pod,
    sha,
)


def contracts():
    profiles = {"schema": "profiles/v1", "profiles": []}
    execution = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "models": [],
        "snapshot_bundles": {"live-only": {"identity": "unchanged"}},
    }
    for model in ("sibling", "gromacs", "gromacs-mpi"):
        workload = {
            "stages": [
                {
                    "id": "workflow",
                    "execution_shapes": [
                        {"id": "one", "placement": {"accelerator": {"count": 1, "pool_ids": ["l40s-1x"]}}},
                        {"id": "four", "placement": {"accelerator": {"count": 4, "pool_ids": ["l40s-4x"]}}},
                    ],
                }
            ]
        }
        identity = {
            "model_revision": "1" * 40,
            "runtime_image_digest": "sha256:" + "a" * 64,
            "runtime_recipe_sha256": "b" * 64,
            "workload_recipe_sha256": PROOFS["digest"](workload),
            "artifact_manifest_digest": "c" * 64,
        }
        identity["execution_identity_sha256"] = PROOFS["digest"](identity)
        profile = {
            "model_id": model,
            "route_exposed": True,
            "workload": workload,
            "execution_identity": identity,
            "qualification": {"h100_semantic_receipt_sha256": "d" * 64},
        }
        profiles["profiles"].append(profile)
        execution["models"].append(
            {
                "model_id": model,
                "execution_identity_sha256": identity["execution_identity_sha256"],
                "stages": [{"image": "registry.test/runtime@" + identity["runtime_image_digest"]}],
            }
        )
    reference = PROOFS["digest"]({"schema": execution["schema"], "models": execution["models"]})
    execution["qualification_baselines"] = {reference: [row["model_id"] for row in execution["models"]]}
    for profile in profiles["profiles"]:
        profile["qualification"]["execution_map_sha256"] = reference
    source, proposed_profiles = copy.deepcopy(execution), copy.deepcopy(profiles)
    for model in SELECTED:
        profile = next(p for p in proposed_profiles["profiles"] if p["model_id"] == model)
        identity = profile["execution_identity"]
        identity["runtime_image_digest"] = "sha256:" + "e" * 64
        identity["execution_identity_sha256"] = PROOFS["digest"](
            {key: value for key, value in identity.items() if key != "execution_identity_sha256"}
        )
        row = next(row for row in source["models"] if row["model_id"] == model)
        row["execution_identity_sha256"] = identity["execution_identity_sha256"]
        row["stages"][0]["image"] = "registry.test/runtime@" + identity["runtime_image_digest"]
        row["stages"][0]["execution_shapes"] = [{"id": "new-shape"}]
    source["qualification_baselines"] = PROOFS["rebase_qualification_baselines"](execution, source, SELECTED)
    proposed_profiles["profiles"] = PROOFS["rebase_profile_qualifications"](
        proposed_profiles["profiles"], execution, source, SELECTED
    )
    new_ref = PROOFS["digest"]({"schema": source["schema"], "models": source["models"]})
    for profile in proposed_profiles["profiles"]:
        if profile["model_id"] in SELECTED:
            profile["qualification"]["execution_map_sha256"] = new_ref
    source["snapshot_bundles"] = {"stale-source-only": {"identity": "must-not-copy"}}
    return execution, profiles, source, proposed_profiles


def fixture():
    execution, live_profiles, source, profiles = contracts()
    scheduling = {
        "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
        "model_eligible_pool_ids": {"gromacs-mpi": ["h100-reserved-8x"], "sibling": ["h100-reserved-8x"]},
        "pools": {
            pool: {"accelerator_resource_name": "nvidia.com/gpu"} for pool in ("h100-reserved-8x", "l40s-1x", "l40s-4x")
        },
        "accelerator_node_capacity": {"l40s-1x": {"accelerator_count": 1}, "l40s-4x": {"accelerator_count": 4}},
        "cluster_queues": {"live-lane": {"quota": 16}},
        "service_classes": {"interactive": {"priority": 100}},
    }
    cms = []
    for key, value, prefix in (
        ("execution-map.json", execution, "fs2-scientific-execution"),
        ("kueue-scheduling.json", scheduling, "fs2-scientific-scheduling"),
    ):
        raw = canonical(value)
        cms.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "immutable": True,
                "metadata": {"name": prefix + "-" + sha(raw)[:12], "namespace": NAMESPACE},
                "data": {key: raw.decode()},
            }
        )
    environment = {
        "FS2_CATALOG_DIR": "/opt/fs2/catalog",
        "FS2_SCIENTIFIC_BATCH_EXECUTION_MAP_FILE": "/etc/fs2-scientific-batch/execution-map.json",
        "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_FILE": "/etc/fs2-scientific-batch/kueue-scheduling.json",
        "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256": sha(canonical(scheduling)),
        "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE": TOOLS_IMAGE,
        "UNRELATED_SETTING": "preserve",
    }
    deployment = {
        "spec": {
            "replicas": 3,
            "template": {
                "metadata": {
                    "annotations": {"unrelated": "keep", "fs2.nebius.ai/catalog-rollout-digest": "sha256:existing"}
                },
                "spec": {
                    "containers": [
                        {
                            "name": "control-plane",
                            "image": API_IMAGE,
                            "env": [{"name": key, "value": value} for key, value in environment.items()],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "scientific-batch-execution",
                            "configMap": {
                                "name": cms[0]["metadata"]["name"],
                                "items": [{"key": "execution-map.json", "path": "execution-map.json"}],
                            },
                        },
                        {
                            "name": "scientific-batch-scheduling",
                            "configMap": {
                                "name": cms[1]["metadata"]["name"],
                                "items": [{"key": "kueue-scheduling.json", "path": "kueue-scheduling.json"}],
                            },
                        },
                        {"name": "unrelated-secret", "secret": {"secretName": "preserve"}},
                    ],
                },
            },
        }
    }
    options = dict(
        image=REPO + "@sha256:" + "f" * 64,
        tools_image=REPO + "@sha256:" + "f" * 64,
        expected_api=API_IMAGE,
        expected_tools=TOOLS_IMAGE,
        expected_execution=cms[0]["metadata"]["name"],
        expected_scheduling=cms[1]["metadata"]["name"],
    )
    return (deployment, *cms, live_profiles, source, profiles), options


class Activation(unittest.TestCase):
    def test_scoped_patch_preserves_all_other_live_settings_and_has_exact_inverse(self):
        arguments, options = fixture()
        before = copy.deepcopy(arguments)
        result = prepare(*arguments, **options)
        self.assertEqual(arguments, before)
        self.assertEqual(
            result["patch"][0], {"op": "test", "path": "/spec/template", "value": arguments[0]["spec"]["template"]}
        )
        self.assertEqual(result["inverse"][0]["value"], result["patch"][1]["value"])
        self.assertEqual(result["inverse"][1]["value"], result["patch"][0]["value"])
        updated = result["patch"][1]["value"]
        self.assertEqual(updated["spec"]["volumes"][-1], before[0]["spec"]["template"]["spec"]["volumes"][-1])
        self.assertEqual(updated["metadata"]["annotations"]["unrelated"], "keep")
        self.assertEqual(result["execution_map"]["snapshot_bundles"], {"live-only": {"identity": "unchanged"}})
        self.assertEqual(result["execution_map"]["models"][0], arguments[4]["models"][0])
        self.assertEqual(len(result["configmaps"]), 1)
        self.assertFalse(result["summary"]["applied"])
        self.assertIn("Old readers", result["summary"]["rollback"])
        reverted = copy.deepcopy(updated)
        reverted["spec"]["containers"][0]["image"] = API_IMAGE
        for item in reverted["spec"]["containers"][0]["env"]:
            if item["name"] == "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE":
                item["value"] = TOOLS_IMAGE
        reverted["spec"]["volumes"][0]["configMap"]["name"] = options["expected_execution"]
        reverted["metadata"]["annotations"].pop("fs2.nebius.ai/image-digest")
        for key, value in result["helm_overlay"]["podAnnotations"].items():
            self.assertEqual(reverted["metadata"]["annotations"].pop(key), value)
        self.assertEqual(reverted, before[0]["spec"]["template"])

    def test_explicit_pool_extension_changes_only_mpi_eligibility(self):
        arguments, options = fixture()
        result = prepare(*arguments, **options, enable_mpi_pools=("l40s-1x", "l40s-4x"))
        old = json.loads(arguments[2]["data"]["kueue-scheduling.json"])
        updated = json.loads(result["configmaps"][1]["data"]["kueue-scheduling.json"])
        self.assertEqual(
            updated["model_eligible_pool_ids"].pop("gromacs-mpi"), ["h100-reserved-8x", "l40s-1x", "l40s-4x"]
        )
        old["model_eligible_pool_ids"].pop("gromacs-mpi")
        self.assertEqual(updated, old)
        for cm in result["configmaps"]:
            raw = next(iter(cm["data"].values())).encode()
            self.assertTrue(cm["immutable"])
            self.assertTrue(cm["metadata"]["name"].endswith("-" + sha(raw)[:12]))

    def test_image_or_map_baseline_drift_stops_preparation(self):
        for field in ("expected_api", "expected_tools", "expected_execution", "expected_scheduling"):
            arguments, options = fixture()
            options[field] = REPO + "@sha256:" + "0" * 64 if "api" in field or "tools" in field else "changed"
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare(*arguments, **options)

    def test_unrelated_source_row_or_profile_drift_is_never_grafted(self):
        live, old_profiles, source, profiles = contracts()
        source["models"][0]["stages"][0]["image"] = "different"
        with self.assertRaisesRegex(ValueError, "unrelated execution"):
            merge_execution(live, old_profiles, source, profiles)
        live, old_profiles, source, profiles = contracts()
        profiles["profiles"][0]["qualification"]["h100_semantic_receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "unrelated profile"):
            merge_execution(live, old_profiles, source, profiles)

    def test_old_whole_map_reference_cannot_qualify_changed_rows(self):
        live, old_profiles, source, profiles = contracts()
        profiles["profiles"][1]["qualification"]["execution_map_sha256"] = old_profiles["profiles"][1]["qualification"][
            "execution_map_sha256"
        ]
        with self.assertRaisesRegex(ValueError, "qualification membership"):
            merge_execution(live, old_profiles, source, profiles)

    def test_invalid_captured_proof_is_rejected(self):
        live, old_profiles, source, profiles = contracts()
        live["models"][1]["stages"][0]["image"] = "different"
        with self.assertRaisesRegex(ValueError, "existing qualification baseline changed"):
            merge_execution(live, old_profiles, source, profiles)

    def test_missing_pool_capacity_and_unknown_pool_are_rejected(self):
        arguments, options = fixture()
        with self.assertRaisesRegex(ValueError, "capacity"):
            prepare(*arguments, **options, enable_mpi_pools=("invented-pool",))

    def test_captured_bytes_mount_and_checksum_drift_are_rejected(self):
        for mutation in ("bytes", "mount", "checksum", "mutable"):
            arguments, options = fixture()
            if mutation == "bytes":
                arguments[1]["data"]["execution-map.json"] += " "
            elif mutation == "mount":
                arguments[0]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["items"][0]["path"] = "elsewhere"
            elif mutation == "checksum":
                environment = arguments[0]["spec"]["template"]["spec"]["containers"][0]["env"]
                next(item for item in environment if item["name"].endswith("CONTRACT_SHA256"))["value"] = "0" * 64
            else:
                arguments[1]["immutable"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                prepare(*arguments, **options)

    def test_stale_selected_identity_and_recipe_are_rejected(self):
        for mutation in ("identity", "recipe"):
            live, old_profiles, source, profiles = contracts()
            if mutation == "identity":
                profiles["profiles"][1]["execution_identity"]["runtime_recipe_sha256"] = "0" * 64
            else:
                profiles["profiles"][1]["workload"]["stages"][0]["execution_shapes"][0]["id"] = "other"
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "stale"):
                merge_execution(live, old_profiles, source, profiles)

    def test_unchanged_reader_image_requires_explicit_mechanics_only(self):
        arguments, options = fixture()
        options["image"] = API_IMAGE
        with self.assertRaisesRegex(ValueError, "updated API reader"):
            prepare(*arguments, **options)
        result = prepare(*arguments, **options, mechanics_only=True)
        self.assertTrue(result["summary"]["mechanics_only"])
        self.assertIn("draining alone is insufficient", result["summary"]["rollback"])

    def test_capture_pod_must_belong_to_the_actual_deployment(self):
        deployment = {"metadata": {"uid": "deployment-uid"}}
        replicasets = {
            "items": [
                {"metadata": {"uid": "rs-uid", "ownerReferences": [{"uid": "deployment-uid", "controller": True}]}}
            ]
        }
        pod = {
            "metadata": {"name": "api", "ownerReferences": [{"uid": "rs-uid", "controller": True}]},
            "spec": {"containers": [{"name": "control-plane", "image": API_IMAGE}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        self.assertEqual(select_api_pod(deployment, replicasets, {"items": [pod]}, API_IMAGE), pod)
        for mutation in ("owner", "image", "ready", "deleting"):
            changed = copy.deepcopy(pod)
            if mutation == "owner":
                changed["metadata"]["ownerReferences"][0]["uid"] = "other-rs"
            elif mutation == "image":
                changed["spec"]["containers"][0]["image"] = TOOLS_IMAGE
            elif mutation == "ready":
                changed["status"]["conditions"][0]["status"] = "False"
            else:
                changed["metadata"]["deletionTimestamp"] = "2026-10-03T01:00:00Z"
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "no ready API"):
                select_api_pod(deployment, replicasets, {"items": [changed]}, API_IMAGE)

    def test_unicode_content_address_matches_helm_json(self):
        self.assertEqual(canonical({"v": "é<&>\u2028"}), b'{"v":"\xc3\xa9\\u003c\\u0026\\u003e\\u2028"}')

    def test_overlay_execution_configmap_matches_the_actual_chart_template(self):
        arguments, options = fixture()
        result = prepare(*arguments, **options)
        helm = shutil.which("helm")
        self.assertIsNotNone(helm, "Helm is required to verify content-addressed chart reconciliation")
        templates = ROOT / "charts/control-plane/fs2-serve-control-plane/templates"
        with tempfile.TemporaryDirectory(prefix="fs2-gromacs-activation-helm-") as directory:
            chart = Path(directory)
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: activation-template-test\nversion: 0.1.0\n")
            (chart / "templates").mkdir()
            for name in ("_helpers.tpl", "scientific-execution-map.yaml"):
                shutil.copyfile(templates / name, chart / "templates" / name)
            values = copy.deepcopy(result["helm_overlay"])
            values["scientificBatch"].update(enabled=True, executionMapKey="execution-map.json")
            (chart / "values.yaml").write_text(json.dumps(values))
            # Render only the unchanged production ConfigMap template, without any live release replay.
            rendered = subprocess.check_output(  # noqa: S603
                [helm, "template", "activation-template-test", str(chart), "--namespace", NAMESPACE], text=True
            )
            cm = yaml.safe_load(rendered)
        expected = result["configmaps"][0]
        self.assertEqual(cm["metadata"]["name"], expected["metadata"]["name"])
        self.assertEqual(cm["data"], expected["data"])
        self.assertEqual(cm["immutable"], expected["immutable"])
        self.assertEqual(
            cm["metadata"]["annotations"]["fs2-serve.nebius.ai/execution-map-sha256"],
            expected["metadata"]["annotations"]["fs2-serve.nebius.ai/execution-map-sha256"],
        )


if __name__ == "__main__":
    unittest.main()
