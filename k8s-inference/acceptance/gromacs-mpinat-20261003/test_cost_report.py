import json
import csv
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ledger import Ledger
from cost_report import (REFERENCES, allocation_bounds, build_report, canonical,
                         command_metrics, hourly_price, interval_union,
                         logical_operation_costs, logical_work, preset_shape, public_comparison,
                         ratio_bounds, read_cohorts, sha256, work_accounting, write_summary_csv)


REFS = json.loads(REFERENCES.read_text())


def replica(op="op", name="repeat-1", intervals=None, atoms=1000, original="a" * 64):
    intervals = intervals if intervals is not None else [[0, 100]]
    return {"operation_id": op, "shard_id": "job", "replica_id": name,
            "science_identity": {"native_input_sha256": original, "particle_count": atoms,
                                 "timestep_ps": .002, "initial_step": 0, "requested_steps": 100},
            "requested_domain": [0, 100], "attempt_intervals": intervals,
            "work": work_accounting(intervals, [0, 100])}


def group(*members):
    return [{"logical_id": "work", "reason": "explicit operator retry",
             "members": [{k: r[k] for k in ("operation_id", "shard_id", "replica_id")} for r in members]}]


class WorkTests(unittest.TestCase):
    def test_union_not_max_checkpoint(self):
        self.assertEqual(interval_union([[0, 100], [80, 150], [0, 100], [200, 250]]), [[0, 150], [200, 250]])
        result = work_accounting([[0, 100], [80, 150], [0, 100], [200, 250]], [0, 250])
        self.assertEqual(result["durable_steps"]["value"], 200)
        self.assertEqual(result["duplicate_durable_steps_lower_bound"]["value"], 120)
        self.assertIsNone(result["retry_or_lost_steps"]["value"])

    def test_unknown_progress_not_zero(self):
        result = work_accounting([None], [0, 100])
        self.assertIsNone(result["durable_steps"]["value"])
        self.assertEqual(result["durable_steps"]["status"], "unknown")
        self.assertEqual(result["known_durable_steps_lower_bound"]["value"], 0)

    def test_full_union_with_unknown_failed_attempt(self):
        result = work_accounting([None, [0, 100]], [0, 100])
        self.assertEqual(result["durable_steps"]["value"], 100)
        self.assertIsNone(result["executed_steps"]["value"])

    def test_partial_union_with_unknown_failed_attempt(self):
        result = work_accounting([None, [0, 80]], [0, 100])
        self.assertIsNone(result["durable_steps"]["value"])
        self.assertEqual(result["known_durable_steps_lower_bound"]["value"], 80)

    def test_measured_execution_waste(self):
        result = work_accounting([[0, 80], [60, 100]], [0, 100], executed_steps=150)
        self.assertEqual(result["retry_or_lost_steps"]["value"], 50)
        self.assertEqual(result["duplicate_durable_steps_lower_bound"]["value"], 20)

    def test_invalid_ranges_rejected(self):
        for intervals in ([[1, 0]], [[-1, 4]], [[.5, 3]], [[0, True]]):
            with self.subTest(intervals=intervals), self.assertRaises(ValueError):
                interval_union(intervals)
        with self.assertRaises(ValueError):
            work_accounting([[0, 101]], [0, 100])
        with self.assertRaises(ValueError):
            work_accounting([[0, 100]], executed_steps=50)

    def test_intentional_replicas_remain_distinct(self):
        rows = [replica(name=f"repeat-{i}") for i in (1, 2, 3)]
        result = logical_work(rows, [])
        self.assertEqual(len(result), 3)
        self.assertEqual(sum(r["work"]["durable_steps"]["value"] for r in result), 300)

    def test_cross_operation_no_automatic_deduplication(self):
        rows = [replica("op-a"), replica("op-b")]
        self.assertEqual(len(logical_work(rows, [])), 2)
        merged = logical_work(rows, group(*rows))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["work"]["durable_steps"]["value"], 100)
        self.assertEqual(merged[0]["work"]["duplicate_durable_steps_lower_bound"]["value"], 100)

    def test_science_mismatch_or_unknown_blocks_deduplication(self):
        for second in (replica("b", atoms=1001), replica("b", atoms=None), replica("b", original="b" * 64)):
            rows = [replica("a"), second]
            with self.subTest(second=second), self.assertRaises(ValueError):
                logical_work(rows, group(*rows))

    def test_cannot_collapse_distinct_repeats(self):
        rows = [replica("a", "repeat-1"), replica("b", "repeat-2")]
        with self.assertRaises(ValueError):
            logical_work(rows, group(*rows))

    def test_duplicate_members_rejected(self):
        row = replica()
        with self.assertRaises(ValueError):
            logical_work([row], group(row, row))

    def test_logical_cost_counts_operation_once_not_once_per_repeat(self):
        rows = [replica(op, f"repeat-{i}") for op in ("a", "b") for i in (1, 2, 3)]
        mapping = []
        for i in (1, 2, 3):
            mapping += [{**group(*[r for r in rows if r["replica_id"] == f"repeat-{i}"])[0], "logical_id": f"rep-{i}"}]
        logical = logical_work(rows, mapping)
        ops = [{"operation_id": op, "occupancy_gpu_seconds": {"lower": 100, "upper": 120},
                "allocated_occupancy_cost": {"lower": 1, "upper": 1.2}} for op in ("a", "b")]
        cost = logical_operation_costs(logical, ops)
        self.assertEqual(len(cost), 1)
        self.assertEqual(cost[0]["allocated_occupancy_cost"]["lower"], 2)
        self.assertAlmostEqual(cost[0]["useful_ns"]["value"], .0006)
        self.assertEqual(cost[0]["occupancy_gpu_seconds"]["lower"], 200)


