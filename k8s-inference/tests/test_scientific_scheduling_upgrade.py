from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]


def _run(terraform: str, directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [terraform, f"-chdir={directory}", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_local_queue_rebind_replacement_is_wired_at_the_production_address() -> None:
    """The immutable binding must force replacement, not an in-place update."""

    queue_source = (ROOT / "stages/workloads/queue.tf").read_text(encoding="utf-8")
    # The binding identity lives in state, and the manifest is replaced when it
    # changes, at the production resource address.
    assert 'resource "terraform_data" "additional_local_queue_binding"' in queue_source
    assert 'resource "kubernetes_manifest" "additional_local_queue"' in queue_source
    assert "replace_triggered_by = [terraform_data.additional_local_queue_binding[each.key]]" in queue_source
    assert 'resource "terraform_data" "model_local_queue_binding"' in queue_source
    assert "replace_triggered_by = [terraform_data.model_local_queue_binding]" in queue_source
    assert "triggers_replace = [" in queue_source
    for trigger in ("each.value.metadata.namespace", "each.value.spec.clusterQueue"):
        assert trigger in queue_source

    # One owner: a queue another module creates is never rendered here.
    assert "module.kueue_scheduling.contract.external_local_queue_names, queue_name" in queue_source

    # The academic module owns its own queue and carries the same semantics.
    academic = (ROOT / "modules/academic-assets/main.tf").read_text(encoding="utf-8")
    assert 'resource "terraform_data" "academic_local_queue_binding"' in academic
    assert "replace_triggered_by = [terraform_data.academic_local_queue_binding[each.key]]" in academic

    general = (ROOT / "stages/workloads/general_cpu.tf").read_text(encoding="utf-8")
    assert 'resource "terraform_data" "general_cpu_local_queue_binding"' in general
    assert "replace_triggered_by = [terraform_data.general_cpu_local_queue_binding[each.key]]" in general

    reference = (ROOT / "reference-data/terraform/main.tf").read_text(encoding="utf-8")
    assert 'resource "terraform_data" "local_queue_binding"' in reference
    assert "replace_triggered_by = [terraform_data.local_queue_binding]" in reference

    # The live proof runs the real kubernetes provider against a Kueue API and
    # asserts delete+create at exactly this address.
    script = (
        ROOT / "modules/kueue-scheduling/scripts/kind-localqueue-rebind.sh"
    ).read_text(encoding="utf-8")
    assert 'kubernetes_manifest.additional_local_queue[\"cancer-primary\"]' in script
    assert "resource_changes" in script
    assert '["delete", "create"]' in script



def test_verified_chart_archive_is_the_bytes_helm_installs() -> None:
    """One materialized archive, consumed by the verifier, the CRD apply, and Helm."""

    materializer = ROOT / "modules/jobset-controller/scripts/materialize-chart.sh"
    assert materializer.is_file()
    jobset = (ROOT / "modules/jobset-controller/main.tf").read_text(encoding="utf-8")
    foundation = (ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
    foundation_locals = (ROOT / "stages/foundation/locals.tf").read_text(encoding="utf-8")

    # The archive is materialized during plan, so the Helm provider resolves
    # the local bytes rather than pulling its own copy of the reference.
    assert 'data "external" "chart"' in jobset
    assert "chart_archive     = var.enabled ? data.external.chart[0].result.path : null" in jobset
    assert "chart            = local.chart_archive" in jobset
    assert "FS2_JOBSET_CHART_ARCHIVE        = local.chart_archive" in jobset
    assert 'data "external" "kueue_chart"' in foundation
    assert "chart            = local.kueue_chart_archive" in foundation
    assert "FS2_KUEUE_CHART_ARCHIVE        = local.kueue_chart_archive" in foundation
    assert "kueue_chart_archive = data.external.kueue_chart.result.path" in foundation_locals

    # Neither installer re-resolves the OCI reference.
    assert "chart            = local.chart_ref" not in jobset
    assert "chart            = local.kueue_release.chart_digest_ref" not in foundation

    # The consumers read the materialized file instead of pulling again.
    crd = (ROOT / "modules/jobset-controller/scripts/apply-jobset-crd.sh").read_text(encoding="utf-8")
    verifier = (ROOT / "modules/jobset-controller/scripts/verify-jobset-release.sh").read_text(encoding="utf-8")
    kueue_verifier = (
        ROOT / "stages/foundation/scripts/materialize-kueue-release.sh"
    ).read_text(encoding="utf-8")
    for script in (crd, verifier, kueue_verifier):
        assert "helm pull" not in script
        assert "CHART_ARCHIVE" in script


def test_jobset_chart_materializer_remains_plan_time_during_namespace_changes() -> None:
    """Namespace ordering must not defer the child module's external data source."""

    foundation = (ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
    jobset_module = foundation.split('module "jobset_controller" {', 1)[1].split("\n}", 1)[0]

    # The resource-derived input keeps consumers behind namespace creation.
    assert (
        'namespace          = kubernetes_namespace_v1.platform["jobset-system"].metadata[0].name'
        in jobset_module
    )
    # A whole-module dependency also captures data.external.chart, moving its
    # read to apply and presenting Helm with an unknown/empty chart at plan.
    assert "  depends_on =" not in jobset_module


def test_chart_materializer_is_content_addressed_and_idempotent() -> None:
    """A rerun with unchanged bytes does no work, and drift is refused."""

    for tool in ("helm", "crane"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is required")
    program = ROOT / "modules/jobset-controller/scripts/materialize-chart.sh"
    digest = "sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24"
    archive = "bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a"

    def materialize(run_root: Path, archive_sha256: str) -> subprocess.CompletedProcess[str]:
        query = {
            "chart_ref": "oci://registry.k8s.io/jobset/charts/jobset",
            "chart_digest": digest,
            "archive_sha256": archive_sha256,
            "chart_name": "jobset",
            "run_root": str(run_root),
        }
        return subprocess.run(
            [str(program)],
            input=json.dumps(query),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    with tempfile.TemporaryDirectory(prefix="fs2-chart-materialize-") as raw_directory:
        run_root = Path(raw_directory) / "run"
        run_root.mkdir()
        first = materialize(run_root, archive)
        assert first.returncode == 0, first.stderr
        result = json.loads(first.stdout)
        materialized = Path(result["path"])
        assert materialized.name == f"jobset-{archive}.tgz"
        assert materialized.parent == run_root / "charts"
        assert hashlib.sha256(materialized.read_bytes()).hexdigest() == archive
        assert stat.S_IMODE(materialized.stat().st_mode) == 0o600

        # Idempotent: the same path, and no second download.
        stamp = materialized.stat().st_mtime_ns
        second = materialize(run_root, archive)
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout) == result
        assert materialized.stat().st_mtime_ns == stamp

        # A declared digest that does not match the bytes is refused.
        drifted = materialize(run_root, "0" * 64)
        assert drifted.returncode != 0
        assert "drifted" in drifted.stderr


def _valid_module_inputs() -> dict[str, object]:
    classes = {
        name: {
            "workload_priority_class": priority_name,
            "priority": priority,
            "preemption_mode": mode,
            "pool_preference": ["regular"],
        }
        for name, priority_name, priority, mode in (
            ("platform-critical", "platform-critical", 10000, "restartable"),
            ("presentation", "presentation", 1000, "restartable"),
            ("interactive", "interactive", 100, "restartable"),
            ("customer-batch", "standard", 0, "restartable"),
            ("bulk-backfill", "batch", -100, "restartable"),
        )
    }
    return {
        "pools": {
            "regular": {
                "flavor_name": "example-regular",
                "resource_name": "example.com/accelerator",
                "capacity": 8,
            }
        },
        "default_queue": {
            "cluster_queue_name": "inference-accelerators",
            "local_queue_name": "inference-models",
            "namespace": "fs2-models",
            "queueing_strategy": "BestEffortFIFO",
        },
        "scheduling": {
            "cohort": {"enabled": True, "name": "inference-shared", "fair_sharing_weight": 1},
            "cluster_queues": {},
            "local_queues": {},
            "service_classes": classes,
        },
        "base_priority_classes": {"interactive": 100, "standard": 0, "batch": -100},
    }


def _invalid_policy(scenario: str) -> dict[str, object]:
    value = _valid_module_inputs()
    scheduling = value["scheduling"]
    assert isinstance(scheduling, dict)
    cluster_queues = scheduling["cluster_queues"]
    local_queues = scheduling["local_queues"]
    classes = scheduling["service_classes"]
    assert isinstance(cluster_queues, dict) and isinstance(local_queues, dict) and isinstance(classes, dict)
    queue = {
        "namespace": "fs2-models",
        "queueing_strategy": "BestEffortFIFO",
        "fair_sharing_weight": 1,
        "admission_fair_sharing": True,
        "flavor_order": ["regular"],
        "pool_quotas": {"regular": {"nominal_quota": 8}},
        "preemption": {
            "reclaim_within_cohort": "LowerPriority",
            "within_cluster_queue": "LowerPriority",
        },
    }
    if scenario == "fair_sharing_weight":
        cohort = scheduling["cohort"]
        assert isinstance(cohort, dict)
        cohort["fair_sharing_weight"] = 0.000000001
    elif scenario == "flavor_preference":
        queue["flavor_fungibility"] = {
            "when_can_borrow": "MayStopSearch",
            "when_can_preempt": "TryNextFlavor",
            "preference": "BorrowingOverPreemption",
        }
        cluster_queues["inference-accelerators"] = queue
    elif scenario == "stable_local_queue":
        cluster_queues["alternate"] = queue
        local_queues["inference-models"] = {
            "namespace": "fs2-models",
            "cluster_queue": "alternate",
            "fair_sharing_weight": 1,
            "model_ids": [],
        }
    elif scenario == "admission_checks":
        queue["admission_checks"] = [
            {"name": f"check-{index}", "on_flavors": ["regular"]} for index in range(65)
        ]
        cluster_queues["inference-accelerators"] = queue
    elif scenario == "on_flavors":
        pools = {
            f"p{index:02d}": {
                "flavor_name": f"example-p{index:02d}",
                "resource_name": f"example.com/accelerator-{index // 33}",
                "capacity": 1,
            }
            for index in range(65)
        }
        value["pools"] = pools
        order = list(pools)
        queue["flavor_order"] = order
        queue["pool_quotas"] = {pool_id: {"nominal_quota": 1} for pool_id in order}
        queue["admission_checks"] = [{"name": "capacity", "on_flavors": order}]
        cluster_queues["inference-accelerators"] = queue
        for policy in classes.values():
            policy["pool_preference"] = order
    elif scenario == "max_execution":
        classes["customer-batch"]["max_execution_seconds"] = 2_147_483_648
    elif scenario == "max_queue":
        classes["customer-batch"]["max_queue_seconds"] = 2_147_483_648
    elif scenario == "checkpointable":
        classes["bulk-backfill"]["preemption_mode"] = "checkpointable"
    elif scenario == "local_queue_label":
        local_queues["q" * 64] = {
            "namespace": "fs2-models",
            "cluster_queue": "inference-accelerators",
            "fair_sharing_weight": 1,
            "model_ids": ["protein-design"],
            "service_classes": ["customer-batch"],
        }
    elif scenario == "priority_label":
        classes["customer-batch"]["workload_priority_class"] = "p" * 64
    elif scenario == "priority_order":
        classes["bulk-backfill"]["priority"] = 1001
    elif scenario == "reversed_warm_order":
        value["pools"] = {
            "warm": {
                "flavor_name": "example-warm",
                "resource_name": "example.com/accelerator",
                "capacity": 8,
                "preemptible": False,
                "min_nodes": 1,
            },
            "burst": {
                "flavor_name": "example-burst",
                "resource_name": "example.com/accelerator",
                "capacity": 8,
                "preemptible": True,
                "min_nodes": 0,
            },
        }
        reverse = ["burst", "warm"]
        queue["flavor_order"] = reverse
        queue["pool_quotas"] = {
            "warm": {"nominal_quota": 8},
            "burst": {"nominal_quota": 0},
        }
        cluster_queues["inference-accelerators"] = queue
        for policy in classes.values():
            policy["pool_preference"] = reverse
    elif scenario == "resource_flavor_label":
        value["pools"]["regular"]["flavor_name"] = "f" * 64
    elif scenario == "pool_count":
        value["pools"] = {
            f"p{index:02d}": {
                "flavor_name": f"example-p{index:02d}",
                "resource_name": "example.com/accelerator",
                "capacity": 1,
            }
            for index in range(33)
        }
    elif scenario == "resource_name_bad_prefix_dot":
        value["pools"]["regular"]["resource_name"] = "a..b/gpu"
    elif scenario == "resource_name_prefix_segment_64":
        value["pools"]["regular"]["resource_name"] = f"{'a' * 64}.example/gpu"
    elif scenario == "resource_name_prefix_over_253":
        # A 254-character prefix with a short name: 258 total, so a
        # total-length bound alone would accept it.
        value["pools"]["regular"]["resource_name"] = f"{'a' * 254}/gpu"
    elif scenario == "resource_name_over_qualified_bound":
        # 318 characters: one past a 253-character prefix plus a slash and a
        # 63-character name, which is the exact Kubernetes maximum.
        prefix = "a" * 254
        resource_name = f"{prefix}/{'g' * 63}"
        assert len(resource_name) == 318, len(resource_name)
        value["pools"]["regular"]["resource_name"] = resource_name
    elif scenario == "cluster_queue_bad_prefix_dot":
        cluster_queues["a..b"] = queue
    elif scenario == "admission_check_prefix_segment_64":
        queue["admission_checks"] = [{"name": f"{'a' * 64}.example", "on_flavors": ["regular"]}]
        cluster_queues["inference-accelerators"] = queue
    elif scenario == "common_label_value":
        value["labels"] = {"fs2.nebius.ai/owner": "rene@nebius.com"}
    elif scenario == "label_bad_prefix_dot":
        value["labels"] = {"a..b/name": "valid"}
    elif scenario == "label_bad_prefix_upper":
        value["labels"] = {"A.example/name": "valid"}
    elif scenario == "label_name_64":
        value["labels"] = {f"example.com/{'n' * 64}": "valid"}
    elif scenario == "label_value_64":
        value["labels"] = {"example.com/name": "v" * 64}
    elif scenario == "annotation_bad_prefix":
        value["annotations"] = {"a..b/name": "not-a-label-value"}
    elif scenario == "base_priority_name":
        value["base_priority_classes"]["p" * 64] = 1
    elif scenario == "base_priority_value":
        value["base_priority_classes"]["extra"] = 2_147_483_648
    elif scenario == "dead_tenant_route":
        local_queues["tenant-route"] = {
            "namespace": "fs2-models",
            "cluster_queue": "inference-accelerators",
            "fair_sharing_weight": 1,
            "model_ids": [],
            "tenant_ids": ["tenant-a"],
        }
    elif scenario == "tenant_label_value":
        local_queues["tenant-route"] = {
            "namespace": "fs2-models",
            "cluster_queue": "inference-accelerators",
            "fair_sharing_weight": 1,
            "model_ids": ["protein-design"],
            "tenant_ids": ["t" * 64],
            "service_classes": ["customer-batch"],
        }
    elif scenario == "route_namespace":
        local_queues["other-namespace"] = {
            "namespace": "other-models",
            "cluster_queue": "inference-accelerators",
            "fair_sharing_weight": 1,
            "model_ids": ["protein-design"],
            "service_classes": ["customer-batch"],
        }
    else:  # pragma: no cover - test table is closed below
        raise AssertionError(scenario)
    return value


@pytest.mark.parametrize(
    "scenario",
    [
        "fair_sharing_weight",
        "flavor_preference",
        "stable_local_queue",
        "admission_checks",
        "on_flavors",
        "max_execution",
        "max_queue",
        "checkpointable",
        "local_queue_label",
        "priority_label",
        "priority_order",
        "reversed_warm_order",
        "resource_flavor_label",
        "pool_count",
        "resource_name_bad_prefix_dot",
        "resource_name_prefix_segment_64",
        "resource_name_prefix_over_253",
        "resource_name_over_qualified_bound",
        "cluster_queue_bad_prefix_dot",
        "admission_check_prefix_segment_64",
        "common_label_value",
        "label_bad_prefix_dot",
        "label_bad_prefix_upper",
        "label_name_64",
        "label_value_64",
        "annotation_bad_prefix",
        "base_priority_name",
        "base_priority_value",
        "dead_tenant_route",
        "tenant_label_value",
        "route_namespace",
    ],
)
def test_kueue_policy_boundaries_fail_during_terraform_plan(scenario: str) -> None:
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("terraform is required")
    with tempfile.TemporaryDirectory(prefix=f"fs2-kueue-{scenario}-") as raw_directory:
        directory = Path(raw_directory)
        module_path = (ROOT / "modules/kueue-scheduling").as_posix()
        (directory / "main.tf").write_text(
            f'''module "scheduling" {{
  source = "{module_path}"
  pools = var.pools
  default_queue = var.default_queue
  scheduling = var.scheduling
  base_priority_classes = var.base_priority_classes
  labels = var.labels
  annotations = var.annotations
}}
variable "pools" {{ type = any }}
variable "default_queue" {{ type = any }}
variable "scheduling" {{ type = any }}
variable "base_priority_classes" {{ type = any }}
variable "labels" {{
  type    = map(string)
  default = {{}}
}}
variable "annotations" {{
  type    = map(string)
  default = {{}}
}}
''',
            encoding="utf-8",
        )
        (directory / "invalid.tfvars.json").write_text(
            json.dumps(_invalid_policy(scenario), sort_keys=True),
            encoding="utf-8",
        )
        initialized = _run(terraform, directory, "init", "-backend=false", "-input=false", "-no-color")
        assert initialized.returncode == 0, initialized.stderr
        planned = _run(
            terraform,
            directory,
            "plan",
            "-input=false",
            "-no-color",
            "-var-file=invalid.tfvars.json",
        )
        assert planned.returncode != 0, f"{scenario} unexpectedly planned successfully"


def test_rendered_contract_cpu_classes_validate_as_exact_v1_bytes() -> None:
    """Validate the module's actual jsonencode output, not a hand-written twin."""

    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("terraform is required")

    values = _valid_module_inputs()
    values.update(
        {
            "core_capacity": {
                "regular": {"cpu_millicores": 32000, "memory_mib": 131072}
            },
            "cpu_classes": {
                "reference-data": {
                    "local_queue": "academic-reference-cpu",
                    "cluster_queue": "reference-data-cpu",
                    "namespace": "fs2-academic-poc",
                    "resource_flavor": "reference-data-cpu",
                    "eligible_pool_ids": ["reference-cpu"],
                    "pool_resolution": {
                        "mode": "per-pool-flavor",
                        "pool_id": "reference-cpu",
                    },
                    "node_selector": {
                        "workload.fs2.nebius/reference-data": "true"
                    },
                    "tolerations": [
                        {
                            "key": "workload.fs2.nebius/reference-data",
                            "operator": "Equal",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ],
                    "schedulable_capacity": {
                        "cpu": "30000m",
                        "memory": "122880Mi",
                        "ephemeral_storage": "114688Mi",
                        "cpu_millicores": 30000,
                        "memory_mib": 122880,
                        "ephemeral_storage_mib": 114688,
                    },
                },
                "general-cpu": {
                    "local_queue": "general-cpu",
                    "cluster_queue": "general-cpu",
                    "namespace": "fs2-models",
                    "resource_flavor": "general-cpu",
                    "eligible_pool_ids": ["general-small", "general-large"],
                    "pool_resolution": {
                        "mode": "node-label-observation",
                        "node_label_key": "accelerator.fs2.nebius/pool-id",
                    },
                    "node_selector": {"workload.fs2.nebius/general-cpu": "true"},
                    "tolerations": [],
                    "schedulable_capacity": {
                        "cpu": "15500m",
                        "memory": "61440Mi",
                        "ephemeral_storage": "0Mi",
                        "cpu_millicores": 15500,
                        "memory_mib": 61440,
                        "ephemeral_storage_mib": 0,
                    },
                },
            },
            "cpu_stage_requests": {
                "reference-data": {"cpu_millicores": 16000, "memory_mib": 65536},
                "general-cpu": {"cpu_millicores": 8000, "memory_mib": 32768},
            },
            "external_cluster_queues": {
                "reference-data-cpu": {
                    "namespaces": ["fs2-academic-poc"],
                    "core_quota": {"cpu_millicores": 24000, "memory_mib": 98304},
                },
                "general-cpu": {
                    "namespaces": ["fs2-models"],
                    "core_quota": {"cpu_millicores": 16000, "memory_mib": 65536},
                },
            },
            "external_local_queues": {
                "academic-reference-cpu": {
                    "namespace": "fs2-academic-poc",
                    "cluster_queue": "reference-data-cpu",
                },
                "general-cpu": {
                    "namespace": "fs2-models",
                    "cluster_queue": "general-cpu",
                },
            },
        }
    )

    with tempfile.TemporaryDirectory(prefix="fs2-cpu-v1-render-") as raw_directory:
        directory = Path(raw_directory)
        module_path = (ROOT / "modules/kueue-scheduling").as_posix()
        (directory / "main.tf").write_text(
            f'''module "scheduling" {{
  source = "{module_path}"
  pools = var.pools
  default_queue = var.default_queue
  scheduling = var.scheduling
  base_priority_classes = var.base_priority_classes
  core_capacity = var.core_capacity
  cpu_classes = var.cpu_classes
  cpu_stage_requests = var.cpu_stage_requests
  external_cluster_queues = var.external_cluster_queues
  external_local_queues = var.external_local_queues
}}
variable "pools" {{ type = any }}
variable "default_queue" {{ type = any }}
variable "scheduling" {{ type = any }}
variable "base_priority_classes" {{ type = any }}
variable "core_capacity" {{ type = any }}
variable "cpu_classes" {{ type = any }}
variable "cpu_stage_requests" {{ type = any }}
variable "external_cluster_queues" {{ type = any }}
variable "external_local_queues" {{ type = any }}
output "contract_json" {{ value = jsonencode(module.scheduling.contract) }}
output "contract_sha256" {{ value = module.scheduling.contract_sha256 }}
''',
            encoding="utf-8",
        )
        (directory / "valid.tfvars.json").write_text(
            json.dumps(values, sort_keys=True), encoding="utf-8"
        )
        initialized = _run(
            terraform, directory, "init", "-backend=false", "-input=false", "-no-color"
        )
        assert initialized.returncode == 0, initialized.stderr
        plan_path = directory / "cpu-v1.tfplan"
        planned = _run(
            terraform,
            directory,
            "plan",
            "-input=false",
            "-no-color",
            "-var-file=valid.tfvars.json",
            f"-out={plan_path}",
        )
        assert planned.returncode == 0, planned.stderr
        shown = _run(terraform, directory, "show", "-json", str(plan_path))
        assert shown.returncode == 0, shown.stderr

        outputs = json.loads(shown.stdout)["planned_values"]["outputs"]
        exact_contract_json = outputs["contract_json"]["value"]
        contract = json.loads(exact_contract_json)
        cpu_document = {
            "schema": contract["cpu_classes_schema"],
            "cpu_classes": contract["cpu_classes"],
        }
        schema = json.loads(
            (ROOT / "catalog/runtime/schema/cpu-stage-classes.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema).validate(cpu_document)
        assert "node_label_key" not in contract["cpu_classes"]["reference-data"][
            "pool_resolution"
        ]
        assert "pool_id" not in contract["cpu_classes"]["general-cpu"][
            "pool_resolution"
        ]
        assert outputs["contract_sha256"]["value"] == hashlib.sha256(
            exact_contract_json.encode("utf-8")
        ).hexdigest()
