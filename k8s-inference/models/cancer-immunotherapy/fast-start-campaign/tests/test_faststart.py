"""Offline contract tests for the fast-start live campaign harness.

These never touch the cluster.  They pin the behaviour that makes a receipt
trustworthy: the level boundaries follow the published contract, an unobserved
boundary is reported as unavailable rather than estimated, a cached image is
never counted as a pull, and the rendered Job matches the qualified runtime.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import faststart  # noqa: E402
import summarize  # noqa: E402


def stamp(seconds: int) -> str:
    base = dt.datetime(2026, 9, 3, 3, 0, 0, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def fake_raw(*, scheduled=10, started=100, result=200, finished=205, cached=True, logs=None):
    events = [
        {"reason": "Scheduled", "firstTimestamp": stamp(scheduled), "message": "assigned"},
        {
            "reason": "Pulled",
            "firstTimestamp": stamp(scheduled + 1),
            "message": (
                'Container image "x@sha256:a" already present on machine and can be accessed by the pod'
                if cached
                else 'Successfully pulled image "x@sha256:a" in 1m45.457s (1m45.457s including waiting). '
                "Image size: 4204439320 bytes."
            ),
        },
    ]
    if logs is None:
        logs = "\n".join([
            f"{stamp(150)} 0 loss: 15.48 time: 92.75",
            f"{stamp(160)} 1 loss: 14.20 time: 0.25",
            f'{stamp(result)} {{"gpu": "NVIDIA H100 80GB HBM3", "status": "succeeded"}}',
        ])
    return {
        "job": {"metadata": {"creationTimestamp": stamp(0), "uid": "job-uid", "name": "j"}},
        "pod": {
            "metadata": {"name": "p", "uid": "pod-uid"},
            "spec": {"nodeName": "node-a"},
            "status": {
                "conditions": [
                    {"type": "PodScheduled", "status": "True", "lastTransitionTime": stamp(scheduled)}
                ],
                "containerStatuses": [
                    {
                        "containerID": "containerd://abc",
                        "imageID": "x@sha256:a",
                        "state": {
                            "terminated": {
                                "startedAt": stamp(started),
                                "finishedAt": stamp(finished),
                                "exitCode": 0,
                            }
                        },
                    }
                ],
            },
        },
        "workload": {
            "status": {"conditions": [{"type": "Admitted", "status": "True", "lastTransitionTime": stamp(5)}]}
        },
        "events": {"items": events},
        "logs": logs,
    }


SPEC = {
    "log_markers": {
        "runtime_result": '"status": "succeeded"',
        "optimizer_progress": r"^\d+ loss: ",
        "step_time": r" time: ([0-9.]+)\s*$",
        "gpu_device_kind": r'"gpu": "([^"]+)"',
    }
}
THRESHOLDS = {"L4": 30, "L3": 60, "L2": 120, "L1": 300}


class TestDurationParsing(unittest.TestCase):
    def test_parses_go_style_durations(self):
        self.assertEqual(faststart.parse_duration("1m45.457s"), 105.457)
        self.assertEqual(faststart.parse_duration("850ms"), 0.85)
        self.assertEqual(faststart.parse_duration("32.5s"), 32.5)

    def test_rejects_non_timestamp_log_fragments(self):
        # tqdm carriage returns split into fragments with no stamp; these must
        # be skipped, not raise, or a whole trial's timeline is lost.
        self.assertIsNone(faststart.parse_time("100%|##########|"))
        self.assertIsNone(faststart.parse_time(""))
        self.assertIsNotNone(faststart.parse_time("2026-09-03T03:22:00.716711290Z"))


class TestImageEvidence(unittest.TestCase):
    def test_cached_image_is_not_reported_as_a_pull(self):
        evidence = faststart.image_evidence(fake_raw(cached=True)["events"])
        self.assertEqual(evidence["state"], "node-cached")
        self.assertTrue(evidence["cached_on_node"])
        self.assertIsNone(evidence["pull_seconds"])

    def test_pull_records_duration_and_bytes(self):
        evidence = faststart.image_evidence(fake_raw(cached=False)["events"])
        self.assertEqual(evidence["state"], "pulled")
        self.assertEqual(evidence["pull_seconds"], 105.457)
        self.assertEqual(evidence["image_bytes"], 4204439320)
        self.assertEqual(evidence["pull_bytes_per_second"], round(4204439320 / 105.457))


class TestTimeline(unittest.TestCase):
    def test_model_start_runs_from_scheduling_not_container_start(self):
        # The published contract puts image setup and artifact restore inside
        # model start.  Measuring from container start would hide them.
        measured = faststart.timeline(fake_raw(), SPEC, THRESHOLDS)
        self.assertEqual(measured["phases_seconds"]["model_start_seconds"], 190.0)
        self.assertEqual(measured["phases_seconds"]["capacity_wait_seconds"], 10.0)
        self.assertEqual(measured["phases_seconds"]["sandbox_and_volume_setup_seconds"], 90.0)
        self.assertEqual(measured["assigned_level"], "L1")

    def test_missing_result_marker_is_unavailable_not_estimated(self):
        raw = fake_raw(logs=f"{stamp(150)} 0 loss: 1.0 time: 1.0")
        measured = faststart.timeline(raw, SPEC, THRESHOLDS)
        self.assertIsNone(measured["phases_seconds"]["model_start_seconds"])
        self.assertIn("first_semantic_result", measured["unavailable"])
        self.assertEqual(measured["assigned_level"], "unavailable")

    def test_runtime_step_timings_are_captured(self):
        measured = faststart.timeline(fake_raw(), SPEC, THRESHOLDS)
        self.assertEqual(measured["throughput"]["runtime_reported_step_seconds_count"], 2)
        self.assertEqual(measured["throughput"]["runtime_reported_step_seconds_first"], 92.75)
        self.assertEqual(measured["throughput"]["runtime_reported_step_seconds_min"], 0.25)

    def test_gpu_kind_is_read_from_the_runtime_not_assumed(self):
        measured = faststart.timeline(fake_raw(), SPEC, THRESHOLDS)
        self.assertEqual(measured["gpu_device_kind_reported_by_runtime"], "NVIDIA H100 80GB HBM3")

    def test_sub_second_phase_is_clamped_and_disclosed(self):
        # Pod timestamps are second-granular while log stamps are nanosecond,
        # so a fast teardown can compute negative.  It must never be published.
        raw = fake_raw(result=205, finished=205)
        raw["logs"] = f'{stamp(0)} start\n2026-09-03T03:03:25.195000Z {{"status": "succeeded"}}'
        measured = faststart.timeline(raw, SPEC, THRESHOLDS)
        self.assertEqual(measured["phases_seconds"]["teardown_seconds"], 0.0)
        self.assertTrue(any("teardown_seconds" in note for note in measured["precision_notes"]))

    def test_recursive_volume_ownership_pass_is_recorded(self):
        raw = fake_raw()
        raw["events"]["items"].append({
            "reason": "VolumePermissionChangeInProgress",
            "firstTimestamp": stamp(20),
            "message": "Setting volume ownership ... is taking long",
        })
        measured = faststart.timeline(raw, SPEC, THRESHOLDS)
        self.assertTrue(measured["volume_ownership"]["recursive_fsgroup_pass_observed"])
        self.assertEqual(measured["volume_ownership"]["event_count"], 1)


class TestLevels(unittest.TestCase):
    def test_thresholds_are_inclusive_upper_bounds(self):
        self.assertEqual(faststart.level_for(30.0, THRESHOLDS), "L4")
        self.assertEqual(faststart.level_for(30.1, THRESHOLDS), "L3")
        self.assertEqual(faststart.level_for(300.0, THRESHOLDS), "L1")
        self.assertEqual(faststart.level_for(300.1, THRESHOLDS), "Off")

    def test_unmeasured_start_never_earns_a_level(self):
        self.assertEqual(faststart.level_for(None, THRESHOLDS), "unavailable")


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.matrix = faststart.matrix()

    def test_every_model_is_pinned_by_digest(self):
        for model, spec in self.matrix["models"].items():
            self.assertIn("@sha256:", spec["image"], f"{model} is not digest-pinned")
            self.assertTrue(spec["image"].endswith(spec["image_digest"]), model)

    def test_mosaic_job_matches_the_qualified_runtime_contract(self):
        _, job = faststart.render("mosaic", "baseline", 1)
        pod = job["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(container["image"], self.matrix["models"]["mosaic"]["image"])
        self.assertIn("--recipe-sha256", container["command"])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["nodeSelector"], self.matrix["placement"]["node_selector"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])

    def test_read_only_is_set_on_the_mount_never_on_the_claim(self):
        # A read-only claim marks the whole CSI attachment read-only, which
        # would silently strip write access from the workspace mount.
        _, job = faststart.render("mosaic", "baseline", 1)
        pod = job["spec"]["template"]["spec"]
        for volume in pod["volumes"]:
            self.assertNotIn("readOnly", volume.get("persistentVolumeClaim", {}))
        mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
        self.assertTrue(mounts["artifacts"].get("readOnly"))
        self.assertNotIn("readOnly", mounts["workspace"])

    def test_fast_volume_variant_only_changes_the_ownership_policy(self):
        _, baseline = faststart.render("mosaic", "baseline", 1)
        _, fast = faststart.render("mosaic", "fast-volume", 1)
        base_pod = baseline["spec"]["template"]["spec"]
        fast_pod = fast["spec"]["template"]["spec"]
        self.assertNotIn("fsGroupChangePolicy", base_pod["securityContext"])
        self.assertEqual(fast_pod["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch")
        self.assertEqual(base_pod["containers"][0]["env"], fast_pod["containers"][0]["env"])
        self.assertEqual(base_pod["containers"][0]["image"], fast_pod["containers"][0]["image"])
        self.assertEqual(base_pod["containers"][0]["resources"], fast_pod["containers"][0]["resources"])
        # Only the per-trial output directory may differ, so two trials never
        # write into each other's result tree.
        base_argv = base_pod["containers"][0]["command"]
        fast_argv = fast_pod["containers"][0]["command"]
        self.assertEqual(base_argv[:-1], fast_argv[:-1])
        self.assertNotEqual(base_argv[-1], fast_argv[-1])

    def test_alphafold3_binds_the_tenant_private_claim_without_fsgroup(self):
        _, job = faststart.render("alphafold3", "baseline", 1)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(job["metadata"]["namespace"], "fs2-academic-poc")
        self.assertNotIn("fsGroup", pod["securityContext"])
        self.assertIn(65532, pod["securityContext"]["supplementalGroups"])
        self.assertNotIn("kueue.x-k8s.io/queue-name", job["spec"]["template"]["metadata"]["labels"])

    def test_alphafold3_never_mounts_the_quarantine_claim(self):
        _, job = faststart.render("alphafold3", "baseline", 1)
        claims = {
            volume["persistentVolumeClaim"]["claimName"]
            for volume in job["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in volume
        }
        self.assertNotIn("cancer-immunotherapy-academic-assets-rwx-v1", claims)
        self.assertIn("academic-assets-runtime-rwx", claims)

    def test_alphafold3_runs_the_no_data_pipeline_path(self):
        _, job = faststart.render("alphafold3", "baseline", 1)
        script = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        self.assertIn("--norun_data_pipeline", script)
        self.assertIn("FS2_SEMANTIC_RESULT_EMITTED", script)

    def test_boltzgen_needs_no_request_configmap(self):
        config_map, job = faststart.render("boltzgen", "baseline", 1)
        self.assertIsNone(config_map)
        names = {volume["name"] for volume in job["spec"]["template"]["spec"]["volumes"]}
        self.assertNotIn("request", names)

    def test_trial_names_are_stable_and_unique(self):
        names = {
            faststart.job_name(self.matrix, model, variant, trial)
            for model, spec in self.matrix["models"].items()
            for variant in spec["variants"]
            for trial in (1, 2, 3)
        }
        expected = sum(len(spec["variants"]) for spec in self.matrix["models"].values()) * 3
        self.assertEqual(len(names), expected)
        self.assertTrue(all(len(name) <= 63 for name in names))
        self.assertEqual(
            faststart.job_name(self.matrix, "mosaic", "baseline", 1),
            faststart.job_name(self.matrix, "mosaic", "baseline", 1),
        )


class TestSummary(unittest.TestCase):
    def test_p95_is_the_worst_observation_for_a_three_trial_cohort(self):
        self.assertEqual(summarize.statistics_for([10.0, 20.0, 30.0])["p95"], 30.0)

    def test_summary_only_counts_semantically_passed_trials(self):
        summary = summarize.summarize()
        for model, entry in summary["models"].items():
            for variant, values in entry["variants"].items():
                self.assertLessEqual(
                    values["trials_semantically_passed"], values["trials_complete"], f"{model}/{variant}"
                )
                if values.get("qualified_level_all_trials"):
                    self.assertGreaterEqual(values["trials_semantically_passed"], 1)


class TestWarmJit(unittest.TestCase):
    def setUp(self):
        self.matrix = faststart.matrix()
        self.variant = self.matrix["models"]["mosaic"]["variants"]["warm-jit"]

    def test_warm_jit_also_carries_the_ownership_remediation(self):
        # Without it the recursive volume pass would swamp the compilation
        # saving this variant exists to measure.
        self.assertEqual(
            self.variant["pod_security_patch"]["fsGroupChangePolicy"], "OnRootMismatch"
        )
        _, job = faststart.render("mosaic", "warm-jit", 1)
        security = job["spec"]["template"]["spec"]["securityContext"]
        self.assertEqual(security["fsGroupChangePolicy"], "OnRootMismatch")

    def test_compile_time_floor_is_zero(self):
        # A non-zero floor silently disables caching for Mosaic, whose startup is
        # many short compilations rather than one long one. This was observed
        # live: at 0.5 s the cache directory was never even created.
        self.assertEqual(
            self.variant["env"]["JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS"], "0"
        )
        _, job = faststart.render("mosaic", "warm-jit", 1)
        env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["JAX_PERSISTENT_CACHE_MIN_COMPILE_TIME_SECS"], "0")
        self.assertTrue(env["JAX_COMPILATION_CACHE_DIR"].startswith("/workspace/"))

    def test_cache_is_evidenced_by_a_probe_not_by_the_setting(self):
        evidence = self.variant["jit_cache_evidence"]
        self.assertEqual(evidence["before_first_trial"]["entries"], 0)
        self.assertGreater(evidence["after_first_trial"]["entries"], 0)

    def test_summary_separates_one_time_compilation_from_steady_state(self):
        summary = summarize.summarize()
        block = summary["models"]["mosaic"]["variants"]["warm-jit"]["compilation"]
        self.assertEqual(len(block["one_time_compilation"]["trials"]), 1)
        self.assertGreaterEqual(len(block["steady_state"]["trials"]), 3)
        cold = block["one_time_compilation"]["model_start_seconds"]["p95"]
        warm = block["steady_state"]["model_start_seconds"]["p95"]
        self.assertLess(warm, cold)


class TestPurgeSafety(unittest.TestCase):
    def test_purge_refuses_paths_outside_campaign_roots(self):
        # The claim belongs to another task, so a mistyped path must not be able
        # to delete its artifact tree.
        for unsafe in ("../etc", "mosaic", "boltzgen", "/", "", "faststart/../mosaic"):
            with self.assertRaises(SystemExit, msg=unsafe):
                faststart.purge([unsafe])

    def test_purge_accepts_campaign_owned_roots(self):
        for root in faststart.CAMPAIGN_DIRECTORIES:
            self.assertIn(root, faststart.CAMPAIGN_DIRECTORIES)
        self.assertNotIn("mosaic", faststart.CAMPAIGN_DIRECTORIES)
        self.assertNotIn("boltzgen", faststart.CAMPAIGN_DIRECTORIES)


class TestScheduleToComplete(unittest.TestCase):
    def test_schedule_to_semantic_complete_spans_the_whole_stage(self):
        measured = faststart.timeline(fake_raw(scheduled=10, finished=205), SPEC, THRESHOLDS)
        phases = measured["phases_seconds"]
        self.assertEqual(phases["schedule_to_semantic_complete_seconds"], 195.0)
        self.assertGreaterEqual(
            phases["schedule_to_semantic_complete_seconds"], phases["model_start_seconds"]
        )

    def test_every_receipt_reports_it(self):
        import json as _json
        from pathlib import Path as _Path
        receipts = sorted((_Path(__file__).resolve().parent.parent / "receipts").glob("*.json"))
        self.assertTrue(receipts)
        for path in receipts:
            phases = _json.loads(path.read_text())["measured"]["phases_seconds"]
            self.assertIsNotNone(
                phases.get("schedule_to_semantic_complete_seconds"), path.name
            )


class TestMechanismClaims(unittest.TestCase):
    def test_no_snapshot_level_is_claimed_without_restore_proof(self):
        snapshot = faststart.matrix()["mechanism_evidence"]["gpu_process_snapshot"]
        self.assertEqual(snapshot["state"], "unsupported")
        self.assertEqual(snapshot["claim"], "none")

    def test_local_nvme_stays_unavailable(self):
        self.assertEqual(
            faststart.matrix()["mechanism_evidence"]["local_nvme"]["state"], "unavailable"
        )


if __name__ == "__main__":
    unittest.main()