class PricingTests(unittest.TestCase):
    def price(self, platform, preset, count, date="2026-10-03", region="eu-north1"):
        return hourly_price(preset_shape(platform, preset, region), count, REFS, date)

    def test_h100_october_price_with_bundled_cpu_ram(self):
        p = self.price("gpu-h100-sxm", "1gpu-16vcpu-200gb", 1)
        self.assertEqual(p["allocated_share_per_hour"], 4.5)
        self.assertEqual(p["full_node_per_hour"], 4.5)

    def test_full_node_and_two_node_shapes(self):
        for gpus in (1, 2, 4, 8, 16):
            nodes, per_node = (2, 8) if gpus == 16 else (1, gpus)
            p = self.price("gpu-h100-sxm", "8gpu-128vcpu-1600gb", per_node)
            self.assertEqual(p["allocated_share_per_hour"] * nodes, 4.5 * gpus)
            self.assertEqual(p["full_node_per_hour"] * nodes, 36 * nodes)

    def test_actual_l40s_intel_not_minimum_headline(self):
        p = self.price("gpu-l40s-a", "1gpu-16vcpu-64gb", 1)
        self.assertAlmostEqual(p["allocated_share_per_hour"], 1.7468)
        self.assertAlmostEqual(p["gpu_component_per_hour"], 1.35)

    def test_l40s_amd_gpu_fraction_not_full_node_per_pod(self):
        p = self.price("gpu-l40s-d", "4gpu-128vcpu-768gb", 1)
        self.assertAlmostEqual(p["full_node_per_hour"], 9.1376)
        self.assertAlmostEqual(p["allocated_share_per_hour"], 2.2844)

    def test_unknown_vm_shape_has_no_total_price(self):
        p = self.price("gpu-l40s-a", None, 1)
        self.assertIsNone(p["allocated_share_per_hour"])
        self.assertEqual(p["status"], "unknown")
        self.assertEqual(p["gpu_component_per_hour"], 1.35)

    def test_wrong_date_region_or_shape_no_invented_price(self):
        for kwargs in ({"date": "2026-10-04"}, {"date": "2026-09-30"}, {"region": "us-central1"}):
            self.assertIsNone(self.price("gpu-h100-sxm", "1gpu-16vcpu-200gb", 1, **kwargs)["allocated_share_per_hour"])
        self.assertIsNone(self.price("gpu-h100-sxm", "8gpu-128vcpu-1600gb", 16)["allocated_share_per_hour"])

    def test_missing_component_not_treated_as_free_or_bundled(self):
        refs = copy.deepcopy(REFS)
        refs["pricing"]["platforms"]["gpu-l40s-a"]["vcpu_hour"] = None
        result = hourly_price(preset_shape("gpu-l40s-a", "1gpu-16vcpu-64gb", "eu-north1"), 1, refs, "2026-10-03")
        self.assertIsNone(result["allocated_share_per_hour"])
        self.assertEqual(result["status"], "unknown")


