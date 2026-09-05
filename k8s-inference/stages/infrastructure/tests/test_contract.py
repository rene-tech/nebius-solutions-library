from __future__ import annotations

import copy
import json
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")


def normalized_private_cidrs(status: dict) -> set[str]:
    return {
        cidr
        for pool in status.get("ipv4_private_pools", [])
        for cidr in pool.get("cidrs", [])
    }


def run_jq_filter(
    filter_name: str,
    document: dict,
    *,
    json_args: dict[str, object],
    string_args: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["jq", "-e"]
    for name, value in json_args.items():
        command.extend(("--argjson", name, json.dumps(value)))
    for name, value in (string_args or {}).items():
        command.extend(("--arg", name, value))
    command.extend(("-f", str(ROOT / "preflight" / filter_name)))
    return subprocess.run(
        command,
        input=json.dumps(document),
        capture_output=True,
        text=True,
        check=False,
    )


class DisposableTerraformContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.terraform = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(ROOT.glob("*.tf"))
        )

    def test_root_uses_local_backend_and_pinned_provider(self) -> None:
        self.assertIn('backend "local" {}', self.terraform)
        versions = (ROOT / "versions.tf").read_text(encoding="utf-8")
        self.assertIn('version = ">= 0.5.232"', versions)

    def test_resource_name_preserves_legacy_default_and_accepts_explicit_name(
        self,
    ) -> None:
        self.assertIn(
            'resource_name = coalesce(var.cluster_name, "${var.name_prefix}-${var.run_id}")',
            self.terraform,
        )
        self.assertIn('default     = "fs2-disposable"', self.terraform)
        self.assertIn('variable "cluster_name"', self.terraform)
        self.assertIn('default     = null', self.terraform)
        self.assertNotIn('name      = "fs2-serve-', self.terraform)

    def test_lifecycle_is_ephemeral_and_cache_is_deletable(self) -> None:
        storage = (ROOT / "storage.tf").read_text(encoding="utf-8")
        cache = storage.split(
            'resource "nebius_compute_v1_filesystem" "cache"', 1
        )[1].split(
            'resource "nebius_compute_v1_filesystem" "reference_data"', 1
        )[0]
        self.assertIn('retention   = "ephemeral"', self.terraform)
        self.assertIn('forbid_deletion  = optional(bool, false)', self.terraform)
        self.assertIn(
            'forbid_deletion  = local.effective_shared_cache.forbid_deletion',
            cache,
        )
        self.assertNotIn("prevent_destroy", cache)
        self.assertEqual(storage.count("prevent_destroy = true"), 2)
        self.assertEqual(storage.count('retention = "durable"'), 5)
        self.assertIn('retention = "disposable-empty-only"', storage)

    def test_reference_lifecycle_distinguishes_retained_and_empty_full_destroy(
        self,
    ) -> None:
        outputs = (ROOT / "outputs.tf").read_text(encoding="utf-8")
        self.assertIn("blocked-retained", outputs)
        self.assertIn(
            "full-stack-destroy-incomplete-infrastructure-retained", outputs
        )
        self.assertIn("eligible-only-while-bucket-empty", outputs)
        self.assertIn("full-only-when-versioned-bucket-empty", outputs)
        self.assertIn("ids-exported-for-explicit-state-adoption", outputs)

    def test_reference_access_handoff_maps_zero_based_provider_version_to_positive_revision(
        self,
    ) -> None:
        outputs = (ROOT / "outputs.tf").read_text(encoding="utf-8")
        handoff = outputs.split(
            'output "reference_data_object_storage_access"', 1
        )[1].split('output "gateway_allocation_id"', 1)[0]
        self.assertIn(
            "nebius_iam_v2_access_key.reference_data[0].resource_version + 1",
            handoff,
        )
        self.assertIn("positive Kubernetes write-only-data revision", handoff)

    def test_reference_data_stage_defaults_to_disposable_and_deletable(self) -> None:
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        reference_default = variables.split(
            'variable "reference_data"', 1
        )[1].split("  validation {", 1)[0]
        self.assertIn('retention_mode = "disposable"', reference_default)
        self.assertIn("forbid_deletion  = false", reference_default)
        self.assertNotIn('retention_mode = "retain"', reference_default)
        self.assertNotIn("forbid_deletion  = true", reference_default)

    def test_reference_data_nodes_mount_both_filesystems_for_runtime_cache_pvcs(
        self,
    ) -> None:
        cluster = (ROOT / "cluster.tf").read_text(encoding="utf-8")
        reference_data = cluster.split(
            'resource "nebius_mk8s_v1_node_group" "reference_data"', 1
        )[1].split(
            'resource "nebius_mk8s_v1_node_group" "general_cpu"', 1
        )[0]

        self.assertIn(
            '"storage.fs2.nebius/shared-cache"    = "true"', reference_data
        )
        self.assertIn(
            '"storage.fs2.nebius/reference-data"  = "true"', reference_data
        )
        self.assertIn(
            "filesystems        = local.shared_cache_reference_data_filesystem_attachment",
            reference_data,
        )
        self.assertIn(
            "cloud_init_user_data = local.shared_cache_reference_data_cloud_init_user_data",
            reference_data,
        )
        self.assertIn("nebius_compute_v1_filesystem.cache", reference_data)
        self.assertIn("nebius_compute_v1_filesystem.reference_data", reference_data)

        # The CSI chart remains fail-closed: its node plugin runs only where
        # Terraform truthfully attached and mounted the shared filesystem.
        foundation = (
            ROOT.parents[0] / "foundation" / "releases.tf"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'key      = "storage.fs2.nebius/shared-cache"', foundation
        )
        self.assertIn('values   = ["true"]', foundation)

    def test_current_gpu_resources_derive_from_typed_b300_pool_profile(self) -> None:
        cluster = (ROOT / "cluster.tf").read_text(encoding="utf-8")
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        pools = json.loads(
            (
                ROOT.parents[1] / "catalog/profiles/accelerator-pools.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn('resource "nebius_mk8s_v1_node_group" "gpu"', cluster)
        self.assertIn("for_each = local.standard_gpu_pools", cluster)
        self.assertIn(
            'each.value.capacity.default_mode == "preemptible" ? {} : null',
            cluster,
        )
        self.assertIn("accelerator-pools.json", variables)
        self.assertIn("accelerator-pool-profiles.json", variables)
        self.assertIn("platform = each.value.provider.platform", cluster)
        self.assertIn("preset   = each.value.provider.preset", cluster)
        one = pools["pool_templates"]["nebius-b300-preemptible-1x"]
        eight = pools["pool_templates"]["nebius-b300-preemptible-8x"]
        self.assertEqual(one["provider"]["platform"], "gpu-b300-sxm")
        self.assertEqual(one["provider"]["preset"], "1gpu-24vcpu-346gb")
        self.assertEqual(eight["provider"]["platform"], "gpu-b300-sxm")
        self.assertEqual(eight["provider"]["preset"], "8gpu-192vcpu-2768gb")
        self.assertEqual(one["capacity"]["allowed_modes"], ["preemptible"])
        self.assertEqual(eight["capacity"]["allowed_modes"], ["preemptible"])

    def test_only_eight_gpu_pool_requests_local_disks(self) -> None:
        cluster = (ROOT / "cluster.tf").read_text(encoding="utf-8")
        pools = json.loads(
            (
                ROOT.parents[1] / "catalog/profiles/accelerator-pools.json"
            ).read_text(encoding="utf-8")
        )["pool_templates"]
        self.assertIn(
            'each.value.features.local_storage.mode == "host-local-nvme" ? '
            "local.gpu_local_disks[each.value.features.local_storage.provider_config] : null",
            cluster,
        )
        self.assertIn('"passthrough-none"', cluster)
        self.assertIn('"kubelet-ephemeral"', cluster)
        self.assertIn("kubelet_ephemeral = true", cluster)
        enabled_local = {
            pool_id
            for pool_id, pool in pools.items()
            if pool["enabled"]
            and pool["features"]["local_storage"]["mode"] == "host-local-nvme"
        }
        self.assertEqual(enabled_local, {"nebius-b300-preemptible-8x"})

    def test_capacity_block_pools_are_fixed_and_never_preemptible(self) -> None:
        cluster = (ROOT / "cluster.tf").read_text(encoding="utf-8")
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        self.assertIn("reservation_policy = optional(object({", variables)
        self.assertIn('contains(["AUTO", "STRICT"]', variables)
        self.assertIn('pool.capacity_type == "regular"', variables)
        self.assertIn("pool.min_nodes == pool.max_nodes", variables)
        self.assertIn(
            'fixed_node_count = each.value.provider.reservation_policy != "FORBID" ? each.value.max_nodes : null',
            cluster,
        )
        self.assertIn(
            'autoscaling = each.value.provider.reservation_policy == "FORBID" ? {',
            cluster,
        )
        self.assertIn("reservation_ids = length(try(", cluster)
        self.assertIn(
            'max_surge       = { count = each.value.provider.reservation_policy == "FORBID" ? 1 : 0 }',
            cluster,
        )
        self.assertIn(
            'max_unavailable = { count = each.value.provider.reservation_policy == "FORBID" ? 0 : 1 }',
            cluster,
        )

    def test_full_catalog_capacity_profile_is_explicit(self) -> None:
        profiles = (
            ROOT.parents[1] / "catalog/profiles/capacity-profiles.json"
        ).read_text(encoding="utf-8")
        self.assertIn('"gpu_1x_max_nodes": 7', profiles)
        self.assertIn('"gpu_8x_max_nodes": 2', profiles)
        self.assertIn('"maximum_gpus": 23', profiles)
        self.assertIn('"shared_cache_size_gib": 2048', profiles)

    def test_public_catalog_contains_no_private_target_or_registry_identity(
        self,
    ) -> None:
        contract = json.loads(
            (
                ROOT.parents[1] / "catalog/profiles/approved-targets.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            contract["schema"], "fs2-serve.nebius.ai/terraform-approved-targets/v1"
        )
        self.assertEqual(contract, {
            "schema": "fs2-serve.nebius.ai/terraform-approved-targets/v1",
            "targets": {},
        })

    def test_project_scoped_registry_pull_and_public_edge_are_owned(self) -> None:
        iam = (ROOT / "iam.tf").read_text(encoding="utf-8")
        data = (ROOT / "data.tf").read_text(encoding="utf-8")
        cluster = (ROOT / "cluster.tf").read_text(encoding="utf-8")
        network = (ROOT / "network.tf").read_text(encoding="utf-8")
        outputs = (ROOT / "outputs.tf").read_text(encoding="utf-8")
        self.assertIn(
            'resource "nebius_iam_v1_group" "target_registry_readers" {\n'
            "  # Access permits inherit the group's scope, so the run-owned registry uses a\n"
            "  # group in the target project. External registries receive their own groups\n"
            "  # in the projects that own them below; no tenant-level IAM write is needed.\n"
            "  parent_id = data.nebius_iam_v2_project.target.id",
            iam,
        )
        self.assertIn(
            'resource "nebius_iam_v1_group_membership" "nodepull_target_registry" {\n'
            "  parent_id = nebius_iam_v1_group.target_registry_readers.id\n"
            "  member_id = nebius_iam_v1_service_account.nodepull.id",
            iam,
        )
        self.assertIn(
            'resource "nebius_iam_v1_access_permit" "nodepull_registry" {\n'
            "  # Managed Kubernetes node credential exchange currently requires the\n"
            "  # node-group service account to hold viewer at project scope. A viewer permit\n"
            "  # attached only to the Registry resource is accepted by IAM but kubelet image\n"
            "  # pulls receive 403 for manifests that are not already cached on the node.\n"
            "  # Keep this in the target project (rather than a tenant default group), and\n"
            "  # retain the dedicated run-owned group so destroy removes the grant.\n"
            "  parent_id   = nebius_iam_v1_group.target_registry_readers.id\n"
            "  resource_id = data.nebius_iam_v2_project.target.id\n"
            '  role        = "viewer"',
            iam,
        )
        self.assertIn(
            'data "nebius_registry_v1_registry" "external" {\n'
            "  for_each = var.external_registry_ids\n\n"
            "  id = each.value",
            data,
        )
        self.assertIn(
            'resource "nebius_iam_v1_group" "external_registry_readers" {\n'
            "  for_each = data.nebius_registry_v1_registry.external\n\n"
            "  parent_id = each.value.parent_id",
            iam,
        )
        self.assertIn(
            'resource "nebius_iam_v1_group_membership" "nodepull_external_registry" {\n'
            "  for_each = data.nebius_registry_v1_registry.external\n\n"
            "  parent_id = nebius_iam_v1_group.external_registry_readers[each.key].id\n"
            "  member_id = nebius_iam_v1_service_account.nodepull.id",
            iam,
        )
        self.assertIn(
            'resource "nebius_iam_v1_access_permit" "nodepull_external_registry" {\n'
            "  for_each = data.nebius_registry_v1_registry.external\n\n"
            "  parent_id   = nebius_iam_v1_group.external_registry_readers[each.key].id\n"
            "  resource_id = each.key\n"
            '  role        = "viewer"',
            iam,
        )
        self.assertNotIn(
            "parent_id = data.nebius_iam_v2_project.target.parent_id",
            iam,
        )
        self.assertNotIn(
            "parent_id   = nebius_iam_v1_service_account.nodepull.id",
            iam,
        )
        self.assertIn(
            "    nebius_iam_v1_group_membership.nodepull_external_registry,\n"
            "    nebius_iam_v1_access_permit.nodepull_registry,\n"
            "    nebius_iam_v1_access_permit.nodepull_external_registry,",
            cluster,
        )
        self.assertIn(
            "    external_reader_groups = {\n"
            "      for registry_id, group in nebius_iam_v1_group.external_registry_readers : registry_id => group.id\n"
            "    }",
            outputs,
        )
        self.assertIn('resource "nebius_vpc_v1_allocation" "gateway"', network)
        self.assertIn(
            'resource "nebius_vpc_v1_security_rule" "workers_public_edge_ingress"',
            network,
        )

    def test_full_catalog_zero_floor_remains_elastic(self) -> None:
        capacity = json.loads(
            (
                ROOT.parents[1] / "catalog/profiles/capacity-profiles.json"
            ).read_text(encoding="utf-8")
        )
        selected = capacity["capacity_profiles"]["full_catalog"]
        self.assertEqual(selected["system_nodes"], 3)
        self.assertEqual(selected["gpu_1x_max_nodes"], 7)
        self.assertEqual(selected["gpu_8x_max_nodes"], 2)
        self.assertEqual(selected["maximum_gpus"], 23)
        self.assertEqual(selected["shared_cache_size_gib"], 2048)
        zero_floor = capacity["floor_profiles"]["zero"]
        self.assertEqual(zero_floor["gpu_1x_min_nodes"], 0)
        self.assertEqual(zero_floor["gpu_8x_min_nodes"], 0)

    def test_system_update_strategy_has_target_default_and_typed_override(self) -> None:
        cluster = (ROOT / "cluster.tf").read_text(encoding="utf-8")
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        self.assertIn("local.effective_system_pool.max_surge", cluster)
        self.assertIn(
            "local.effective_system_pool.max_unavailable", cluster
        )
        self.assertIn(
            "coalesce(var.system_pool.max_surge, local.selected_target.system_update_strategy.max_surge)",
            variables,
        )
        self.assertIn(
            "coalesce(var.system_pool.max_unavailable, local.selected_target.system_update_strategy.max_unavailable)",
            variables,
        )

    def test_generated_target_adapter_is_provider_verified(self) -> None:
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        data = (ROOT / "data.tf").read_text(encoding="utf-8")
        self.assertIn('variable "target_binding"', variables)
        self.assertIn('project_id          = string', variables)
        self.assertIn('private_subnet_cidr = string', variables)
        self.assertIn(
            "nonsensitive(data.nebius_iam_v2_project.target.id) == local.selected_target.project_id",
            data,
        )
        self.assertIn(
            "data.nebius_iam_v2_project.target.region == local.selected_target.region",
            data,
        )
        self.assertIn(
            'try(data.nebius_vpc_v1_network.target.status.state, "") == "READY"',
            data,
        )
        self.assertIn(
            "contains(local.target_subnet_private_cidrs, local.selected_target.private_subnet_cidr)",
            data,
        )

    def test_customer_inputs_cannot_self_assert_accelerator_qualification(
        self,
    ) -> None:
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        self.assertIn('variable "accelerator_pool_profile"', variables)
        self.assertIn(
            "effective_accelerator_pool_profile = coalesce(var.accelerator_pool_profile, var.capacity_profile)",
            variables,
        )
        self.assertIn(
            "selected_accelerator_pool_profile  = local.accelerator_pool_profile_contract.profiles[local.effective_accelerator_pool_profile]",
            variables,
        )
        target_block = variables.split('variable "target_binding"', 1)[1].split(
            'variable "source_commit"', 1
        )[0]
        for provider_fact in (
            "accelerator_class",
            "driver_preset",
            "resource_name",
            "region_availability",
        ):
            self.assertNotIn(provider_fact, target_block)

    def test_system_cache_and_profile_overrides_disable_legacy_v1_handoff(
        self,
    ) -> None:
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        for guard in (
            "local.effective_accelerator_pool_profile == var.capacity_profile",
            "var.target_binding == null",
            "var.system_pool == null",
            "var.shared_cache == null",
        ):
            self.assertIn(guard, variables)

    def test_nebius_profile_is_portable_but_bounded(self) -> None:
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        profile_block = variables.split('variable "nebius_profile"', 1)[1].split(
            'variable "project_id"', 1
        )[0]
        self.assertIn('default     = "sandbox"', profile_block)
        self.assertIn("[A-Za-z0-9._-]", profile_block)
        self.assertNotIn('var.nebius_profile == "sandbox"', profile_block)

    def test_subnet_contract_normalizes_provider_0628_private_pool_shape(
        self,
    ) -> None:
        data = (ROOT / "data.tf").read_text(encoding="utf-8")
        fixture = json.loads(
            (FIXTURES / "nebius-provider-0.6.28-subnet-status.json").read_text(
                encoding="utf-8"
            )
        )
        expected = {"10.104.0.0/13"}

        self.assertEqual(normalized_private_cidrs(fixture["status"]), expected)
        with_extra = json.loads(json.dumps(fixture["status"]))
        with_extra["ipv4_private_pools"].append(
            {"cidrs": ["10.80.0.0/16"], "pool_id": "vpcpool-extra"}
        )
        self.assertNotEqual(normalized_private_cidrs(with_extra), expected)

        self.assertIn("target_subnet_private_pool_cidrs = toset(flatten([", data)
        self.assertIn(
            "data.nebius_vpc_v1_subnet.target.status.ipv4_private_pools", data
        )
        self.assertIn(
            "tolist(data.nebius_vpc_v1_subnet.target.status.ipv4_private_pools)",
            data,
        )
        self.assertIn("try(tolist(pool.cidrs), [])", data)
        self.assertIn(
            "contains(local.target_subnet_private_cidrs, "
            "local.selected_target.private_subnet_cidr)",
            data,
        )
        self.assertIn(
            "length(local.target_subnet_private_pool_cidrs) > 0 ? "
            "local.target_subnet_private_pool_cidrs",
            data,
        )
        self.assertEqual(data.count("status.ipv4_private_cidrs"), 1)

    def test_read_only_preflight_filters_accept_cli_and_provider_shapes(
        self,
    ) -> None:
        target = {
            "project_name": "synthetic-local-project",
            "subnet_name": "synthetic-subnet",
            "private_subnet_cidr": "10.104.0.0/13",
        }
        tenant = "tenant-syntheticlocal"
        project_shapes = json.loads(
            (FIXTURES / "nebius-project-status-shapes.json").read_text(encoding="utf-8")
        )
        cli_subnets = json.loads(
            (FIXTURES / "nebius-cli-subnet-list.json").read_text(encoding="utf-8")
        )
        provider_status = json.loads(
            (FIXTURES / "nebius-provider-0.6.28-subnet-status.json").read_text(
                encoding="utf-8"
            )
        )["status"]
        provider_subnets = {
            "items": [
                {
                    "metadata": {"name": target["subnet_name"]},
                    "status": {"state": "READY", **provider_status},
                }
            ]
        }

        for project in (project_shapes["cli"], project_shapes["provider"]):
            result = run_jq_filter(
                "verify-project.jq",
                project,
                json_args={"contract": target},
                string_args={"tenant": tenant},
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        for subnets in (cli_subnets, provider_subnets):
            result = run_jq_filter(
                "verify-subnet.jq", subnets, json_args={"target": target}
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        inactive_project = copy.deepcopy(project_shapes["cli"])
        inactive_project["status"]["container_state"] = "SUSPENDED"
        wrong_parent_project = copy.deepcopy(project_shapes["provider"])
        wrong_parent_project["metadata"]["parent_id"] = "tenant-wrong"
        for project in (inactive_project, wrong_parent_project):
            result = run_jq_filter(
                "verify-project.jq",
                project,
                json_args={"contract": target},
                string_args={"tenant": tenant},
            )
            self.assertNotEqual(result.returncode, 0)

        missing_cidr = copy.deepcopy(cli_subnets)
        missing_cidr["items"][0]["status"].pop("ipv4_private_cidrs")
        extra_cli_cidr = copy.deepcopy(cli_subnets)
        extra_cli_cidr["items"][0]["status"]["ipv4_private_cidrs"].append(
            "10.80.0.0/16"
        )
        extra_provider_cidr = copy.deepcopy(provider_subnets)
        extra_provider_cidr["items"][0]["status"]["ipv4_private_pools"].append(
            {"cidrs": ["10.80.0.0/16"], "pool_id": "vpcpool-extra"}
        )
        for subnets in (missing_cidr, extra_cli_cidr, extra_provider_cidr):
            result = run_jq_filter(
                "verify-subnet.jq", subnets, json_args={"target": target}
            )
            self.assertNotEqual(result.returncode, 0)

        project_filter = (ROOT / "preflight/verify-project.jq").read_text(
            encoding="utf-8"
        )
        subnet_filter = (ROOT / "preflight/verify-subnet.jq").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            ".status.container_state // .status.project_state", project_filter
        )
        self.assertIn("(.items // [])[]", subnet_filter)

    def test_public_registry_closure_is_unbound_and_canary_is_placeholder(self) -> None:
        closure = json.loads(
            (
                ROOT.parents[1] / "catalog/profiles/source-registry-closure.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            closure["schema"], "fs2-serve.nebius.ai/source-registry-closure/v1"
        )
        self.assertIsNone(closure["registry_id"])
        self.assertEqual(closure["references"], [])
        self.assertIn("project-local registry", closure["policy"])

        canary = yaml.safe_load(
            (ROOT / "smoke/source-registry-pull-job.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(canary["kind"], "Job")
        container = canary["spec"]["template"]["spec"]["containers"][0]
        self.assertRegex(
            container["image"],
            r"^registry\.example\.invalid/.+@sha256:[a-f0-9]{64}$",
        )
        self.assertEqual(container["imagePullPolicy"], "Always")
        self.assertEqual(
            canary["spec"]["template"]["spec"]["nodeSelector"],
            {
                "workload.fs2.nebius/system": "true",
                "capacity.fs2.nebius/type": "regular",
            },
        )

    def test_plan_verifier_explicitly_denylists_shared_clusters(self) -> None:
        verifier = (ROOT / "tests/verify_plan.py").read_text(encoding="utf-8")
        self.assertIn('"mk8scluster-syntheticretained"', verifier)
        self.assertIn('"mk8scluster-syntheticlegacy"', verifier)
        self.assertIn(
            'parser.add_argument("--expected-project-id", required=True)', verifier
        )
        self.assertIn(
            'parser.add_argument("--expected-source-commit", required=True)', verifier
        )

    def test_lifecycle_runbook_binds_destroy_to_protected_run_inputs(
        self,
    ) -> None:
        wrapper = (ROOT.parents[1] / "inference-stack").read_text(encoding="utf-8")
        for exact_guard in (
            "private_directory(run_root)",
            "lock_path.chmod(0o600)",
            "fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
            "contract, configuration_environment = validate_configuration(",
            "args.terraform, variable_file, run_root",
            "destroy_stack(args, run_root, contract, commit)",
            'reference_infrastructure.get("lifecycle", {}).get("retention_mode")',
            '== "retain"',
        ):
            self.assertIn(exact_guard, wrapper)

    def test_infrastructure_contract_binds_source_target_and_full_topology(
        self,
    ) -> None:
        variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        data = (ROOT / "data.tf").read_text(encoding="utf-8")
        outputs = (ROOT / "outputs.tf").read_text(encoding="utf-8")
        self.assertIn('variable "source_commit"', variables)
        self.assertIn(
            'schema        = "fs2-serve.nebius.ai/terraform-infrastructure-contract/v1"',
            variables,
        )
        for value in (
            "source_commit = var.source_commit",
            "project_id = nonsensitive(var.project_id)",
            "maximum_gpus",
            "shared_cache_size_gib",
            "gpus_per_node = local.current_gpu_pool_1x.node.gpus_per_node",
            "gpus_per_node = local.current_gpu_pool_8x.node.gpus_per_node",
            "min_nodes",
            "max_nodes",
            "max_surge",
            "max_unavailable",
        ):
            self.assertIn(value, variables)
        self.assertIn('output "accelerator_pool_contract_sha256"', outputs)
        self.assertIn(
            "value       = sha256(jsonencode(local.resolved_accelerator_pool_contract))",
            outputs,
        )
        self.assertIn('output "infrastructure_contract"', outputs)

    def test_cuda_smoke_proves_b300_sm103_with_secret_free_json_evidence(self) -> None:
        manifest = (ROOT / "smoke/cuda-job.yaml").read_text(encoding="utf-8")
        self.assertIn("torch.cuda.is_available()", manifest)
        self.assertIn("torch.cuda.get_device_capability(0)", manifest)
        self.assertIn("compute_capability != (10, 3)", manifest)
        self.assertIn("GPU_COMPUTE_CAPABILITY_NOT_SM103", manifest)
        self.assertIn(
            "--query-gpu=uuid,name,compute_cap,driver_version,memory.total", manifest
        )
        self.assertIn('"schema": "fs2-serve.nebius.ai/cuda-smoke/v2"', manifest)
        self.assertIn("fieldPath: spec.nodeName", manifest)
        self.assertIn(
            "@sha256:0fec7ec5f3e6bc168e54899935fb0557da908a4832a1dbc88e2debcf2f889416",
            manifest,
        )


if __name__ == "__main__":
    unittest.main()