class MetricTests(unittest.TestCase):
    def test_dimensions(self):
        m = command_metrics(864, .002, 1000, 2, 2, 10000)
        self.assertAlmostEqual(m["native_ms_per_step"]["value"], .2)
        self.assertEqual(m["native_particle_steps_per_gpu_second"]["value"], 2500000)
        self.assertEqual(m["durable_particle_steps_per_mdrun_gpu_second"]["value"], 2500000)
        self.assertEqual(m["useful_ns_per_mdrun_gpu_hour"]["value"], 18)
        self.assertIsNone(m["simulation_only_cost"]["value"])

    def test_missing_gpu_or_zero_denominator(self):
        for gpu in (None, 0):
            m = command_metrics(864, .002, 1000, gpu, 0, 100)
            self.assertIsNone(m["native_particle_steps_per_gpu_second"]["value"])
            self.assertIsNone(m["useful_ns_per_mdrun_gpu_hour"]["value"])
        self.assertEqual(ratio_bounds(1, {"lower": 0, "upper": 20})["lower"], .05)
        self.assertIsNone(ratio_bounds(1, {"lower": 0, "upper": 20})["upper"])

    def test_invalid_numeric_rejected(self):
        for rate in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                command_metrics(rate, .002, 1000, 1, 1, 1)

    def test_release_bounds_not_a_single_exact_time(self):
        result = allocation_bounds({"scheduled_at": "2026-10-03T10:00:00Z",
            "first_observed_at": "2026-10-03T10:00:05Z", "release_lower_bound": "2026-10-03T10:01:00Z",
            "release_upper_bound": "2026-10-03T10:01:10Z"})
        self.assertEqual((result["lower"], result["upper"]), (60, 70))
        self.assertIsNone(result["exact_device_release_at"])

    def test_missing_release_upper_remains_unknown(self):
        result = allocation_bounds({"scheduled_at": "2026-10-03T10:00:00Z", "first_observed_at": "2026-10-03T10:00:05Z"})
        self.assertEqual(result["lower"], 5)
        self.assertIsNone(result["upper"])

    def test_first_pending_observation_is_not_allocation_time(self):
        result = allocation_bounds({"scheduled_at": "2026-10-03T10:00:00Z",
            "first_observed_at": "2026-10-03T09:59:00Z", "release_upper_bound": "2026-10-03T10:01:00Z"})
        self.assertIsNone(result["lower"])
        self.assertEqual(result["upper"], 60)

    def test_inconsistent_timestamps_fail(self):
        with self.assertRaises(ValueError):
            allocation_bounds({"scheduled_at": "2026-10-03T10:00:00Z", "release_upper_bound": "2026-10-03T09:59:00Z"})

    def test_public_comparison_never_claims_matched_speedup(self):
        result = public_comparison({"operation_id": "op", "case_id": "benchsfi"}, [], [], REFS)
        self.assertEqual(result["public_ns_per_day"]["value"], 6.7)
        self.assertIsNone(result["matched_speedup"])
        self.assertEqual(result["comparison_status"], "historical_context_not_matched")
        self.assertIsNone(public_comparison({"operation_id": "op", "case_id": "benchmem"}, [], [], REFS)["public_ns_per_day"]["value"])


class IntegrationTests(unittest.TestCase):
    def fixture(self, root):
        path = root / "ledger.sqlite"
        ledger = Ledger(path)
        d = ledger.db
        d.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)", ("op", "REST-B", "benchsfi", "REST", "succeeded", "b" * 64, "a" * 64, '{"secret":"MUST_NOT_LEAK"}'))
        d.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", ("op", "a1", "workflow", "job", 1, "succeeded", "{}"))
        for name, val in (("particle_count", 3363), ("timestep_ps", .002), ("requested_steps", 100), ("protocol", {"initial_step": 0})):
            ledger.measure("op", "", "job", "repeat-1", name, val, None, "measured", "retained.log")
        ledger.measure("op", "a1", "job", "repeat-1", "durable_interval", [0, 100], "steps", "derived", "cpt")
        ledger.measure("op", "a1", "job", "repeat-1", "executed_steps", None, "steps", "unknown", "none")
        command = {"command": ["gmx", "mdrun", "-s", "input.tpr", "-ntmpi", "1", "-ntomp", "8"]}
        d.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("op", "job", "a1", "repeat-1", 1, "simulation_and_native_initialization", "2026-10-03T10:01:00Z", "2026-10-03T10:00:10Z", 50, 0, 100, 200, canonical(command)))
        d.execute("INSERT INTO allocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("op", "a1", "pod", "node", "2026-10-03T10:00:00Z", "2026-10-03T10:00:05Z", "2026-10-03T10:01:00Z", "2026-10-03T10:01:00Z", "2026-10-03T10:01:10Z", 1, "{}", '{"scientific-stage":"repo@sha256:123"}'))
        nodes = {"items": [{"metadata": {"name": "node", "labels": {
            "node.kubernetes.io/instance-type": "gpu-h100-sxm", "nebius.com/resource-preset": "1gpu-16vcpu-200gb", "topology.kubernetes.io/region": "eu-north1"}}, "secret": "MUST_NOT_LEAK"}]}
        d.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", ("telemetry/nodes.jsonl", 1, "2026-10-03T10:00:00Z", None, None, canonical(nodes)))
        d.commit()
        d.close()
        return path

    def test_real_schema_report_is_read_only_and_allowlisted(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.fixture(Path(directory))
            before = sha256(p)
            report = build_report(p, REFS)
            self.assertEqual(sha256(p), before)
            self.assertNotIn("MUST_NOT_LEAK", canonical(report))
            self.assertEqual(report["source"]["ledger_open_mode"], "read-only transaction")
            op = report["operations"][0]
            self.assertAlmostEqual(op["useful_ns"]["value"], .0002)
            self.assertEqual(op["allocated_occupancy_cost"]["lower"], .075)
            self.assertEqual(op["allocated_occupancy_cost"]["upper"], .0875)
            self.assertEqual(report["commands"][0]["gpu_allocation_count"], 1)
            self.assertEqual(report["commands"][0]["timing_policy"], "no_forced_reset_warmup_inclusive")

    def test_absent_attempt_telemetry_no_total_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.fixture(Path(directory))
            with sqlite3.connect(p) as d:
                d.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", ("op", "a0", "workflow", "job", 0, "failed", "{}"))
            op = build_report(p, REFS)["operations"][0]
            self.assertIsNone(op["allocated_occupancy_cost"]["upper"])
            self.assertEqual(op["allocated_occupancy_cost"]["status"], "incomplete_attempt_coverage")
            self.assertAlmostEqual(op["useful_ns"]["value"], .0002)

    def test_unknown_node_no_price(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.fixture(Path(directory))
            with sqlite3.connect(p) as d:
                d.execute("DELETE FROM observations")
            self.assertIsNone(build_report(p, REFS)["allocations"][0]["allocated_occupancy_cost"]["upper"])

    def test_conflicting_shapes_not_silently_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            p = self.fixture(Path(directory))
            with sqlite3.connect(p) as d:
                n = {"items": [{"metadata": {"name": "node", "labels": {"nebius.com/resource-preset": "8gpu-128vcpu-1600gb"}}}]}
                d.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", ("telemetry/nodes.jsonl", 2, "2026-10-03T10:02:00Z", None, None, canonical(n)))
            self.assertIsNone(build_report(p, REFS)["allocations"][0]["price"]["allocated_share_per_hour"])

    def test_saved_receipt_strips_signed_urls_and_detects_index_lag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = self.fixture(root)
            case = root / "cohort" / "case"
            case.mkdir(parents=True)
            result = {"operation_id": "other", "status": "succeeded", "engine_id": "digest"}
            native = case / "output.artifact"
            native.write_text(canonical(result))
            receipt = {"operation_id": "other", "state": "verified", "case": "case",
                       "signed_url": "SECRET", "native_outputs": {"manifest_url": "SECRET"},
                       "verified_artifacts": [{"path": str(native), "semantic_type": "gromacs-workflow-result/v1", "sha256": sha256(native), "size_bytes": native.stat().st_size}]}
            (case / "receipt.json").write_text(canonical(receipt))
            report = build_report(p, REFS, [case.parent])
            self.assertEqual(report["coverage"]["saved_operations_missing_from_ledger"], ["other"])
            self.assertNotIn("SECRET", canonical(report))
            native.write_text("tamper")
            with self.assertRaises(ValueError):
                read_cohorts([case.parent])

    def test_summary_csv_preserves_unknown_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = build_report(self.fixture(root), REFS)
            output = root / "table.csv"
            write_summary_csv(report, output)
            with output.open() as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["simulation_only_cost_usd"], "")
            self.assertEqual(row["simulation_only_cost_status"], "unknown")
            self.assertEqual(row["public_gtx1080_ns_day"], "6.7")
            with self.assertRaises(FileExistsError):
                write_summary_csv(report, output)


if __name__ == "__main__":
    unittest.main()
